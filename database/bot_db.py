from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ReturnDocument
from nanoid import generate

from utils.app_config import Config

alphabet = '123456789abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ'
client = AsyncIOMotorClient(Config.MONGO_URI)
database = client[Config.MONGO_DB_NAME]
users = database['m_users']
admins = database['m_admins']
ads_db = database['m_ads']
channels = database['m_channels']


async def register_user(from_source: str, user_id: int, user_name: str):
    """Регистрация пользователя (защита от повторного старта)."""
    existing = await search_user(user_id)
    if existing:
        return existing
    doc = {
        '_id': generate(alphabet, 16),
        'user_name': user_name,
        'user_id': user_id,
        'source': from_source,
        'user_status': 'OK',
        'lang': None,            # язык интерфейса (i18n)
        'is_hidden': False,      # скрытие профиля
        'about': '',             # o sebe
        'played_count': 0,       # treki
    }
    await users.insert_one(doc)
    return doc


async def search_user(user_id: int):
    return await users.find_one({'user_id': user_id})


async def search_userById(user_id: str):
    return await users.find_one({'_id': user_id})


async def set_lang(user_id: int, lang: str):
    await users.update_one({'user_id': user_id}, {'$set': {'lang': lang}})


async def set_hidden(user_id: int, hidden: bool):
    await users.update_one({'user_id': user_id}, {'$set': {'is_hidden': bool(hidden)}})


async def set_about(user_id: int, about: str):
    await users.update_one({'user_id': user_id}, {'$set': {'about': about[:300]}})


async def incr_played(user_id: int) -> int:
    """Увеличивает счетчик прослушанных треков и возвращает новое значение."""
    doc = await users.find_one_and_update(
        {'user_id': user_id},
        {'$inc': {'played_count': 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return (doc or {}).get('played_count', 1)


async def block_user(user_id: int, block_reason: str):
    doc = {
        'user_status': 'BAN',
        'ban_reason': block_reason,
        'ban_id': generate(alphabet, 7),
    }
    await users.update_one({'user_id': user_id}, {'$set': doc})
    return doc


async def make_admin(user_id: int, from_user: int):
    await admins.insert_one({'user_id': user_id})


async def search_admin(user_id: int):
    return await admins.find_one({'user_id': user_id})


async def del_admin(user_id: int, from_user: int):
    await admins.delete_one({'user_id': user_id})


async def new_ads(link: str, short_name: str):
    await ads_db.insert_one({
        '_id': generate(alphabet, 11),
        'url': link,
        'text': short_name,
    })


async def get_ads_random():
    """Случайное рекламное объявление (для показа каждые N треков)."""
    async for doc in ads_db.aggregate([{'$sample': {'size': 1}}]):
        return doc
    return None


async def del_ads(_id: str):
    await ads_db.delete_one({'_id': _id})
    return _id


async def add_channel(link: str, chat_id: int):
    doc = {
        '_id': generate(alphabet, 11),
        'url': link,
        'chat_id': chat_id,
    }
    await channels.insert_one(doc)
    return doc


async def del_channel(_id: str):
    # fix: раньше удалялся по несуществующему полю `id`
    await channels.delete_one({'_id': _id})


def get_channels():
    return channels.find({})
