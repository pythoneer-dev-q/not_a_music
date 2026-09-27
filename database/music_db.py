from motor.motor_asyncio import AsyncIOMotorClient
from nanoid import generate
import datetime
import re

from utils.app_config import Config
from database import settings_db

alphabet = '123456789abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ'
client = AsyncIOMotorClient(Config.MONGO_URI)
database = client[Config.MONGO_DB_NAME]
search_col = database['m_search']
music_col = database['m_db']
playlist_col = database['m_pllist']


async def init_indexes():
    await search_col.create_index('query_str')
    await search_col.create_index('title')
    await search_col.create_index('name')
    await search_col.create_index('artist')
    await music_col.create_index('file')
    await music_col.create_index('query_str')
    await music_col.create_index('title')
    await music_col.create_index('artist')
    await music_col.create_index('name')


def _token_filter(query: str) -> list:
    return [t.strip() for t in re.split(r'[\s\-]+', query) if len(t.strip()) >= 2]


def check_query_relevance(query: str, doc: dict) -> bool:
    words = query.lower().split()
    fields_text = ' '.join(str(doc.get(f) or '') for f in ('title', 'name', 'artist', 'query_str')).lower()
    return all(w in fields_text for w in words)


async def search_downloaded_tracks(query: str, limit: int = 7) -> list:
    tokens = _token_filter(query)
    if not tokens:
        return []
    token_clauses = [
        {'$or': [{field: {'$regex': token, '$options': 'i'}} for field in ('query_str', 'title', 'artist', 'name')]}
        for token in tokens
    ]
    mongo_filter = {'$and': token_clauses, 'file': {'$exists': True, '$ne': None}}
    cursor = music_col.find(mongo_filter).limit(limit)
    docs = await cursor.to_list(length=limit)
    result = []
    for doc in docs:
        if not check_query_relevance(query, doc):
            continue
        result.append({
            'id': doc['_id'], 'fileId': doc['_id'],
            'title': doc.get('title') or doc.get('name') or 'Unknown',
            'artist': doc.get('artist') or doc.get('title') or 'SoundCloud',
            'duration': doc.get('duration') or 0,
            'imageInfo': {'imageUrl': doc.get('cover') or ''},
            'url': doc.get('url') or '', 'query_str': doc.get('query_str') or '',
            'is_downloaded': True, 'file': doc.get('file'),
        })
    return result


async def register_request(title: str, name: str, query_str: str, cover: str, _id,
                           artist: str = None, duration: int = 0, url: str = ''):
    await search_col.update_one(
        {'_id': str(_id)},
        {'$set': {'title': title, 'name': name, 'artist': artist or name or title,
                  'query_str': query_str, 'cover': cover, 'duration': duration, 'url': url,
                  'updated_at': datetime.datetime.utcnow()},
         '$setOnInsert': {'likes': 0, 'dislikes': 0, 'in_top': 0}},
        upsert=True
    )
    return str(_id)


async def get_request_by_id(shortId: str):
    return await search_col.find_one({'_id': str(shortId)})


async def get_request_by_query(query: str):
    if '-' in query:
        return await search_col.find_one({'query_str': query})
    return None


async def search_track_exists(shortId: str):
    return await music_col.find_one({'_id': str(shortId)})


async def register_track(shortId, file_id: str, title: str = None, name: str = None,
                         artist: str = None, duration: int = 0, cover: str = None, url: str = None):
    shortId = str(shortId)
    if not (title or name or artist):
        if (sd := await get_request_by_id(shortId)):
            title = sd.get('title', 'Unknown'); name = sd.get('name', 'Unknown')
            artist = sd.get('artist') or name or title
            duration = sd.get('duration') or 0; cover = sd.get('cover') or cover; url = sd.get('url') or url
        else:
            title = name = artist = 'Unknown'
    query_str = f"{artist or name or title} - {title or name}"
    await music_col.update_one(
        {'_id': shortId},
        {'$set': {'title': title, 'name': name, 'artist': artist or name or title,
                  'query_str': query_str, 'file': file_id, 'duration': duration,
                  'cover': cover, 'url': url, 'updated_at': datetime.datetime.utcnow()}},
        upsert=True
    )
    return file_id


async def update_track_likes(shortId: str, to: int):
    await search_col.update_one({'_id': str(shortId)}, {'$inc': {'likes': to}})


async def update_track_dislikes(shortId: str, to: int):
    await search_col.update_one({'_id': str(shortId)}, {'$inc': {'dislikes': to}})


