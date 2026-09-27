import asyncio
from html import escape

from aiogram import Router, F
from aiogram.filters import CommandStart, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from core.app_logic import downloader
from database import bot_db, music_db, nav_db, settings_db
from handlers import fsm_classes
from utils import kbs, messages, routes, utils
from utils.i18n import t

arouter = Router()


def _safe_q(query: str) -> str:
    return str(query).replace('|', ' ').replace(':', ' ')[:50]


@arouter.message(CommandStart())
async def greet_handler(message: Message, command: CommandObject):
    user = message.from_user
    user_id = user.id
    username = escape(user.first_name)
    args = command.args or ""

    is_registered = await bot_db.search_user(user_id)

    if args:
        if args.startswith("pl_"):
            playlist_id = args[3:]
            pl_data = await music_db.search_playlistByShortId(playlist_id)
            if not pl_data:
                return await message.answer(t('pl_not_found'))

            if not is_registered:
                await bot_db.register_user(f"Плейлист: {playlist_id}", user_id, username)

            await music_db.inc_playlist_views(playlist_id)
            await nav_db.set_last_cb(user_id, f'plv:{playlist_id}:1')
            return await message.answer(
                text=await messages.render_pllist(pl_data, (await message.bot.get_me()).username),
                reply_markup=await kbs.pl_view_kb(pl_data, user_id, page=1),
            )

        if args.startswith("us_"):
            user_short_id = args[3:]
            user_data = await bot_db.search_userById(user_short_id)
            if not user_data:
                return await message.answer(t('user_not_found'))

            if not is_registered:
                await bot_db.register_user("Профиль", user_id, username)

            pllists = await music_db.search_playlistById(user_data.get('user_id'))
            return await message.answer(
                text=await messages.show_profile(user_data, (await message.bot.get_me()).username),
                reply_markup=await kbs.show_playlists(pllists or []),
            )

    if not is_registered:
        source_name = "Кнопка Start"
        if "_" in args:
            raw = args.split("_", 1)[1]
            if raw.isdigit():
                try:
                    chat = await message.bot.get_chat(int(raw))
                    source_name = escape(chat.first_name or f"ID: {raw}")
                except Exception:
                    source_name = f"ID: {raw}"
            elif raw:
                source_name = f"Web: {raw}"

        await bot_db.register_user(source_name, user_id, username)
        return await message.answer(
            text=await messages.main_greet_sub(username, source=source_name),
            reply_markup=await kbs.grt_kb(),
        )

    await nav_db.set_last_cb(user_id, 'user_menu')
    await message.answer(text=await messages.main_menu(), reply_markup=await kbs.main_menu(user_id))


@arouter.message(fsm_classes.NewPl.title)
async def new_pl(message: Message, state: FSMContext):
    if len(message.text) <= 30:
        result = await music_db.create_playlist(
            from_user=message.from_user.id,
            pl_name=escape(message.text),
        )
        await state.clear()

        if not result:
            return await message.answer(
                t('pl_limit', max=int(await settings_db.get('PLAYLISTS_MAX'))))

        await message.answer(
            text=t('pl_created') + "\n\n" + await messages.render_pllist(
                result, (await message.bot.get_me()).username),
            reply_markup=await kbs.back_to('user_pllists', t('btn_to_playlists')),
        )
    else:
        await message.answer(t('pl_name_long'))


@arouter.message(fsm_classes.Profile.about)
async def process_about(message: Message, state: FSMContext):
    await bot_db.set_about(message.from_user.id, message.text.strip())
    await state.clear()
    await message.answer(t('about_saved'))


# ──────────── поиск ────────────

