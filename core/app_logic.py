"""
Гибридный клиент SoundCloud + YouTube на базе yt-dlp.

Поиск идёт в SoundCloud, а если трек защищён (SoundCloud Go+/DRM) —
автоматически подбирается полноценная версия на YouTube.
"""
import asyncio
import html
import io
import os
import re
import socket
import time
from typing import Optional, Callable

import aiohttp
from aiohttp import ClientTimeout
import yt_dlp

# Заполненный/пустой блок прогресс-бара (экранированные литералы — не ломаются
# при любой кодировке исходника).
BAR_FILLED = "\u2588"
BAR_EMPTY = "\u2591"

# Иконки, используемые в прогрессе загрузки.
ICON_HOURGLASS = "\u23f3"
ICON_DOWNLOAD = "\u2b07"


def _log(message: str, important: bool = False) -> None:
    """Единая точка логирования: видно в journalctl -u not_a_music."""
    try:
        print(f"{'!!' if important else '[music]'} {message}", flush=True)
    except Exception:
        pass


class _DiagLogger:
    """Логгер yt-dlp: раньше тут был `pass`, из-за чего ошибки YouTube
    («No supported JavaScript runtime», «nsig extraction failed») терялись."""

    _seen: dict = {}
    _LIMIT = 5  # одинаковые сообщения не спамим

    def __init__(self, quiet: bool = True):
        self.quiet = quiet

    def _emit(self, prefix: str, msg: str):
        text = " ".join(str(msg).split())
        if not text:
            return
        key = text[:120]
        count = self._seen.get(key, 0) + 1
        self._seen[key] = count
        if count <= self._LIMIT:
            _log(f"{prefix} {text[:300]}")

    def debug(self, msg):
        if not self.quiet and not str(msg).startswith("[debug] "):
            self._emit("", msg)

    def info(self, msg):
        if not self.quiet:
            self._emit("", msg)

    def warning(self, msg):
        self._emit("⚠ yt-dlp:", msg)

    def error(self, msg):
        self._emit("❌ yt-dlp:", msg)


# Сколько параллельных соединений использовать при скачивании аудио.
# 6 уже даёт всплески 403 у googlevideo, 3 — стабильно.
DL_WORKERS = int(os.getenv("YTDLP_DL_WORKERS") or 3)
DL_CHUNK = int(os.getenv("YTDLP_DL_CHUNK") or 1_048_576)
# Сколько раз повторять упавший чанк, прежде чем считать загрузку неудачной.
DL_RETRIES = int(os.getenv("YTDLP_DL_RETRIES") or 3)

_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


class TrackUnavailable(Exception):
    """Трек невозможно получить (нет ссылки, обрезан, DRM)."""


def ytdl_env_opts() -> dict:
    """Настройки yt-dlp из окружения — чтобы YouTube можно было починить без правок кода.

    YTDLP_COOKIES        — путь к cookies.txt (сильно помогает YouTube)
    YTDLP_PLAYER_CLIENT  — клиенты через запятую, например: default,android_vr
    YTDLP_PROXY          — прокси, например: socks5://user:pass@host:1080
    YTDLP_JS_RUNTIMES    — JS-рантаймы для расшифровки n-sig, по умолчанию «deno,node»
    """
    opts: dict = {}
    cookies = (os.getenv("YTDLP_COOKIES") or "").strip()
    if cookies and os.path.exists(cookies):
        opts["cookiefile"] = cookies
    clients = [c.strip() for c in (os.getenv("YTDLP_PLAYER_CLIENT") or "").split(",") if c.strip()]
    if clients:
        opts["extractor_args"] = {"youtube": {"player_client": clients}}
    proxy = (os.getenv("YTDLP_PROXY") or "").strip()
    if proxy:
        opts["proxy"] = proxy
    # yt-dlp по умолчанию ищет только deno; node ставим в deploy.sh — дадим оба.
    # Без JS-рантайма YouTube не расшифровывает n-sig: форматы деградируют
    # до 48 kbps, а загрузка обрывается на первом мегабайте.
    # Формат опции — словарь {рантайм: конфиг}; можно «deno:/usr/local/bin/deno».
    runtimes = {}
    for raw in (os.getenv("YTDLP_JS_RUNTIMES") or "deno,node").split(","):
        item = raw.strip().lower()
        if not item:
            continue
        if ":" in item:
            name, _, path = item.partition(":")
            runtimes[name.strip()] = {"path": path.strip()}
        else:
            runtimes[item] = {}
    opts["js_runtimes"] = runtimes
    return opts


