import datetime

from motor.motor_asyncio import AsyncIOMotorClient

from utils.app_config import Config
from database import music_db, settings_db

client = AsyncIOMotorClient(Config.MONGO_URI)
database = client[Config.MONGO_DB_NAME]
likes_col = database['m_likes']
dislikes_col = database['m_dislikes']
saved_col = database['m_saved']
views_col = database['m_views']
history_col = database['m_history']          # {_id: "{uid}:{tid}", user_id, track_id, at, count}

HISTORY_TTL_SECONDS = 30 * 24 * 3600         # история хранится 30 дней
_history_indexes_ready = False


async def _ensure_history_indexes():
    global _history_indexes_ready
    if _history_indexes_ready:
        return
    await history_col.create_index('at', expireAfterSeconds=HISTORY_TTL_SECONDS)
    await history_col.create_index([('user_id', 1), ('at', -1)])
    _history_indexes_ready = True


# ------------------------- история прослушиваний -------------------------

async def add_history(user_id: int, track_id):
    """Записывает прослушивание в историю (повторное — продлевает срок)."""
    await _ensure_history_indexes()
    user_id, track_id = int(user_id), str(track_id)
    await history_col.update_one(
        {'_id': f"{user_id}:{track_id}"},
        {'$set': {'user_id': user_id, 'track_id': track_id,
                  'at': datetime.datetime.now(datetime.timezone.utc)},
         '$inc': {'count': 1}},
        upsert=True,
    )


async def history_count(user_id: int) -> int:
    await _ensure_history_indexes()
    return await history_col.count_documents({'user_id': int(user_id)}) or 0


async def get_history(user_id: int, skip: int = 0, limit: int = 7) -> list:
    await _ensure_history_indexes()
    cursor = (history_col.find({'user_id': int(user_id)})
              .sort('at', -1).skip(max(0, skip)).limit(limit))
    return await cursor.to_list(length=limit)


async def delete_history_item(user_id: int, track_id: str):
    await _ensure_history_indexes()
    await history_col.delete_one({'_id': f"{int(user_id)}:{str(track_id)}"})


async def clear_history(user_id: int) -> int:
    await _ensure_history_indexes()
    result = await history_col.delete_many({'user_id': int(user_id)})
    return result.deleted_count or 0


# ------------------------- просмотры -------------------------

async def register_view(track_id):
    """Счетчик просмотров (прослушиваний) трека."""
    await views_col.update_one(
        {'_id': str(track_id)},
        {'$inc': {'count': 1}},
        upsert=True,
    )


async def count_views(track_id) -> int:
    doc = await views_col.find_one({'_id': str(track_id)}, {'count': 1})
    return (doc or {}).get('count', 0)


# ------------------------- лайки / дизлайки -------------------------

async def count_likes(track_id) -> int:
    return await likes_col.count_documents({'track_id': str(track_id)}) or 0


async def count_dislikes(track_id) -> int:
    return await dislikes_col.count_documents({'track_id': str(track_id)}) or 0


async def get_like(user_id: int, track_id: str):
    return await likes_col.find_one({'user_id': user_id, 'track_id': str(track_id)})


async def get_dislike(user_id: int, track_id: str):
    return await dislikes_col.find_one({'user_id': user_id, 'track_id': str(track_id)})


async def toggle_like(user_id: int, track_id: str) -> bool:
    track_id = str(track_id)
    deleted_dislike = await dislikes_col.find_one_and_delete({
        'user_id': user_id, 'track_id': track_id
    })
    if deleted_dislike:
        await music_db.update_track_dislikes(track_id, -1)

    existing_like = await likes_col.find_one({'user_id': user_id, 'track_id': track_id})
    if existing_like:
        await likes_col.delete_one({'_id': existing_like['_id']})
        await music_db.update_track_likes(track_id, -1)
        return False

    await likes_col.insert_one({'user_id': user_id, 'track_id': track_id})
    await music_db.update_track_likes(track_id, 1)
    return True


async def toggle_dislike(user_id: int, track_id: str) -> bool:
    track_id = str(track_id)
    deleted_like = await likes_col.find_one_and_delete({
        'user_id': user_id, 'track_id': track_id
    })
    if deleted_like:
        await music_db.update_track_likes(track_id, -1)

    existing_dislike = await dislikes_col.find_one({'user_id': user_id, 'track_id': track_id})
    if existing_dislike:
        await dislikes_col.delete_one({'_id': existing_dislike['_id']})
        await music_db.update_track_dislikes(track_id, -1)
        return False

    await dislikes_col.insert_one({'user_id': user_id, 'track_id': track_id})
    await music_db.update_track_dislikes(track_id, 1)
    return True


async def count_user_likes(user_id: int) -> int:
    return await likes_col.count_documents({'user_id': int(user_id)}) or 0


async def count_user_dislikes(user_id: int) -> int:
    return await dislikes_col.count_documents({'user_id': int(user_id)}) or 0


# ------------------------- избранное (макс. FAV_MAX, пагинация) -------------------------

async def get_saved_one(user_id: int, track_id: str):
    return await saved_col.find_one({'user_id': user_id, 'track_id': str(track_id)})


async def saved_count(user_id: int) -> int:
    return await saved_col.count_documents({'user_id': user_id}) or 0


async def get_saved(user_id: int, skip: int = 0, limit: int = None):
    cursor = saved_col.find({'user_id': user_id}).sort('added_at', -1)
    if skip:
        cursor = cursor.skip(skip)
    if limit:
        cursor = cursor.limit(limit)
    else:
        limit = int(await settings_db.get('USER_SAVED_LIMIT'))
        cursor = cursor.limit(limit)
    return await cursor.to_list(length=limit)


async def toggle_saved(user_id: int, track_id: str) -> str:
    track_id = str(track_id)
    existing = await get_saved_one(user_id, track_id)
    if existing:
        await saved_col.delete_one({'_id': existing['_id']})
        return 'removed'

    fav_max = int(await settings_db.get('USER_SAVED_LIMIT'))
    if await saved_col.count_documents({'user_id': user_id}) >= fav_max:
        return 'full'

    info = (await music_db.get_request_by_id(track_id)
            or await music_db.search_track_musicDb(track_id)
            or {})
    await saved_col.insert_one({
        'user_id': user_id,
        'track_id': track_id,
        'title': info.get('title'),
        'name': info.get('name'),
        'query_str': info.get('query_str') or f"{info.get('name', 'Unknown')} - {info.get('title', 'Unknown')}",
        'added_at': datetime.datetime.now(),
    })
    return 'added'