async def render_search(call: CallbackQuery, query: str, page: int):
    """Отрисовка страницы результатов (поиск или топ)."""
    if query == 'top':
        search_data = await music_db.get_top_tracks(page=page)
        origin = 'top'
        title = t('top_chart')
        cached_tracks = []
        if not search_data or not search_data.get('items'):
            # Если в базе ещё нет прослушиваний — берём чарт прямо из SoundCloud
            search_data = await downloader.top_tracks(page=page)
    else:
        cached_task = asyncio.create_task(music_db.search_downloaded_tracks(query, limit=3))
        sc_task = asyncio.create_task(downloader.search_track(query, page=page))
        cached_tracks = await cached_task
        search_data = await sc_task
        origin = f"sq:{_safe_q(query)}:{page}"
        title = t('results_for', query=escape(query))
        if page == 1:
            # запоминаем запрос для кнопок «Недавние» на экране поиска
            utils.spawn(nav_db.add_query(call.from_user.id, query))

    if not search_data or not isinstance(search_data, dict):
        return await call.answer(t('no_results'), show_alert=True)

    sc_items = search_data.get('items', [])
    if not sc_items and not cached_tracks:
        return await call.answer(t('no_more'), show_alert=True)

    for term in sc_items:
        track_id = str(term.get('fileId') or term.get('id') or '')
        img = (term.get('imageInfo') or {}).get('imageUrl')
        utils.spawn(music_db.register_request(
            title=term.get('title'),
            name=term.get('artist'),
            cover=img or kbs.DEFAULT_COVER,
            query_str=term.get('query_str') or f"{term.get('artist')} - {term.get('title')}",
            _id=track_id,
            artist=term.get('artist'),
            duration=term.get('duration') or 0,
            url=term.get('url') or '',
        ))

    cached_ids = {str(c['id']) for c in cached_tracks}
    sc_items_dedup = [t2 for t2 in sc_items if str(t2.get('fileId') or t2.get('id') or '') not in cached_ids]
    for c in cached_tracks:
        c['is_downloaded'] = True
    items = cached_tracks + sc_items_dedup

    pages_all = (search_data.get('paginationInfo') or {}).get('lastPage', 1)
    safe_q = _safe_q(query)
    await utils.edit_or_resend(
        call.message, title,
        reply_markup=await kbs.get_search_kb(items, safe_q, page, pages_all, origin),
    )
    await nav_db.set_last_cb(call.from_user.id, f'srp|{safe_q}|{page}')


@routes.route('srp|')
async def search_page(call: CallbackQuery, data: str = None):
    data = data or call.data
    try:
        _, query, page = data.split('|', 2)
    except ValueError:
        return await call.answer()
    await call.answer()
    await render_search(call, query, int(page))


@arouter.message(F.text)
async def search_items(message: Message):
    if await utils.checkAccount(message, message.from_user.id) != 'OK':
        return

    query = message.text.strip()
    if len(query) > 50:
        return await message.answer(t('too_long'))
    if len(query) < 2:
        return

    # Слишком короткий/бессмысленный запрос
    import re as _re
    if len(query) < 3 or not [tok for tok in _re.split(r'[\s\-]+', query) if len(tok) >= 3]:
        return await message.answer(
            "🔍 Запрос слишком короткий. Уточни: напиши название трека или исполнителя подробнее.",
            reply_markup=await kbs.back_to('user_menu', t('btn_to_menu')),
        )

    status_message = await message.answer(await messages.search_progress(query))

    # запоминаем запрос для кнопок «Недавние» на экране поиска
    utils.spawn(nav_db.add_query(message.from_user.id, query))

    cached_task = asyncio.create_task(music_db.search_downloaded_tracks(query, limit=3))
    sc_task = asyncio.create_task(downloader.search_track(query))
    cached_tracks = await cached_task
    search_data = await sc_task

    if not search_data or not isinstance(search_data, dict) or (not search_data.get('items') and not cached_tracks):
        return await status_message.edit_text(
            "🤔 Ничего не нашлось. Попробуй уточнить: укажи исполнителя или полное название трека.",
            reply_markup=await kbs.back_to('user_menu', t('btn_to_menu')),
        )

    sc_items = search_data.get('items', [])
    for term in sc_items:
        track_id = str(term.get('fileId') or term.get('id') or '')
        img = (term.get('imageInfo') or {}).get('imageUrl')
        utils.spawn(music_db.register_request(
            title=term.get('title'),
            name=term.get('artist'),
            cover=img or kbs.DEFAULT_COVER,
            query_str=term.get('query_str') or f"{term.get('artist')} - {term.get('title')}",
            _id=track_id,
            artist=term.get('artist'),
            duration=term.get('duration') or 0,
            url=term.get('url') or '',
        ))

    cached_ids = {str(c['id']) for c in cached_tracks}
    sc_items_dedup = [t2 for t2 in sc_items if str(t2.get('fileId') or t2.get('id') or '') not in cached_ids]
    for c in cached_tracks:
        c['is_downloaded'] = True
    items = cached_tracks + sc_items_dedup

    pages_all = (search_data.get('paginationInfo') or {}).get('lastPage', 1)
    safe_q = _safe_q(query)
    await status_message.edit_text(
        t('results_for', query=escape(query)),
        reply_markup=await kbs.get_search_kb(items, safe_q, 1, pages_all, f'sq:{safe_q}:1'),
    )
    await nav_db.set_last_cb(message.from_user.id, f'srp|{safe_q}|1')