def _pick_best_audio(formats, prefer_ext=("m4a", "mp3"),
                     min_compatible_ratio: float = 0.8, allow_hls: bool = True):
    """Выбирает наиболее качественный аудио-формат.

    Раньше брался ПЕРВЫЙ m4a из списка, а yt-dlp отдаёт форматы без сортировки
    по битрейту — из-за этого для YouTube выбирался `139` (48 kbps) вместо
    `140` (128 kbps). Здесь сортируем по битрейту и предпочитаем m4a/mp3
    (Telegram понимает их как аудио), но не теряем качество: если webm/opus
    заметно быстрее — берём его.

    allow_hls=True важен для SoundCloud: там НЕТ прямых mp3-файлов, только
    HLS-дорожки (hls_mp3_1_0 / hls_aac_160k). Старый код брал m3u8-ссылку и
    отправлял в Telegram текст плейлиста вместо музыки.
    """
    candidates = []
    for fmt in formats or []:
        if not isinstance(fmt, dict):
            continue
        ext = str(fmt.get("ext") or "").lower()
        protocol = str(fmt.get("protocol") or "")
        note = str(fmt.get("format_note") or "").lower()
        if fmt.get("has_drm"):
            continue
        if str(fmt.get("vcodec") or "none").lower() not in ("none", ""):
            continue                      # это видеодорожка
        if not fmt.get("url"):
            continue
        if ext in ("mhtml",) or "storyboard" in note:
            continue                      # набор кадров — не аудио
        if "preview" in str(fmt.get("format_id") or "").lower() or "preview" in note:
            continue                      # 30-сек сниппет SoundCloud Go+
        if protocol in ("m3u8", "m3u8_native") and not allow_hls:
            continue                      # HLS напрямую стримить не умеем
        if not (protocol.startswith("http") or protocol in ("m3u8", "m3u8_native")):
            continue
        candidates.append(fmt)

    if not candidates:
        return None

    def bitrate(fmt) -> float:
        return float(fmt.get("abr") or fmt.get("tbr") or 0)

    best_compatible = max(
        (f for f in candidates if str(f.get("ext") or "").lower() in prefer_ext),
        key=bitrate, default=None)
    best_any = max(candidates, key=bitrate, default=None)

    if best_compatible and (not best_any or
                            bitrate(best_compatible) >= bitrate(best_any) * min_compatible_ratio):
        return best_compatible
    return best_any or best_compatible


# Слова-маркеры «не той» версии трека, которые часто вылезают в выдаче YouTube.
_BAD_VERSION_WORDS = (
    "slowed", "nightcore", "sped up", "speed up", "speedup", "8d audio", "reverb",
    "remix", "cover", "instrumental", "karaoke", "mashup", "extended", "loop",
    "tiktok version", "chopped",
)


def score_candidate(query: str, candidate: dict, duration_hint: int = 0) -> float:
    """Оценка кандидата из поиска YouTube: длительность + совпадение слов + штрафы."""
    score = 0.0
    title_l = str(candidate.get("title") or "").lower()
    duration = float(candidate.get("duration") or 0)
    query_l = (query or "").lower()

    if duration_hint and duration:
        diff = abs(duration - duration_hint) / max(float(duration_hint), 1.0)
        if diff <= 0.05:
            score += 3.0
        elif diff <= 0.15:
            score += 2.0
        elif diff <= 0.3:
            score += 1.0
        else:
            score -= 2.5          # явно другая версия или другой трек

    words = {w for w in re.split(r"\W+", query_l) if len(w) > 2}
    if words:
        found = {w for w in re.split(r"\W+", title_l) if len(w) > 2}
        score += 2.0 * len(words & found) / len(words)

    for word in _BAD_VERSION_WORDS:
        if word in title_l and word not in query_l:
            score -= 2.0
            break
    return score


def parse_content_range(value: str) -> tuple:
    """Разбирает Content-Range: 'bytes 0-1023/4096' -> (0, 1023, 4096)."""
    if not value:
        return 0, 0, 0
    match = re.match(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", str(value).strip(), re.IGNORECASE)
    if not match:
        return 0, 0, 0
    total = 0 if match.group(3) == "*" else int(match.group(3))
    return int(match.group(1)), int(match.group(2)), total



def format_progress_bar(downloaded: int, total: int, width: int = 10) -> str:
    """Собирает прогресс-бар вида: [██████░░░░] 45% (3.2/7.1 MB)."""
    downloaded = max(0, int(downloaded or 0))
    total = max(0, int(total or 0))
    if total <= 0:
        mb = downloaded / 1_048_576
        return f"{ICON_HOURGLASS} <b>Загрузка…</b> <code>{mb:.1f} MB</code>"
    pct = min(1.0, downloaded / total)
    filled = int(width * pct)
    bar = BAR_FILLED * filled + BAR_EMPTY * (width - filled)
    dl_mb = downloaded / 1_048_576
    tot_mb = total / 1_048_576
    return f"<code>[{bar}] {int(pct * 100)}%</code> ({dl_mb:.1f}/{tot_mb:.1f} MB)"


def track_display_name(artist: str = "", title: str = "") -> str:
    """Собирает подпись «Исполнитель — Название» без лишних разделителей."""
    artist = str(artist or "").strip().strip("-—–").strip()
    title = str(title or "").strip().strip("-—–").strip()
    if artist and title and artist.lower() != title.lower():
        return f"{artist} — {title}"
    return artist or title


class TelegramProgressReporter:
    UPDATE_INTERVAL = 1.8

    def __init__(self, edit_func: Callable, title: str = ""):
        self._edit = edit_func
        self._title = html.escape(track_display_name(title=title), quote=False) if title else ""
        self._last_update = 0.0

    def render(self, downloaded: int, total: int) -> str:
        """Готовый текст сообщения о прогрессе (HTML)."""
        if self._title:
            header = f"{ICON_DOWNLOAD} <b>Загружаю трек</b>\n<i>{self._title}</i>\n\n"
        else:
            header = f"{ICON_DOWNLOAD} <b>Загружаю трек…</b>\n\n"
        return f"{header}{format_progress_bar(downloaded, total)}"

    async def update(self, downloaded: int, total: int, force: bool = False):
        now = time.monotonic()
        if not force and (now - self._last_update) < self.UPDATE_INTERVAL:
            return
        self._last_update = now
        try:
            await self._edit(self.render(downloaded, total))
        except Exception:
            pass


def _clean_track_id(raw_id: str) -> str:
    raw = str(raw_id).strip()
    if raw.startswith("soundcloud:tracks:"):
        return raw.split(":")[-1]
    return raw


_search_cache: dict = {}
_cache_ttl = 180

# ─────────── SoundCloud api-v2: определение премиум/DRM-треков ───────────

_SC_API_BASE = "https://api-v2.soundcloud.com/"
# Кэш состояний треков: id -> (state_dict, expires_at)
_track_state_cache: dict = {}
_state_ttl = 3600
# Протоколы, которые yt-dlp считает DRM-защищёнными.
_DRM_PROTOCOLS = ("ctr-", "cbc-")
# client_id SoundCloud, который достаёт сам yt-dlp (кэшируется на процесс).
_sc_client_id: Optional[str] = None


class _ApiUnauthorized(Exception):
    """SoundCloud отклонил client_id — нужно получить новый."""


def _load_client_id(force: bool = False) -> Optional[str]:
    """Достаёт client_id SoundCloud тем же способом, что и yt-dlp."""
    global _sc_client_id
    if _sc_client_id and not force:
        return _sc_client_id
    _sc_client_id = None
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True,
                               "logger": _DiagLogger()}) as ydl:
            extractor = ydl.get_info_extractor("Soundcloud")
            extractor.set_downloader(ydl)
            extractor.initialize()
            _sc_client_id = getattr(extractor, "_CLIENT_ID", None) or None
    except Exception as e:
        _log(f"Не удалось получить client_id SoundCloud: {type(e).__name__}: {e}")
        _sc_client_id = None
    return _sc_client_id