async def search_track_musicDb(shortId: str):
    return await music_col.find_one({'_id': str(shortId)})


async def search_playlistByName(pl_name: str):
    return await playlist_col.find({'pl_name': pl_name}).to_list(length=None)


async def search_playlistById(from_user: int):
    return await playlist_col.find({'founder_id': from_user}).to_list(length=None)


async def pl_count_user(from_user: int) -> int:
    return await playlist_col.count_documents({'founder_id': from_user}) or 0


async def search_playlistByShortId(shortId: str):
    return await playlist_col.find_one({'_id': shortId})


async def delete_allTracks_playlist(shortId: str):
    result = await playlist_col.update_one({'_id': shortId}, {'$set': {'tracks': []}})
    return shortId if result.modified_count > 0 else None


async def delete_playlist(shortId: str):
    result = await playlist_col.delete_one({'_id': shortId})
    return shortId if result.deleted_count > 0 else None


async def delete_tracks_playlist(shortId: str, track_id: str):
    result = await playlist_col.update_one({'_id': shortId}, {'$pull': {'tracks': {'_id': track_id}}})
    return track_id if result.modified_count > 0 else None


async def add_tracks_playlist(shortId: str, title: dict, track_id: str):
    tracks_max = int(await settings_db.get('TRACKS_MAX'))
    result = await playlist_col.update_one(
        {'_id': shortId, f'tracks.{tracks_max - 1}': {'$exists': False}, 'tracks._id': {'$ne': track_id}},
        {'$push': {'tracks': {'_id': track_id, 'title': title}}}
    )
    return track_id if result.modified_count > 0 else None


async def toggle_playlist_like(shortId: str, user_id: int) -> bool:
    pl = await playlist_col.find_one({'_id': shortId}, {'liked_by': 1})
    if not pl:
        return False
    liked_by = pl.get('liked_by') or []
    if user_id in liked_by:
        await playlist_col.update_one({'_id': shortId}, {'$pull': {'liked_by': user_id}})
        return False
    await playlist_col.update_one({'_id': shortId}, {'$addToSet': {'liked_by': user_id}})
    return True


async def inc_playlist_views(shortId: str):
    await playlist_col.update_one({'_id': shortId}, {'$inc': {'views': 1}})


async def create_playlist(from_user: int, pl_name: str):
    pl_max = int(await settings_db.get('PLAYLISTS_MAX'))
    if await playlist_col.count_documents({'founder_id': from_user}) >= pl_max:
        return None
    doc = {
        '_id': generate(alphabet, 15), 'pl_name': pl_name, 'founder_id': from_user,
        'tracks': [], 'liked_by': [],
        'created_at_msk': datetime.datetime.now(), 'updated_at_msk': datetime.datetime.now(),
        'is_public': True, 'views': 0, 'likes': 0,
    }
    await playlist_col.insert_one(doc)
    return doc


async def get_top_tracks(page: int = 1, limit: int = 7) -> dict:
    """??? ?????? ?? ?????????? ?? m_views."""
    skip = max(0, (page - 1) * limit)
    cursor = database['m_views'].find({'count': {'$gt': 0}}).sort('count', -1).skip(skip).limit(limit)
    views_docs = await cursor.to_list(length=limit)
    
    items = []
    for vd in views_docs:
        t_id = str(vd['_id'])
        m_doc = await music_col.find_one({'_id': t_id})
        s_doc = await search_col.find_one({'_id': t_id}) if not m_doc else None
        doc = m_doc or s_doc or {}
        
        artist = doc.get('artist') or doc.get('title') or 'SoundCloud'
        title = doc.get('title') or doc.get('name') or f'Track {t_id}'
        dur = doc.get('duration') or 0
        cover = doc.get('cover') or ''
        
        items.append({
            'id': t_id,
            'fileId': t_id,
            'title': title,
            'artist': artist,
            'duration': int(dur),
            'imageInfo': {'imageUrl': cover},
            'url': doc.get('url') or '',
            'query_str': f"{artist} - {title}",
            'views': vd.get('count', 0),
            'is_downloaded': bool(doc.get('file')),
            'file': doc.get('file'),
        })
    
    total = await database['m_views'].count_documents({'count': {'$gt': 0}})
    pages_all = max(1, -(-total // limit))
    return {'items': items, 'paginationInfo': {'lastPage': pages_all}}
