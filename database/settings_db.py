import time

from motor.motor_asyncio import AsyncIOMotorClient

from utils.app_config import Config

client = AsyncIOMotorClient(Config.MONGO_URI)
database = client[Config.MONGO_DB_NAME]
settings_col = database['m_settings']

INT_KEYS = (
    'USER_SAVED_LIMIT',   # максимум треков
    'FAV_PAGE_SIZE',      # на страницу
    'TRACKS_MAX',         # в плейлисте
    'PLAYLISTS_MAX',      # плейлистов 
    'SEARCH_LIMIT',       # результатов 
    'AD_EVERY_N',         # реклама к
)

ALL_KEYS = INT_KEYS + ('DEFAULT_LANG',)

DEFAULTS = {
    'USER_SAVED_LIMIT': Config.USER_SAVED_LIMIT,
    'FAV_PAGE_SIZE': Config.FAV_PAGE_SIZE,
    'TRACKS_MAX': Config.TRACKS_MAX,
    'PLAYLISTS_MAX': Config.PLAYLISTS_MAX,
    'SEARCH_LIMIT': Config.SEARCH_LIMIT,
    'AD_EVERY_N': Config.AD_EVERY_N,
    'DEFAULT_LANG': Config.DEFAULT_LANG,
}


_CACHE_TTL = 30.0  # сек: настройки меняются редко, кэш бережёт скан коллекции
_cache = {'vals': None, 'expires': 0.0}


async def get_all() -> dict:
    now = time.monotonic()
    if _cache['vals'] is None or now >= _cache['expires']:
        vals = dict(DEFAULTS)
        async for doc in settings_col.find({}):
            key = doc.get('_id')
            if key in vals and doc.get('value') is not None:
                vals[key] = doc['value']
        _cache['vals'] = vals
        _cache['expires'] = now + _CACHE_TTL
    return _cache['vals']


async def get(key: str):
    vals = await get_all()
    return vals.get(key, DEFAULTS.get(key))


async def set_key(key: str, value):
    if key not in ALL_KEYS:
        raise ValueError(f'Unknown setting: {key}')
    await settings_col.update_one(
        {'_id': key},
        {'$set': {'value': value}},
        upsert=True,
    )
    if _cache['vals'] is not None:
        _cache['vals'][key] = value
    return value


async def reset() -> int:
    result = await settings_col.delete_many({})
    _cache['vals'] = None
    _cache['expires'] = 0.0
    return result.deleted_count