def _is_playable_transcoding(transcoding: dict) -> bool:
    """True, если транскодинг можно воспроизвести без подписки и без DRM."""
    if not isinstance(transcoding, dict):
        return False
    protocol = str((transcoding.get("format") or {}).get("protocol") or "")
    url = str(transcoding.get("url") or "")
    if protocol.startswith(_DRM_PROTOCOLS) or "/encrypted-hls" in url:
        return False
    if transcoding.get("snipped") is True or "/preview/" in url:
        return False
    return bool(protocol or url)


def is_premium_track(state: Optional[dict]) -> bool:
    """True, если трек недоступен целиком (SoundCloud Go+, DRM или блокировка).

    Пустое состояние трактуется как «нет данных» — такой трек не отбрасываем,
    чтобы не терять результаты при недоступности api-v2.
    """
    if not state:
        return False
    policy = str(state.get("policy") or "").upper()
    if policy and policy != "ALLOW":
        return True
    if state.get("streamable") is False:
        return True
    track_state = state.get("state")
    if track_state and track_state != "finished":
        return True
    if str(state.get("monetization_model") or "").upper() == "SUB_HIGH_TIER":
        return True
    transcodings = (state.get("media") or {}).get("transcodings") or []
    if transcodings and not any(_is_playable_transcoding(t) for t in transcodings):
        return True
    return False


async def _api_get(session: aiohttp.ClientSession, path: str, params: dict):
    """GET-запрос к api-v2.soundcloud.com (бросает _ApiUnauthorized на 401/403)."""
    async with session.get(f"{_SC_API_BASE}{path}", params=params,
                           timeout=ClientTimeout(total=15)) as resp:
        if resp.status in (401, 403):
            raise _ApiUnauthorized()
        resp.raise_for_status()
        return await resp.json(content_type=None)


async def fetch_track_states(track_ids) -> dict:
    """Батч-запрос состояний треков (policy/DRM/streamable) через api-v2."""
    ids = [str(i) for i in track_ids if i]
    if not ids:
        return {}

    loop = asyncio.get_event_loop()
    session = aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(family=socket.AF_INET, limit=10))
    result: dict = {}
    try:
        pending = ids
        for attempt in (0, 1):
            client_id = await loop.run_in_executor(
                None, lambda f=attempt > 0: _load_client_id(f))
            if not client_id:
                return result
            unauthorized = False
            retry: list = []
            for start in range(0, len(pending), 50):
                chunk = pending[start:start + 50]
                try:
                    data = await _api_get(
                        session, "tracks",
                        {"ids": ",".join(chunk), "client_id": client_id})
                except _ApiUnauthorized:
                    unauthorized = True
                    retry.extend(chunk)
                    continue
                except Exception:
                    continue
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and item.get("id") is not None:
                            result[str(item["id"])] = item
            if not unauthorized or not retry:
                break
            pending = retry
    finally:
        await session.close()
    return result


async def get_track_states(track_ids, use_cache: bool = True) -> dict:
    """Состояния треков с кэшем в памяти (чтобы не дёргать API на каждой странице)."""
    ids = [str(i) for i in track_ids if i]
    if not ids:
        return {}
    # Простейшая защита от неограниченного роста кэша в долгоживущем боте.
    if len(_track_state_cache) > 5000:
        _track_state_cache.clear()
    now = time.monotonic()
    result: dict = {}
    missing: list = []
    for tid in ids:
        cached = _track_state_cache.get(tid)
        if use_cache and cached and now < cached[1]:
            result[tid] = cached[0]
        else:
            missing.append(tid)
    if not missing:
        return result
    fetched = await fetch_track_states(missing)
    for tid, state in fetched.items():
        _track_state_cache[tid] = (state, now + _state_ttl)
    result.update(fetched)
    return result


# ─────────── Второй, независимый способ поиска в SoundCloud (api-v2) ───────────
# yt-dlp-поиск и api-v2 — две разные реализации одного и того же API. Если одна
# ломается (обновили клиент, отдали 403/429), вторая продолжает работать.

def _entry_from_api(item: dict) -> dict:
    """Приводит трек из api-v2 к виду «плоской» записи yt-dlp."""
    duration_ms = item.get("full_duration") or item.get("duration") or 0
    user = item.get("user") or {}
    artwork = item.get("artwork_url") or user.get("avatar_url") or ""
    return {
        "id": item.get("id"),
        "title": item.get("title"),
        "uploader": user.get("username"),
        "duration": float(duration_ms) / 1000.0,
        "webpage_url": item.get("permalink_url"),
        "thumbnails": [{"url": artwork.replace("-large.", "-t500x500.")}] if artwork else [],
        "_api_state": item,      # policy/media уже тут — премиум-фильтр без запроса
    }


