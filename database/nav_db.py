# callbacks factory :0
import datetime

from motor.motor_asyncio import AsyncIOMotorClient
from nanoid import generate

from utils.app_config import Config

alphabet = '123456789abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ'
client = AsyncIOMotorClient(Config.MONGO_URI)
database = client[Config.MONGO_DB_NAME]
nav_col = database['m_nav']        # {_id: short_id, payload, created_at}
state_col = database['m_state']    # {_id: user_id, last_cb, updated_at}

NAV_TTL_SECONDS = 7 * 24 * 3600  
_id_len = 10

_indexes_ready = False


async def _ensure_indexes():
    global _indexes_ready
    if not _indexes_ready:
        await nav_col.create_index('created_at', expireAfterSeconds=NAV_TTL_SECONDS)
        _indexes_ready = True


async def pack(payload: str) -> str:
    await _ensure_indexes()
    _id = generate(alphabet, _id_len)
    await nav_col.insert_one({
        '_id': _id,
        'payload': str(payload)[:1024],
        'created_at': datetime.datetime.now(datetime.timezone.utc),
    })
    return _id


async def pack_many(payloads) -> list:
    """Батч-упаковка N payload-ов ОДНОЙ вставкой (вместо N insert_one).

    Критично для скорости: клавиатуры списков пакуют 10-20 кнопок,
    каждая отдельная вставка — это round-trip к Mongo.
    """
    payloads = [str(p)[:1024] for p in payloads]
    if not payloads:
        return []
    await _ensure_indexes()
    now = datetime.datetime.now(datetime.timezone.utc)
    docs = [{'_id': generate(alphabet, _id_len),
             'payload': p,
             'created_at': now} for p in payloads]
    await nav_col.insert_many(docs)
    return [d['_id'] for d in docs]


async def unpack(nav_id: str):
    doc = await nav_col.find_one({'_id': nav_id})
    return doc.get('payload') if doc else None


async def set_last_cb(user_id: int, payload: str):
    await state_col.update_one(
        {'_id': user_id},
        {'$set': {'last_cb': str(payload)[:1024],
                  'updated_at': datetime.datetime.now(datetime.timezone.utc)}},
        upsert=True,
    )


async def get_last_cb(user_id: int):
    doc = await state_col.find_one({'_id': user_id}, {'last_cb': 1})
    return (doc or {}).get('last_cb')


# ------------------------- история поисковых запросов -------------------------

RECENT_QUERIES_MAX = 5


def clean_query(query) -> str:
    """Нормализация запроса для хранения (обезвреживает разделители payload-ов)."""
    return str(query or '').strip().replace('|', ' ').replace(':', ' ')[:50]


async def add_query(user_id: int, query: str) -> list:
    """Сохраняет запрос в конец списка недавних (дубликат убирается)."""
    query = clean_query(query)
    if not query:
        return await get_queries(user_id)
    # $pull и $push по одному полю нельзя выполнять одним update (conflict),
    # поэтому два прохода — это происходит один раз на поиск.
    await state_col.update_one({'_id': user_id}, {'$pull': {'recent_queries': query}},
                               upsert=True)
    await state_col.update_one(
        {'_id': user_id},
        {'$push': {'recent_queries': {'$each': [query], '$slice': -RECENT_QUERIES_MAX}}},
        upsert=True)
    return await get_queries(user_id)


async def get_queries(user_id: int) -> list:
    doc = await state_col.find_one({'_id': user_id}, {'recent_queries': 1})
    return list((doc or {}).get('recent_queries') or [])


async def clear_queries(user_id: int):
    await state_col.update_one({'_id': user_id}, {'$pull': {'recent_queries': None}})