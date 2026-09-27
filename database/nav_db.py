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