async def search_via_api(query: str, limit: int = 40) -> list:
    """Поиск через api-v2 (независимая стратегия). При 401 обновляет client_id."""
    session = aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(family=socket.AF_INET, limit=10))
    loop = asyncio.get_event_loop()
    try:
        for attempt in (0, 1):
            client_id = await loop.run_in_executor(
                None, lambda force=attempt > 0: _load_client_id(force))
            if not client_id:
                _log("api-v2 поиск: нет client_id")
                return []
            try:
                data = await _api_get(session, "search/tracks", {
                    "q": query, "client_id": client_id,
                    "limit": max(1, min(int(limit), 200)), "offset": 0,
                    "linked_partitioning": 1})
            except _ApiUnauthorized:
                _log("api-v2 поиск: 401/403, обновляю client_id")
                continue
            except Exception as e:
                if getattr(e, "status", None) == 429 and attempt == 0:
                    _log("api-v2 поиск: 429 Too Many Requests, пауза 2с")
                    await asyncio.sleep(2)
                    continue
                _log(f"api-v2 поиск не удался: {type(e).__name__}: {e}")
                return []
            collection = (data or {}).get("collection") or []
            return [_entry_from_api(i) for i in collection if isinstance(i, dict)]
    finally:
        await session.close()
    return []


# ─────────── Предохранитель для api-v2 ───────────
# Если SoundCloud стабильно недоступен, не тратим время на него в каждом поиске.
_api_fail_count = 0
_api_disabled_until_ts = 0.0
_API_FAIL_LIMIT = 3
_API_COOLDOWN = 120.0


def _api_disabled_until() -> float:
    """Возвращает время, до которого api-v2 отключён (0 — работает)."""
    return _api_disabled_until_ts if time.monotonic() < _api_disabled_until_ts else 0.0


def _api_health_ok():
    global _api_fail_count, _api_disabled_until_ts
    _api_fail_count = 0
    _api_disabled_until_ts = 0.0


def _api_health_fail():
    global _api_fail_count, _api_disabled_until_ts
    _api_fail_count += 1
    if _api_fail_count >= _API_FAIL_LIMIT:
        _api_disabled_until_ts = time.monotonic() + _API_COOLDOWN
        _log(f"api-v2 отключён на {_API_COOLDOWN:.0f} с после {_api_fail_count} сбоев",
             important=True)


