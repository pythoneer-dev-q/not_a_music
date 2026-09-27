"""
SoundCloud + YouTube Hybrid Client via yt-dlp.
??????????? ????? ? ?????????? ? SoundCloud ? ?????????????? fallback ?? YouTube.
"""
import asyncio
import io
import socket
import time
from typing import Optional, Callable

import aiohttp
from aiohttp import ClientTimeout
import yt_dlp


class _QuietLogger:
    def debug(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def format_progress_bar(downloaded: int, total: int, width: int = 10) -> str:
    if total <= 0:
        return f"? ????????? {downloaded / 1_048_576:.1f} MB"
    pct = downloaded / total
    bar = "?" * int(width * pct) + "?" * (width - int(width * pct))
    return f"[{bar}] {int(pct * 100)}%  {downloaded / 1_048_576:.1f}/{total / 1_048_576:.1f} MB"


class TelegramProgressReporter:
    UPDATE_INTERVAL = 2.0

    def __init__(self, edit_func: Callable, prefix: str = "? ???????? ?????\n"):
        self._edit = edit_func
        self._prefix = prefix
        self._last_update = 0.0

    async def update(self, downloaded: int, total: int, force: bool = False):
        now = time.monotonic()
        if not force and (now - self._last_update) < self.UPDATE_INTERVAL:
            return
        self._last_update = now
        try:
            await self._edit(f"{self._prefix}{format_progress_bar(downloaded, total)}")
        except Exception:
            pass


def _clean_track_id(raw_id: str) -> str:
    raw = str(raw_id).strip()
    if raw.startswith("soundcloud:tracks:"):
        return raw.split(":")[-1]
    return raw


_search_cache: dict = {}
_cache_ttl = 180


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
        fetch = per_page * 3
        start = (page - 1) * per_page + 1
        end = start + fetch - 1

        loop = asyncio.get_event_loop()

        # 1. ??????? ??????? SoundCloud
        sc_opts = {**self._ydl_opts(), "extract_flat": True, "playlist_items": f"{start}-{end}"}
        # ??????????? ???? ? ???????, ????? ?????????????? ????????? 30-????????? ??????
        total_fetch = max(25, (page * per_page) + 15)
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
        
        # ??????? ??????????: ??????????? ??? ????? ?????? 35 ?????? ? ????? ??????
        valid_sc_entries = []
        for e in (raw_entries or []):
            if not e:
                continue
            if self._is_garbage(e, self.MIN_DURATION):
                continue
            valid_sc_entries.append(e)

        # ????????? ?? ???????? ??????
        page_start = (page - 1) * per_page
        page_slice = valid_sc_entries[page_start : page_start + per_page]

        items = []
        for e in page_slice:
            track_id = _clean_track_id(e.get("id") or "")
            title = (e.get("title") or "Unknown").strip()
            artist = (e.get("uploader") or e.get("channel") or "SoundCloud").strip()
            duration_s = e.get("duration") or 0
            cover = self._extract_cover(e)
            sc_page_url = e.get("url") or e.get("webpage_url") or ""

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

# 2. ???? SoundCloud ?????? ????/????? (??? ????????? ???? ?? IP) ? ?????????? YouTube
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

    async def top_tracks(self, page: int = 1) -> dict:
        return await self.search_track("popular music", page=page)

    async def get_track_info(self, track_id: str) -> Optional[dict]:
        track_id = _clean_track_id(track_id)
        loop = asyncio.get_event_loop()

        # YouTube ????
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

            dl_url = None
            audio_fmts = [f for f in (info.get("formats") or []) if f.get("vcodec") == "none" and f.get("url")]
            for fmt in audio_fmts:
                if fmt.get("ext") in ("m4a", "mp3"):
                    dl_url = fmt.get("url")
                    break
            if not dl_url and audio_fmts:
                dl_url = audio_fmts[0].get("url")

            return {
                "id": track_id,
                "fileId": track_id,
                "title": info.get("title") or "Unknown",
                "artist": info.get("uploader") or info.get("channel") or "YouTube",
                "duration": int(info.get("duration") or 0),
                "download": dl_url,
                "imageInfo": {"imageUrl": self._extract_cover(info)},
                "url": yt_url,
            }

        sc_url = f"https://api.soundcloud.com/tracks/{track_id}"
        ydl_opts = {**self._ydl_opts(), "format": "http_mp3_0_0/bestaudio[protocol=http]/bestaudio[ext=mp3]/bestaudio/best"}

        def _extract_sc():
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    return ydl.extract_info(sc_url, download=False)
            except Exception:
                return None

        info = await loop.run_in_executor(None, _extract_sc)

        # ???? SoundCloud ?? ????? ?????? ???? (DRM/????) ? ???? ???????????? ?? YouTube
        formats = (info.get("formats") or []) if info else []
        is_drm_or_preview = (not info) or (not formats) or all("preview" in f.get("format_id", "").lower() for f in formats)

        if is_drm_or_preview:
            # ??????? DRM SoundCloud Go+: ????????? ???????? ?????????? ?? YouTube
            title_hint = (info.get("title") if info else None) or ""
            uploader_hint = (info.get("uploader") if info else None) or ""
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
                    audio_fmts = [
                        f for f in (yt_entry.get("formats") or [])
                        if f.get("vcodec") == "none" and f.get("url") and f.get("ext") in ("m4a", "webm", "mp3")
                    ]
                    dl_url = None
                    for fmt in audio_fmts:
                        if fmt.get("ext") in ("m4a", "mp3"):
                            dl_url = fmt.get("url")
                            break
                    if not dl_url and audio_fmts:
                        dl_url = audio_fmts[0].get("url")

                    if dl_url:
                        return {
                            "id": track_id,
                            "fileId": track_id,
                            "title": yt_entry.get("title") or title_hint or "Unknown",
                            "artist": yt_entry.get("uploader") or uploader_hint or "YouTube",
                            "duration": int(yt_entry.get("duration") or info.get("duration") or 0),
                            "download": dl_url,
                            "imageInfo": {"imageUrl": self._extract_cover(yt_entry)},
                            "url": f"https://www.youtube.com/watch?v={yt_entry.get('id')}",
                        }
            return None
        cover = self._extract_cover(info)
        dl_url = None
        formats = info.get("formats") or []
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
            "title": info.get("title") or "Unknown",
            "artist": info.get("uploader") or info.get("channel") or "SoundCloud",
            "duration": int(info.get("duration") or 0),
            "download": dl_url,
            "imageInfo": {"imageUrl": cover},
            "url": info.get("webpage_url") or sc_url,
        }

    async def download_track(self, track_id: str, progress_cb: Optional[Callable] = None) -> io.BytesIO:
        info = await self.get_track_info(track_id)
        if not info or not info.get("download"):
            raise ValueError("DRM_OR_NOT_FOUND")
        return await self._stream_to_buffer(info["download"], progress_cb=progress_cb)

    async def _stream_to_buffer(self, url: str, progress_cb: Optional[Callable] = None,
                                timeout_total: int = 120) -> io.BytesIO:
        timeout = ClientTimeout(total=timeout_total, connect=10)
        headers = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                                  "Chrome/124.0.0.0 Safari/537.36")}
        session = self._session
        own = False
        if session is None:
            session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(family=socket.AF_INET))
            own = True
        buf = io.BytesIO()
        try:
            async with session.get(url, timeout=timeout, headers=headers) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("Content-Length") or 0)
                downloaded = 0
                async for chunk in resp.content.iter_chunked(65536):
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


# ????????????? ?? ??????? ?????????
SoundCloudClient = MusicClient
downloader = MusicClient()
