"""
Гибридный клиент SoundCloud + YouTube на базе yt-dlp.

Поиск идёт в SoundCloud, а если трек защищён (SoundCloud Go+/DRM) —
автоматически подбирается полноценная версия на YouTube.
"""
import asyncio
import html
import io
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


class _QuietLogger:
    def debug(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


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
                               "logger": _QuietLogger()}) as ydl:
            extractor = ydl.get_info_extractor("Soundcloud")
            extractor.set_downloader(ydl)
            extractor.initialize()
            _sc_client_id = getattr(extractor, "_CLIENT_ID", None) or None
    except Exception:
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
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": True,
            "logger": _QuietLogger(),
            "socket_timeout": 10,
            "retries": 2,
            "skip_download": True,
            "lazy_extractors": True,
            "nocheckcertificate": True,
            "source_address": "0.0.0.0",
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

    async def search_track(self, query: str, page: int = 1, limit: int = None) -> dict:
        cache_key = f"{query.lower().strip()}_{page}_{limit}"
        now = time.monotonic()
        if cache_key in _search_cache:
            res, exp = _search_cache[cache_key]
            if now < exp:
                return res

        from database import settings_db
        per_page = limit or int(await settings_db.get("SEARCH_LIMIT") or self.LIMIT_ON_PAGE)

        loop = asyncio.get_event_loop()

        # 1. Поиск в SoundCloud (flat-режим: без выкачивания форматов)
        # Забираем с запасом: премиум/сниппеты отсеиваются уже после запроса.
        total_fetch = min(200, max(40, page * per_page * 3))
        sc_opts = {**self._ydl_opts(), "extract_flat": True}
        sc_url = f"scsearch{total_fetch}:{query}"

        def _sc_search():
            try:
                with yt_dlp.YoutubeDL(sc_opts) as ydl:
                    info = ydl.extract_info(sc_url, download=False)
                    return (info.get("entries") or []) if info else []
            except Exception:
                return []

        raw_entries = await loop.run_in_executor(None, _sc_search)

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

        Состояния треков берутся одним батч-запросом к api-v2 и кэшируются.
        Если API недоступен — возвращаем список как есть (остаётся фильтр по
        длительности из ``_is_garbage``).
        """
        if not entries:
            return entries
        ids = [_clean_track_id(e.get("id") or "") for e in entries]
        states = await get_track_states(ids)
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

    @staticmethod
    def _pick_best_audio_format(formats: list) -> tuple[Optional[str], Optional[dict]]:
        """Выбирает лучший аудиопоток с максимальным битрейтом (128-160+ kbps)."""
        if not formats:
            return None, None
        audio_fmts = [
            f for f in formats
            if f.get("vcodec") == "none" and f.get("url")
        ]
        if not audio_fmts:
            return None, None

        # Предпочитаем форматы m4a/mp3 с наивысшим битрейтом
        m4a_fmts = [f for f in audio_fmts if f.get("ext") in ("m4a", "mp3")]
        m4a_fmts.sort(key=lambda f: f.get("abr") or 0, reverse=True)
        if m4a_fmts:
            chosen = m4a_fmts[0]
            return chosen.get("url"), chosen.get("http_headers")

        # Fallback: любой аудиоформат с максимальным битрейтом
        audio_fmts.sort(key=lambda f: f.get("abr") or 0, reverse=True)
        chosen = audio_fmts[0]
        return chosen.get("url"), chosen.get("http_headers")

    async def get_track_info(self, track_id: str) -> Optional[dict]:
        track_id = _clean_track_id(track_id)
        loop = asyncio.get_event_loop()

        req_meta = None
        try:
            from database import music_db
            req_meta = await music_db.get_request_by_id(track_id)
            if not req_meta:
                req_meta = await music_db.search_track_exists(track_id)
        except Exception:
            req_meta = None

        # Ветка YouTube
        if track_id.startswith("yt_"):
            real_id = track_id[3:]
            yt_url = f"https://www.youtube.com/watch?v={real_id}"
            ydl_opts = {**self._ydl_opts(), "format": "ba/b[ext=m4a]/bestaudio"}

            def _extract_yt():
                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        return ydl.extract_info(yt_url, download=False)
                except Exception:
                    return None

            info = await loop.run_in_executor(None, _extract_yt)
            if not info:
                return None

            dl_url, dl_headers = self._pick_best_audio_format(info.get("formats") or [])
            return {
                "id": track_id,
                "fileId": track_id,
                "title": info.get("title") or (req_meta.get("title") if req_meta else "Unknown"),
                "artist": info.get("uploader") or info.get("channel") or (req_meta.get("artist") if req_meta else "YouTube"),
                "duration": int(info.get("duration") or (req_meta.get("duration") if req_meta else 0)),
                "download": dl_url,
                "headers": dl_headers,
                "imageInfo": {"imageUrl": self._extract_cover(info)},
                "url": yt_url,
            }

        sc_url = (req_meta.get("url") if req_meta else None) or f"https://api.soundcloud.com/tracks/{track_id}"
        ydl_opts = {**self._ydl_opts(), "format": "http_mp3_0_0/bestaudio[protocol=http]/bestaudio[ext=mp3]/bestaudio/best"}

        def _extract_sc():
            urls = [sc_url]
            fallback_api = f"https://api.soundcloud.com/tracks/{track_id}"
            if fallback_api not in urls:
                urls.append(fallback_api)
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                for u in urls:
                    try:
                        res = ydl.extract_info(u, download=False)
                        if res:
                            return res
                    except Exception:
                        continue
            return None

        info = await loop.run_in_executor(None, _extract_sc)

        formats = (info.get("formats") or []) if info else []
        is_drm_or_preview = (
            (not info) or
            (not formats) or
            all("preview" in f.get("format_id", "").lower() for f in formats)
        )

        if is_drm_or_preview:
            # Обход DRM SoundCloud Go+: подбираем полную версию на YouTube в высоком качестве
            title_hint = (info.get("title") if info else None) or (req_meta.get("title") if req_meta else "") or ""
            uploader_hint = (info.get("uploader") if info else None) or (req_meta.get("artist") if req_meta else "") or ""
            fallback_query = f"{uploader_hint} {title_hint}".strip()

            if fallback_query:
                yt_opts = {**self._ydl_opts(), "format": "ba/b[ext=m4a]/bestaudio"}
                def _yt_direct_find():
                    try:
                        with yt_dlp.YoutubeDL(yt_opts) as ydl:
                            res = ydl.extract_info(f"ytsearch1:{fallback_query}", download=False)
                            entries = res.get("entries") or []
                            return entries[0] if entries else None
                    except Exception:
                        return None

                yt_entry = await loop.run_in_executor(None, _yt_direct_find)
                if yt_entry:
                    dl_url, dl_headers = self._pick_best_audio_format(yt_entry.get("formats") or [])
                    return {
                        "id": track_id,
                        "fileId": track_id,
                        "title": yt_entry.get("title") or title_hint or "Unknown",
                        "artist": yt_entry.get("uploader") or uploader_hint or "YouTube",
                        "duration": int(yt_entry.get("duration") or (info.get("duration") if info else 0)),
                        "download": dl_url,
                        "headers": dl_headers,
                        "imageInfo": {"imageUrl": self._extract_cover(yt_entry)},
                        "url": f"https://www.youtube.com/watch?v={yt_entry.get('id')}",
                    }
            return None

        cover = self._extract_cover(info)
        dl_url = None
        valid_formats = [f for f in formats if "preview" not in f.get("format_id", "").lower()]

        for fmt in (valid_formats or formats):
            if fmt.get("protocol") == "http" and fmt.get("ext") in ("mp3", "m4a", None):
                dl_url = fmt.get("url")
                break
        if not dl_url and valid_formats:
            dl_url = valid_formats[-1].get("url")

        return {
            "id": track_id,
            "fileId": track_id,
            "title": info.get("title") or (req_meta.get("title") if req_meta else "Unknown"),
            "artist": info.get("uploader") or info.get("channel") or (req_meta.get("artist") if req_meta else "SoundCloud"),
            "duration": int(info.get("duration") or (req_meta.get("duration") if req_meta else 0)),
            "download": dl_url,
            "headers": None,
            "imageInfo": {"imageUrl": cover},
            "url": info.get("webpage_url") or sc_url,
        }

    async def download_track(self, track_id: str, progress_cb: Optional[Callable] = None) -> io.BytesIO:
        info = await self.get_track_info(track_id)
        if not info:
            raise ValueError("DRM_OR_NOT_FOUND")

        dl_url = info.get("download")
        page_url = info.get("url") or ""
        is_youtube = ("youtube.com" in page_url) or ("youtu.be" in page_url) or str(track_id).startswith("yt_")

        # 1. Если это обычный SoundCloud трек с прямым MP3 CDN URL — быстро стримим напрямую
        if dl_url and not is_youtube:
            try:
                headers = info.get("headers")
                return await self._stream_to_buffer(dl_url, progress_cb=progress_cb, headers=headers)
            except Exception:
                pass

        # 2. Для YouTube, DRM обходов и сложных треков — нативная загрузка yt-dlp + ffmpeg 192k MP3
        target_url = page_url or dl_url
        if not target_url:
            raise ValueError("DRM_OR_NOT_FOUND")

        try:
            return await self._download_file_via_ytdlp(target_url, progress_cb=progress_cb)
        except Exception:
            # Fallback к стримингу если yt-dlp на сервере не смог локально скачать
            if dl_url:
                return await self._stream_to_buffer(dl_url, progress_cb=progress_cb, headers=info.get("headers"))
            raise

    async def _download_file_via_ytdlp(self, url: str, progress_cb: Optional[Callable] = None) -> io.BytesIO:
        """Скачивает аудиофайл через встроенный движок yt-dlp и конвертирует в MP3 192k."""
        loop = asyncio.get_event_loop()
        buf = io.BytesIO()

        def _worker():
            import tempfile, os, shutil
            tmp_dir = tempfile.mkdtemp(prefix="notamusic_")
            try:
                outtmpl = os.path.join(tmp_dir, "%(id)s.%(ext)s")

                def _hook(d):
                    if not progress_cb:
                        return
                    if d.get("status") == "downloading":
                        dl = d.get("downloaded_bytes") or 0
                        tot = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                        asyncio.run_coroutine_threadsafe(progress_cb(dl, tot), loop)

                opts = {
                    "format": "ba[ext=m4a]/ba[ext=mp3]/ba/bestaudio/best",
                    "outtmpl": outtmpl,
                    "quiet": True,
                    "no_warnings": True,
                    "noplaylist": True,
                    "logger": _QuietLogger(),
                    "progress_hooks": [_hook],
                    "socket_timeout": 30,
                    "retries": 3,
                }
                if shutil.which("ffmpeg"):
                    opts["postprocessors"] = [{
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    }]

                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.extract_info(url, download=True)
                    all_files = [
                        os.path.join(tmp_dir, f) for f in os.listdir(tmp_dir)
                        if not f.endswith(".part") and not f.endswith(".ytdl")
                    ]
                    if not all_files:
                        raise ValueError("NO_AUDIO_FILE_DOWNLOADED")

                    chosen = all_files[0]
                    for f in all_files:
                        if f.endswith(".mp3") or f.endswith(".m4a"):
                            chosen = f
                            break

                    with open(chosen, "rb") as af:
                        return af.read()
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)

        data = await loop.run_in_executor(None, _worker)
        buf.write(data)
        buf.seek(0)
        return buf

    async def _stream_to_buffer(self, url: str, progress_cb: Optional[Callable] = None,
                                headers: Optional[dict] = None,
                                timeout_total: int = 180) -> io.BytesIO:
        timeout = ClientTimeout(total=timeout_total, connect=15)
        req_headers = {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/124.0.0.0 Safari/537.36"),
            "Accept": "*/*",
            "Accept-Encoding": "identity",
        }
        if headers and isinstance(headers, dict):
            req_headers.update(headers)

        session = self._session
        own = False
        if session is None:
            session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(family=socket.AF_INET, enable_cleanup_closed=True),
                read_bufsize=262144
            )
            own = True
        buf = io.BytesIO()
        try:
            async with session.get(url, timeout=timeout, headers=req_headers) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("Content-Length") or 0)
                downloaded = 0
                async for chunk in resp.content.iter_chunked(262144):  # 256 KB chunk for high throughput
                    buf.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb:
                        await progress_cb(downloaded, total)
        finally:
            if own:
                await session.close()
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
