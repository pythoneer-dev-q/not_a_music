import os
from dataclasses import dataclass
from dotenv import load_dotenv
load_dotenv()


def _int(name: str, default: int) -> int:
    """Безопасный парсинг int из .env (env всегда возвращает строки)."""
    try:
        return int(os.getenv(name) or default)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True) 
class Config:
    tg_token: str = os.getenv("BOT_TOKEN")
    MONGO_URI: str = os.getenv("MONGO_URI", "mongodb://127.0.0.1:27017")
    MONGO_DB_NAME: str = os.getenv("MONGO_DB_NAME", "nt_music")

    IS_DOWNLOAD_ALL: bool = os.getenv("IS_DW_ALL", "False").lower() == "true"
    SUDO_ADMIN: int = _int("SUDO_ADMIN", 7194401988)
    USER_SAVED_LIMIT: int = _int("USER_SAVED_LIMIT", 50)   # избранное (макс. треков)
    FAV_PAGE_SIZE: int = _int("FAV_PAGE_SIZE", 10)         # избранное (треков на страницу)
    TRACKS_MAX: int = _int("TRACKS_MAX", 50)               # треков в плейлисте
    PLAYLISTS_MAX: int = _int("PLAYLISTS_MAX", 25)         # плейлистов на пользователя
    SEARCH_LIMIT: int = _int("SEARCH_LIMIT", 7)            # результатов поиска на страницу

    AD_EVERY_N: int = _int("AD_EVERY_N", 5)                # реклама каждые N треков (0 = выкл)
    DEFAULT_LANG: str = os.getenv("DEFAULT_LANG", "ru")