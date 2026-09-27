import asyncio
import math

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database import actions_db, bot_db, music_db, nav_db, settings_db
from utils.i18n import t

DEFAULT_COVER = 'https://storage.yandexcloud.net/user-media/35881445362164.jpg'

PL_PAGE_SIZE = 10  # треков плейлиста на страницу


async def _pack_cb(payload: str) -> str:
    """Упаковка callback-payload в БД: короткий ID вместо лимита 64 байт."""
    return 'n' + await nav_db.pack(payload)


async def _pack_many(payloads) -> list:
    """Батч-упаковка N payload-ов одной вставкой в БД (вместо N insert_one).

    Клавиатуры строят 3-20 кнопок за раз — каждая отдельная вставка это
    round-trip к Mongo, батч превращает их в один.
    """
    ids = await nav_db.pack_many(payloads)
    return ['n' + i for i in ids]


def _nav_row(page: int, pages_all: int, prev_cb: str, next_cb: str, current_cb: str = 'nt'):
    """Строка пагинации: ⬅️ / стр. / ➡️"""
    row = []
    if page > 1:
        row.append(InlineKeyboardButton(text="⬅️", callback_data=prev_cb))
    row.append(InlineKeyboardButton(text=t('page_of', p=page, all=pages_all), callback_data=current_cb, style='danger'))
    if page < pages_all:
        row.append(InlineKeyboardButton(text="➡️", callback_data=next_cb))
    return row


async def grt_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=t('btn_go'), callback_data='reg_rg')
    ]])


async def greet_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t('btn_next'), callback_data='reg_reg')],
        [InlineKeyboardButton(text=t('btn_rules'), url='https://ya.ru/')],
    ])


async def greet_kb_2():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t('btn_policy'), url='https://ya.ru/')],
        [InlineKeyboardButton(text=t('btn_done'), callback_data='reg_reg_2')],
    ])


async def sub_channels():
    builder = InlineKeyboardBuilder()
    async for channel in bot_db.get_channels():
        builder.row(InlineKeyboardButton(text='↗️ Подписаться', url=channel.get('url')))
    builder.row(InlineKeyboardButton(text=t('btn_done'), callback_data='user_checkSub'))
    return builder.as_markup()


async def back_to(to_: str, text: str = None):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=text or t('btn_back'), callback_data=to_, style='primary')
    ]])



async def main_menu(from_user: int) -> InlineKeyboardMarkup:
    pl_count = await music_db.pl_count_user(from_user)
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=t('btn_top'), callback_data='user_topTracks', style='success'),
            InlineKeyboardButton(text=t('btn_favs'), callback_data='favs:1'),
        ],
        [InlineKeyboardButton(text=t('btn_search'), callback_data='user_search', style='success')],
        [InlineKeyboardButton(text=t('btn_playlists', n=pl_count), callback_data='user_pllists', style='primary')],
        [InlineKeyboardButton(text=t('btn_profile'), callback_data='prof')],
    ])


# ------------------------- поиск / топ -------------------------

async def get_search_kb(items: list, query: str, page: int, pages_all: int, origin: str):
    builder = InlineKeyboardBuilder()

    # батч: все payload-ы (треки + пагинация) одной вставкой
    play_payloads = [f"dl|{origin}|{track.get('fileId') or track.get('id')}" for track in items]
    nav_payloads = []
    if page > 1:
        nav_payloads.append(f"srp|{query}|{page - 1}")
    if page < pages_all:
        nav_payloads.append(f"srp|{query}|{page + 1}")
    ids = await _pack_many(play_payloads + nav_payloads)
    nav_ids = ids[len(play_payloads):]

    for track, cb in zip(items, ids):
        artist   = track.get('artist') or track.get('title') or 'Unknown'
        title    = track.get('title') or track.get('name') or 'Unknown'
        duration = int(track.get('duration') or 0)
        dur_str  = f"{duration // 60}:{duration % 60:02d}" if duration else "?:??"
        if track.get('is_downloaded'):
            text = f"✅ {artist} — {title} [{dur_str}]"
            builder.button(text=text, callback_data=cb, style='success')
        else:
            text = f"🎵 {artist} — {title} [{dur_str}]"
            builder.button(text=text, callback_data=cb)

    builder.adjust(*([1] * len(items)))

    nav = []
    idx = 0
    if page > 1:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=nav_ids[idx]))
        idx += 1
    nav.append(InlineKeyboardButton(text=t('page_of', p=page, all=pages_all), callback_data='nt', style='danger'))
    if page < pages_all:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=nav_ids[idx]))
    builder.row(*nav)

    builder.row(InlineKeyboardButton(text=t('btn_to_menu'), callback_data='user_menu'))
    return builder.as_markup()


# ------------------------- клавиатура трека -------------------------