class MusicClient:
    LIMIT_ON_PAGE = 7
    MIN_DURATION = 35

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._own_session = False

    async def __aenter__(self):
        connector = aiohttp.TCPConnector(
            family=socket.AF_INET, ttl_dns_cache=300, keepalive_timeout=60, limit=50)
        self._session = aiohttp.ClientSession(connector=connector)
        self._own_session = True
        return self

    async def __aexit__(self, *args):
        if self._own_session and self._session:
            await self._session.close()
            self._session = None

    @staticmethod
    def _ydl_opts(quiet: bool = True) -> dict:
        return {
            "quiet": quiet,
            "no_warnings": False,       # предупреждения важны: видно проблемы YouTube
            "ignoreerrors": True,
            "logger": _DiagLogger(quiet),
            "socket_timeout": 15,
            "retries": 3,
            "skip_download": True,
            "lazy_extractors": True,
            "nocheckcertificate": True,
            "source_address": "0.0.0.0",
            **ytdl_env_opts(),
        }

    @staticmethod
    def _extract_cover(entry: dict) -> str:
        from utils.kbs import DEFAULT_COVER
        thumbs = entry.get("thumbnails") or []
        if thumbs:
            best = sorted([t for t in thumbs if t.get("url")], key=lambda t: t.get("width") or 0)
            if best:
                return best[-1]["url"]
        return entry.get("thumbnail") or DEFAULT_COVER

    @staticmethod
    def _is_garbage(entry: dict, min_dur: int = 35) -> bool:
        dur = float(entry.get("duration") or 0)
        title = (entry.get("title") or "").strip().lower()
        if dur and (dur < min_dur or abs(dur - 30.0) < 0.9):
            return True
        if not title or title in ("", "untitled", "[deleted]", "[private]", "deleted"):
            return True
        if "preview" in title or "snippet" in title:
            return True
        return False

    async def _sc_search_entries(self, query: str, total_fetch: int) -> list:
        """Поиск в SoundCloud двумя независимыми способами.

        1) api-v2 напрямую — быстрее и сразу отдаёт policy/media;
        2) yt-dlp scsearch — запасной вариант (свой client_id и обновление).
        Если оба не дали результата — пишем причину в лог, чтобы сбой был виден.
        """
        loop = asyncio.get_event_loop()
        errors = []

        # --- способ 1: api-v2 ---
        if not _api_disabled_until():
            try:
                entries = await search_via_api(query, limit=total_fetch)
            except Exception as e:
                entries = []
                errors.append(f"api-v2: {type(e).__name__}: {e}")
            if entries:
                _log(f"поиск «{query[:30]}»: api-v2 дал {len(entries)} записей")
                _api_health_ok()
                return entries
            _api_health_fail()
        else:
            errors.append("api-v2: временно отключён после серии сбоев")

        # --- способ 2: yt-dlp scsearch ---
        sc_opts = {**self._ydl_opts(), "extract_flat": True}
        sc_url = f"scsearch{total_fetch}:{query}"

        def _sc_search():
            try:
                with yt_dlp.YoutubeDL(sc_opts) as ydl:
                    info = ydl.extract_info(sc_url, download=False)
                    return (info.get("entries") or []) if info else []
            except Exception as e:
                _log(f"scsearch не удался: {type(e).__name__}: {e}")
                return []

        entries = await loop.run_in_executor(None, _sc_search)
        entries = [e for e in (entries or []) if e]
        if entries:
            _log(f"поиск «{query[:30]}»: yt-dlp дал {len(entries)} записей")
        else:
            _log(f"❌ SoundCloud не дал результатов по «{query[:40]}». "
                 f"Причины: {'; '.join(errors) or 'scsearch вернул пусто'}", important=True)
        return entries

    async def search_track(self, query: str, page: int = 1, limit: int = None) -> dict:
        cache_key = f"{query.lower().strip()}_{page}_{limit}"
        now = time.monotonic()
        if cache_key in _search_cache:
            res, exp = _search_cache[cache_key]
            if now < exp:
                return res

        from database import settings_db
        per_page = limit or int(await settings_db.get("SEARCH_LIMIT") or self.LIMIT_ON_PAGE)

        # Забираем с запасом: премиум/сниппеты отсеиваются уже после запроса.
        total_fetch = min(200, max(40, page * per_page * 3))
        raw_entries = await self._sc_search_entries(query, total_fetch)

        # Предфильтр: мусор, реклама и 30-секундные сниппеты
        valid_sc_entries = []
        for e in (raw_entries or []):
            if not e:
                continue
            if self._is_garbage(e, self.MIN_DURATION):
                continue
            valid_sc_entries.append(e)

        # 2. Убираем премиум-треки SoundCloud Go+ (policy=SNIP, DRM, блокировки)
        valid_sc_entries = await self._drop_premium(valid_sc_entries)

        # Постраничная нарезка уже очищенного списка
        page_start = (page - 1) * per_page
        page_slice = valid_sc_entries[page_start : page_start + per_page]

        items = []
        for e in page_slice:
            track_id = _clean_track_id(e.get("id") or "")
            title = (e.get("title") or "Unknown").strip()
            artist = (e.get("uploader") or e.get("channel") or "SoundCloud").strip()
            duration_s = e.get("duration") or 0
            cover = self._extract_cover(e)
            sc_page_url = e.get("webpage_url") or e.get("url") or ""

            items.append({
                "id": track_id,
                "fileId": track_id,
                "title": title,
                "artist": artist,
                "duration": int(duration_s),
                "imageInfo": {"imageUrl": cover},
                "url": sc_page_url,
                "query_str": f"{artist} - {title}",
                "source": "sc",
            })

        # Если после очистки SoundCloud дал мало результатов — добираем из YouTube.
        if len(items) < per_page:
            loop = asyncio.get_event_loop()
            needed = per_page - len(items)
            yt_opts = {**self._ydl_opts(), "extract_flat": True}
            yt_url = f"ytsearch{needed + 3}:{query}"

            def _yt_search():
                try:
                    with yt_dlp.YoutubeDL(yt_opts) as ydl:
                        info = ydl.extract_info(yt_url, download=False)
                        return (info.get("entries") or []) if info else []
                except Exception:
                    return []

            yt_entries = await loop.run_in_executor(None, _yt_search)
            for e in yt_entries:
                if not e or len(items) >= per_page:
                    break
                yt_id = e.get("id")
                if not yt_id or any(it["id"] == f"yt_{yt_id}" for it in items):
                    continue
                dur = float(e.get("duration") or 0)
                if dur < 30 or dur > 1800:
                    continue
                title = (e.get("title") or "Unknown").strip()
                artist = (e.get("uploader") or e.get("channel") or "YouTube Music").strip()
                items.append({
                    "id": f"yt_{yt_id}",
                    "fileId": f"yt_{yt_id}",
                    "title": title,
                    "artist": artist,
                    "duration": int(dur),
                    "imageInfo": {"imageUrl": self._extract_cover(e)},
                    "url": f"https://www.youtube.com/watch?v={yt_id}",
                    "query_str": f"{artist} - {title}",
                    "source": "yt",
                })

        pages_all = page + 1 if len(items) >= per_page else page
        res = {"items": items, "paginationInfo": {"lastPage": pages_all}}
        _search_cache[cache_key] = (res, now + _cache_ttl)
        return res

    async def _drop_premium(self, entries: list) -> list:
        """Убирает премиум-треки SoundCloud Go+ (30-сек сниппеты, DRM, блокировки).

        Если запись пришла из api-v2, состояние (policy/media) уже внутри и
        дополнительный запрос не нужен. Для записей yt-dlp состояния тянутся
        одним батч-запросом и кэшируются.
        """
        if not entries:
            return entries
        ids = [_clean_track_id(e.get("id") or "") for e in entries]
        states = {_clean_track_id(e.get("id") or ""): e.get("_api_state")
                  for e in entries if e.get("_api_state")}
        missing = [i for i in ids if i and i not in states]
        if missing:
            states.update(await get_track_states(missing))
        if not states:
            return entries
        kept = []
        for entry, track_id in zip(entries, ids):
            state = states.get(track_id)
            if is_premium_track(state):
                continue
            full_duration = (state or {}).get("full_duration")
            if full_duration:
                entry["duration"] = full_duration / 1000
            kept.append(entry)
        return kept

    async def top_tracks(self, page: int = 1) -> dict:
        return await self.search_track("popular music", page=page)

    # ─────────── вспомогательные методы получения трека ───────────

    @staticmethod
    def _extract_sync(opts: dict, url: str) -> Optional[dict]:
        """Синхронный вызов yt-dlp (выполняется в executor-е)."""
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)
        except Exception as e:
            _log(f"yt-dlp {url[:70]}: {type(e).__name__}: {e}")
            return None

    @staticmethod
    def _yt_result(track_id: str, info: dict, fmt: dict, url: str) -> dict:
        """Собирает единый вид «трека» из видео + выбранного аудио-формата."""
        return {
            "id": track_id,
            "fileId": track_id,
            "title": info.get("title") or "Unknown",
            "artist": info.get("uploader") or info.get("channel") or "YouTube",
            "duration": int(float(info.get("duration") or 0)),
            "download": fmt.get("url"),
            # размер нужен, чтобы прогресс-бар показывал процент, а не только МБ
            "filesize": int(fmt.get("filesize") or fmt.get("filesize_approx") or 0),
            "abr": fmt.get("abr") or fmt.get("tbr"),
            "format_id": fmt.get("format_id"),
            # HLS/фрагменты нельзя качать обычным потоком — их докачает yt-dlp
            "direct": str(fmt.get("protocol") or "").startswith("http"),
            "page": url,
            "imageInfo": {"imageUrl": MusicClient._extract_cover(info)},
            "url": url,
        }

    async def _stored_meta(self, track_id: str) -> dict:
        """Метаданные из БД — единственный источник, когда SoundCloud недоступен."""
        try:
            from database import music_db
            return await music_db.get_request_by_id(track_id) or {}
        except Exception:
            return {}

    async def _find_youtube(self, query: str, duration_hint: int = 0) -> Optional[dict]:
        """Ищет лучший вариант на YouTube: быстрый flat-поиск + оценка кандидатов.

        Раньше брался первый попавшийся ролик — попадались slowed/версии-каверы
        с другим хронометражем. Теперь выбираем кандидата по совпадению длительности
        и слов, штрафуя «slowed/remix/cover» и т.п.
        """
        if not query:
            return None
        loop = asyncio.get_event_loop()
        flat_opts = {**self._ydl_opts(), "extract_flat": True}
        flat = await loop.run_in_executor(
            None, self._extract_sync, flat_opts, f"ytsearch5:{query}")
        entries = [e for e in ((flat or {}).get("entries") or []) if e]
        if not entries:
            return None

        best = max(entries, key=lambda e: score_candidate(query, e, duration_hint))
        video_id = best.get("id")
        if not video_id:
            return None
        info = await loop.run_in_executor(
            None, self._extract_sync, self._ydl_opts(),
            f"https://www.youtube.com/watch?v={video_id}")
        return info or None

    async def get_track_info(self, track_id: str) -> Optional[dict]:
        track_id = _clean_track_id(track_id)
        loop = asyncio.get_event_loop()

        # ── Ветка YouTube: извлекаем и выбираем ЛУЧШИЙ аудио-формат ──
        if track_id.startswith("yt_"):
            real_id = track_id[3:]
            yt_url = f"https://www.youtube.com/watch?v={real_id}"
            info = await loop.run_in_executor(
                None, self._extract_sync, self._ydl_opts(), yt_url)
            if not info:
                return None
            fmt = _pick_best_audio(info.get("formats"))
            if not fmt:
                _log(f"yt_{real_id}: пригодных аудио-форматов нет "
                     f"({len(info.get('formats') or [])} всего) — "
                     f"вероятно, нет JS-рантайма deno", important=True)
                return None
            return self._yt_result(track_id, info, fmt, yt_url)

        # ── Ветка SoundCloud ──
        sc_url = f"https://api.soundcloud.com/tracks/{track_id}"
        info = await loop.run_in_executor(
            None, self._extract_sync, self._ydl_opts(), sc_url)

        fmt = _pick_best_audio((info or {}).get("formats"))
        if fmt:
            return {
                "id": track_id,
                "fileId": track_id,
                "title": info.get("title") or "Unknown",
                "artist": info.get("uploader") or info.get("channel") or "SoundCloud",
                "duration": int(float(info.get("duration") or 0)),
                "download": fmt.get("url"),
                "filesize": int(fmt.get("filesize") or fmt.get("filesize_approx") or 0),
                "abr": fmt.get("abr") or fmt.get("tbr"),
                "format_id": fmt.get("format_id"),
                # у SoundCloud почти всегда только HLS — качать будет yt-dlp
                "direct": str(fmt.get("protocol") or "").startswith("http"),
                "page": sc_url,
                "imageInfo": {"imageUrl": self._extract_cover(info)},
                "url": info.get("webpage_url") or sc_url,
            }

        # Ни одной полноценной дорожки: DRM/SoundCloud Go+ либо SoundCloud недоступен.
        # Прежний код здесь падал с NameError (`req_meta`), из-за чего гибли ВСЕ треки.
        meta = await self._stored_meta(track_id)
        query = track_display_name(
            (info or {}).get("uploader") or meta.get("artist") or meta.get("name"),
            (info or {}).get("title") or meta.get("title"))
        duration_hint = int(float((info or {}).get("duration") or meta.get("duration") or 0))
        if not query:
            _log(f"SC {track_id}: нет метаданных (SC недоступен, в БД тоже пусто) — "
                 f"переход на YouTube невозможен", important=True)
            return None

        _log(f"SC {track_id}: полноразмерной дорожки нет — ищу «{query}» на YouTube")
        yt_info = await self._find_youtube(query, duration_hint)
        if not yt_info:
            _log(f"SC {track_id}: на YouTube ничего не найдено по «{query}»",
                 important=True)
            return None
        yt_fmt = _pick_best_audio(yt_info.get("formats"))
        if not yt_fmt:
            return None
        result = self._yt_result(track_id, yt_info, yt_fmt,
                                 f"https://www.youtube.com/watch?v={yt_info.get('id')}")
        if duration_hint:
            result["duration"] = result["duration"] or duration_hint
        return result

    async def _download_with_ytdlp(self, info: dict, progress_cb=None) -> io.BytesIO:
        """Скачивает трек средствами yt-dlp: HLS-фрагменты, PO-токены, заголовки.

        Нужен для дорожек SoundCloud (там почти всегда только HLS) и как
        запасной путь, когда прямой http-поток упирается в 403.
        """
        import shutil
        import tempfile

        tmpdir = tempfile.mkdtemp(prefix="nam_audio_")
        state = {"done": 0, "total": int(info.get("filesize") or 0)}

        def _hook(status: dict):
            if status.get("status") == "downloading":
                state["done"] = int(status.get("downloaded_bytes") or 0)
                state["total"] = int(status.get("total_bytes")
                                     or status.get("total_bytes_estimate")
                                     or state["total"])
            elif status.get("status") == "finished":
                state["done"] = state["total"] or state["done"]

        async def _poll():
            while True:
                if progress_cb:
                    try:
                        await progress_cb(state["done"], state["total"])
                    except Exception:
                        pass
                await asyncio.sleep(0.4)

        page = info.get("page") or info.get("url") or ""
        fmt_id = info.get("format_id") or ""
        attempts = ([f"{fmt_id}/bestaudio/best"] if fmt_id else []) + ["bestaudio/best"]

        loop = asyncio.get_event_loop()
        last_error = None
        try:
            for fmt in attempts:
                # ignoreerrors=False: иначе yt-dlp молча вернёт ошибку и мы её не увидим.
                # skip_download=False обязателен: в _ydl_opts он включён ради извлечения.
                opts = {**self._ydl_opts(), "format": fmt, "noprogress": True,
                        "ignoreerrors": False, "skip_download": False,
                        "progress_hooks": [_hook],
                        "outtmpl": os.path.join(tmpdir, "audio.%(ext)s")}

                def _run():
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        return ydl.download([page])

                poller = loop.create_task(_poll())
                try:
                    code = await loop.run_in_executor(None, _run)
                except Exception as e:
                    last_error = e
                    continue
                finally:
                    poller.cancel()
                    try:
                        await poller
                    except asyncio.CancelledError:
                        pass
                if code != 0:
                    last_error = f"yt-dlp вернул код {code}"
                    continue
                # yt-dlp может оставить файл с расширением .part — берём самый
                # большой из скачанных и проверяем, что он не обрезан.
                candidates = []
                for name in os.listdir(tmpdir):
                    path = os.path.join(tmpdir, name)
                    try:
                        candidates.append((os.path.getsize(path), path))
                    except OSError:
                        continue
                if not candidates:
                    last_error = f"yt-dlp ничего не записал (код {code})"
                    continue
                size, path = max(candidates)
                expected = int(info.get("filesize") or 0)
                if expected and size < expected * 0.8:
                    last_error = f"скачано {size} из {expected} байт — обрезано"
                    _log(f"yt-dlp: {last_error}", important=True)
                    continue
                with open(path, "rb") as fh:
                    data = fh.read()
                if data:
                    _log(f"yt-dlp скачал {len(data) / 1_048_576:.1f} МБ "
                         f"(format {fmt}, битрейт {info.get('abr')} kbps)")
                    return io.BytesIO(data)
                last_error = "yt-dlp записал пустой файл"
            raise TrackUnavailable(f"yt-dlp не смог скачать: {last_error}")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    async def download_track(self, track_id: str, progress_cb: Optional[Callable] = None) -> io.BytesIO:
        info = await self.get_track_info(track_id)
        if not info or not info.get("download"):
            raise ValueError("DRM_OR_NOT_FOUND")

        if not info.get("direct", True):
            # HLS/фрагменты: обычный поток их не умеет — докачивает сам yt-dlp
            return await self._download_with_ytdlp(info, progress_cb)
        try:
            return await self._stream_to_buffer(
                info["download"],
                progress_cb=progress_cb,
                total_hint=int(info.get("filesize") or 0),
                label=str(info.get("title") or track_id)[:60],
            )
        except TrackUnavailable as e:
            # прямой поток не удался (обычно 403 из-за n-sig) — пробуем через yt-dlp
            _log(f"прямое скачивание не вышло ({e}) — пробую через yt-dlp")
            return await self._download_with_ytdlp(info, progress_cb)


    @staticmethod
    async def _fetch_range(session, url, headers, start, end, timeout, attempts=None):
        """Один Range-запрос с повторами. Возвращает (status, байты, total из Content-Range)."""
        attempts = DL_RETRIES if attempts is None else attempts
        last_status = None
        for attempt in range(max(1, attempts)):
            try:
                async with session.get(
                        url, headers={**headers, "Range": f"bytes={start}-{end}"},
                        timeout=timeout) as resp:
                    last_status = resp.status
                    _, _, cr_total = parse_content_range(resp.headers.get("Content-Range"))
                    if resp.status in (200, 206):
                        buf = bytearray()
                        async for chunk in resp.content.iter_chunked(262144):  # 256 KB для пропускной способности
                            buf += chunk
                        return resp.status, bytes(buf), cr_total
                    if resp.status in (401, 403):
                        # у googlevideo это чаще всего отсутствие расшифровки n-sig
                        return resp.status, b"", 0
            except Exception as e:
                last_status = type(e).__name__
            await asyncio.sleep(0.4 * (attempt + 1))
        return last_status, b"", 0

    @staticmethod
    async def _fetch_stream(session, url, headers, timeout, progress_cb=None):
        """Обычная последовательная загрузка — путь на случай, если Range не работает."""
        async with session.get(url, headers=headers, timeout=timeout) as resp:
            if resp.status in (401, 403):
                raise TrackUnavailable(f"сервер отклонил запрос (HTTP {resp.status})")
            resp.raise_for_status()
            total = int(resp.headers.get("Content-Length") or 0)
            buf = bytearray()
            async for chunk in resp.content.iter_chunked(262144):  # 256 KB для пропускной способности
                buf += chunk
                if progress_cb:
                    try:
                        await progress_cb(len(buf), total)
                    except Exception:
                        pass
            return bytes(buf), total

    async def _download_bytes(self, session, url, progress_cb, timeout_total, total_hint):
        """Возвращает байты файла: Range-чанки, а при неудаче — обычный поток.

        Googlevideo быстро отдаёт первый мегабайт, а последующие запросы может
        залить 403 — поэтому: ограниченная параллельность, повторы и обязательная
        проверка, что пришли все байты (иначе в Telegram уедет битый файл).
        Общий размер берём из Content-Range, а не предположением yt-dlp.
        """
        timeout = ClientTimeout(total=timeout_total, connect=15)
        headers = {"User-Agent": _BROWSER_UA}
        status, head, total = await self._fetch_range(
            session, url, headers, 0, max(DL_CHUNK - 1, 1), timeout)
        total = int(total or total_hint or 0)

        async def report(done: int):
            if progress_cb:
                try:
                    await progress_cb(done, int(total or 0))
                except Exception:
                    pass

        if status in (200, 206):
            parts = [head]
            downloaded = len(head)
            await report(downloaded)

            if status == 206 and total and downloaded < total:
                pending = list(range(downloaded, total, DL_CHUNK))
                sem = asyncio.Semaphore(max(1, min(DL_WORKERS, len(pending))))
                results: dict = {}

                async def one(offset):
                    async with sem:
                        got_status, chunk, _ = await self._fetch_range(
                            session, url, headers, offset,
                            min(offset + DL_CHUNK - 1, total - 1), timeout)
                        results[offset] = (got_status, chunk)

                await asyncio.gather(*[one(i) for i in pending])
                failures = [i for i, (_, c) in sorted(results.items()) if not c]
                if failures:
                    _log(f"чанков не скачалось: {len(failures)} из {len(pending)} "
                         f"(HTTP {results[failures[0]][0]}) — пробую обычную загрузку")
                    return await self._plain_or_fail(
                        session, url, headers, timeout, progress_cb, total)
                for offset, (_, chunk) in sorted(results.items()):
                    parts.append(chunk)
                    downloaded += len(chunk)
                    await report(downloaded)
                data = b"".join(parts)
                if total and len(data) < total:
                    raise TrackUnavailable(f"получено {len(data)} из {total} байт — обрезан")
                return data

            if status == 206 and not total:
                # размер неизвестен — качаем чанками до короткого ответа
                start = downloaded
                while True:
                    got_status, chunk, _ = await self._fetch_range(
                        session, url, headers, start, start + DL_CHUNK - 1, timeout)
                    if not chunk:
                        raise TrackUnavailable(
                            f"обрыв на {start} байт (HTTP {got_status})")
                    parts.append(chunk)
                    downloaded += len(chunk)
                    start += len(chunk)
                    await report(downloaded)
                    if len(chunk) < DL_CHUNK:
                        break
                return b"".join(parts)

            # status == 200: сервер проигнорировал Range и уже отдал файл целиком
            return b"".join(parts)

        # Range не поддерживается (416/403/сбой) — пробуем обычную загрузку
        _log(f"Range не сработал (HTTP {status}) — пробую обычную загрузку")
        return await self._plain_or_fail(
            session, url, headers, timeout, progress_cb, int(total_hint or 0))

    async def _plain_or_fail(self, session, url, headers, timeout, progress_cb, expected):
        """Последовательная загрузка; падает, если файла получено меньше обещанного."""
        data, _ = await self._fetch_stream(session, url, headers, timeout, progress_cb)
        if expected and len(data) < expected:
            raise TrackUnavailable(f"получено {len(data)} из {expected} байт — файл обрезан")
        return data

    async def _stream_to_buffer(self, url: str, progress_cb: Optional[Callable] = None,
                                timeout_total: int = 180, total_hint: int = 0,
                                label: str = "") -> io.BytesIO:
        """Скачивает аудио в память: прогресс + проверка целостности."""
        started = time.perf_counter()
        session = self._session
        own = False
        if session is None:
            session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(family=socket.AF_INET))
            own = True
        try:
            data = await self._download_bytes(session, url, progress_cb,
                                              timeout_total, total_hint)
        finally:
            if own:
                await session.close()

        elapsed = time.perf_counter() - started
        speed = len(data) / 1024 / max(elapsed, 0.01)
        _log(f"скачано {len(data) / 1_048_576:.1f} МБ за {elapsed:.1f} с "
             f"({speed:.0f} КБ/с)" + (f" — {label}" if label else ""))

        buf = io.BytesIO(data)
        buf.seek(0)
        return buf

    async def download_with_retry(self, url: str, attempts: int = 3) -> bytes:
        if not url:
            return b""
        timeout = ClientTimeout(total=60, connect=10)
        last_err = None
        for i in range(attempts):
            try:
                async with self._session.get(url, timeout=timeout) as resp:
                    resp.raise_for_status()
                    return await resp.read()
            except Exception as e:
                last_err = e
                if i < attempts - 1:
                    await asyncio.sleep(1.5 * (i + 1))
        raise last_err

    async def download_to_buffer(self, url: str) -> io.BytesIO:
        return await self._stream_to_buffer(url)


# Обратная совместимость со старым именем класса
SoundCloudClient = MusicClient
downloader = MusicClient()