async def track_kb(t_id: str, user_id: int, back_cb: str = 'user_menu', next_payload: str = None):
    t_id = str(t_id)
    nxt = next_payload or '-'

    # счётчики и флаги — одним параллельным пакетом вместо 6 последовательных запросов
    likes, dislikes, views, is_liked, is_disliked, is_saved = await asyncio.gather(
        actions_db.count_likes(t_id),
        actions_db.count_dislikes(t_id),
        actions_db.count_views(t_id),
        actions_db.get_like(user_id, t_id),
        actions_db.get_dislike(user_id, t_id),
        actions_db.get_saved_one(user_id, t_id),
    )

    # все callback-кнопки пакуются одной вставкой в БД
    payloads = [
        f'lk|{t_id}|{back_cb}|{nxt}',
        f'dk|{t_id}|{back_cb}|{nxt}',
        f'fsv|{t_id}|{back_cb}|{nxt}',
        f'pl_add|{t_id}',
    ]
    if next_payload:
        payloads.append(next_payload)
    packed = await _pack_many(payloads)
    lk_cb, dk_cb, fsv_cb, pladd_cb = packed[0], packed[1], packed[2], packed[3]
    next_cb = packed[4] if next_payload else None

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=f"{'❤️' if is_liked else '🤍'} {likes}",
            callback_data=lk_cb),
        InlineKeyboardButton(
            text=f"{'💔' if bool(is_disliked) else '👎'} {dislikes}",
            callback_data=dk_cb),
        InlineKeyboardButton(text=f"👁 {views}", callback_data='nt', style='primary'),
    )
    builder.row(
        InlineKeyboardButton(
            text=t('btn_in_fav') if is_saved else t('btn_add_fav'),
            callback_data=fsv_cb),
        InlineKeyboardButton(
            text=t('btn_add_pl'),
            callback_data=pladd_cb),
    )
    if next_payload:
        builder.row(InlineKeyboardButton(
            text=t('btn_next_track'),
            callback_data=next_cb))
    builder.row(InlineKeyboardButton(text=t('btn_back'), callback_data=back_cb))
    return builder.as_markup()


# ------------------------- избранное -------------------------

async def fav_kb(page: int, pages_all: int, tracks: list):
    # батч: 2 payload-а на трек одной вставкой
    payloads = []
    for doc in tracks:
        t_id = doc.get('track_id')
        payloads.append(f"dl|fav:{page}|{t_id}")
        payloads.append(f"frem|{t_id}|{page}")
    ids = await _pack_many(payloads)

    builder = InlineKeyboardBuilder()
    for i, doc in enumerate(tracks):
        label = doc.get('query_str') or f"{doc.get('name', '?')} - {doc.get('title', '?')}"
        builder.row(
            InlineKeyboardButton(
                text=f"▶️ {str(label)[:40]}",
                callback_data=ids[2 * i], style='primary'),
            InlineKeyboardButton(
                text='❌',
                callback_data=ids[2 * i + 1], style='danger'),
        )
    builder.row(*_nav_row(
        page, pages_all,
        prev_cb=f'favs:{max(page - 1, 1)}',
        next_cb=f'favs:{min(page + 1, pages_all)}',
    ))
    builder.row(InlineKeyboardButton(text=t('btn_to_menu'), callback_data='user_menu'))
    return builder.as_markup()

async def user_playlists(pllists: list):
    pl_max = int(await settings_db.get('PLAYLISTS_MAX'))
    builder = InlineKeyboardBuilder()
    for pl in pllists or []:
        builder.row(InlineKeyboardButton(
            text=f'🎵 {pl.get("pl_name")}',
            callback_data=f'plv:{pl.get("_id")}:1'
        ))
    if len(pllists or []) < pl_max:
        builder.row(InlineKeyboardButton(text=t('btn_create_pl'), callback_data='pl_new'))
    builder.row(InlineKeyboardButton(text=t('btn_to_menu'), callback_data='user_menu'))
    return builder.as_markup()


async def show_playlists(pllists: list):
    builder = InlineKeyboardBuilder()
    for pl in pllists or []:
        builder.row(InlineKeyboardButton(
            text=f'🎵 {pl.get("pl_name")}',
            callback_data=f'plv:{pl.get("_id")}:1'
        ))
    builder.row(InlineKeyboardButton(text=t('btn_back'), callback_data='user_menu'))
    return builder.as_markup()


async def pl_add_kb(pllists: list, track_id: str, back_to: str = 'user_menu'):
    builder = InlineKeyboardBuilder()
    pllists = pllists or []
    # батч: один payload на плейлист
    ids = await _pack_many([f'pl_up|{pl.get("_id")}|{track_id}' for pl in pllists])
    for pl, cb in zip(pllists, ids):
        builder.row(InlineKeyboardButton(
            text=f'➕ {pl.get("pl_name")}',
            callback_data=cb
        ))
    builder.row(InlineKeyboardButton(text=t('btn_create_pl'), callback_data='pl_new'))
    builder.row(InlineKeyboardButton(text=t('btn_back'), callback_data=back_to))
    return builder.as_markup()


async def pl_view_kb(pl_data: dict, user_id: int, page: int = 1):
    pl_id = pl_data.get('_id')
    tracks = [tr for tr in (pl_data.get('tracks') or []) if isinstance(tr, dict)]
    is_owner = pl_data.get('founder_id') == user_id

    pages_all = max(1, math.ceil(len(tracks) / PL_PAGE_SIZE))
    page = min(max(1, page), pages_all)
    start = (page - 1) * PL_PAGE_SIZE

    builder = InlineKeyboardBuilder()
    page_tracks = tracks[start:start + PL_PAGE_SIZE]

    # батч: play-кнопки + delete-кнопки владельца + «слушать с начала» — одной вставкой
    play_payloads = [f'dl|pl:{pl_id}:{page}|{tr.get("_id")}' for tr in page_tracks]
    del_payloads = ([f'pl_del|{pl_id}|{page}|{tr.get("_id")}' for tr in page_tracks]
                    if is_owner else [])
    listen_payload = (f'dl|pl:{pl_id}:{page}|{tracks[0].get("_id")}' if tracks else None)
    ids = await _pack_many(play_payloads + del_payloads + ([listen_payload] if listen_payload else []))
    del_ids = ids[len(play_payloads):len(play_payloads) + len(del_payloads)]
    listen_cb = ids[-1] if listen_payload else None

    for i, tr in enumerate(page_tracks):
        title = tr.get('title') or {}
        label = title.get('query_str') if isinstance(title, dict) else None
        label = label or f'ID: {tr.get("_id")}'
        row = [InlineKeyboardButton(
            text=f'▶️ {str(label)[:40]}',
            callback_data=ids[i], style='primary'
        )]
        if is_owner:
            row.append(InlineKeyboardButton(
                text='🗑',
                callback_data=del_ids[i]
            ))
        builder.row(*row)

    if pages_all > 1:
        builder.row(*_nav_row(
            page, pages_all,
            prev_cb=f'plv:{pl_id}:{page - 1}',
            next_cb=f'plv:{pl_id}:{page + 1}',
        ))

    if tracks:
        builder.row(InlineKeyboardButton(
            text=t('btn_listen_all'),
            callback_data=listen_cb
        ))

    builder.row(
        InlineKeyboardButton(
            text=f"❤️ {len(pl_data.get('liked_by') or [])}",
            callback_data=f'plk|{pl_id}|{page}'),
        InlineKeyboardButton(
            text=f"👁 {pl_data.get('views', 0)}",
            callback_data='nt', style='primary'),
    )
    builder.row(InlineKeyboardButton(
        text=t('btn_share_pl'),
        switch_inline_query=f'playlist_{pl_id}',
    ))
    if is_owner:
        builder.row(InlineKeyboardButton(text=t('btn_del_pl'), callback_data=f'pl_pdel|{pl_id}'))
        builder.row(InlineKeyboardButton(text=t('btn_clear_pl'), callback_data=f'pl_tdel|{pl_id}'))
    builder.row(InlineKeyboardButton(text=t('btn_to_playlists'), callback_data='user_pllists'))
    return builder.as_markup()



async def profile_kb(user_data: dict):
    lang = user_data.get('lang') or await settings_db.get('DEFAULT_LANG')
    hidden = bool(user_data.get('is_hidden'))
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=t('btn_visibility_off') if hidden else t('btn_visibility_on'),
            callback_data='prof_vis')],
        [InlineKeyboardButton(text=t('btn_about'), callback_data='prof_about')],
        [InlineKeyboardButton(text=t('btn_to_menu'), callback_data='user_menu')],
    ])


async def ad_kb(url: str):
    if not url:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='➡️ Перейти', url=url)
    ]])


async def ad_continue_kb(go_payload: str):
    """Кнопка «Продолжить» после рекламы: возврат к последнему разделу."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=t('btn_continue'),
            callback_data=await _pack_cb(go_payload))
    ]])


# ------------------------- инлайн -------------------------

async def inline_pllist(pl_data: dict, bot_username: str):
    builder = InlineKeyboardBuilder()
    tracks = [tr for tr in (pl_data.get('tracks') or []) if isinstance(tr, dict)][:5]

    builder.row(
        InlineKeyboardButton(text=f"❤️ {len(pl_data.get('liked_by') or [])}", callback_data='nt'),
        InlineKeyboardButton(text=f"👁 {pl_data.get('views', 0)}", callback_data='nt'),
    )

    for track in tracks:
        title = track.get('title') or {}
        label = title.get('query_str') if isinstance(title, dict) else None
        label = label or f'ID: {track.get("_id")}'
        builder.row(InlineKeyboardButton(text=f'🎵 {str(label)[:40]}', callback_data='call_to_bot'))

    total = len(pl_data.get('tracks') or [])
    if total > 5:
        builder.row(InlineKeyboardButton(text=f'... и еще {total - 5}'))

    builder.row(InlineKeyboardButton(
        text=t('btn_listen_bot'),
        url=f'https://t.me/{bot_username}?start=pl_{pl_data.get("_id")}',
    ))
    return builder.as_markup()