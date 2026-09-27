import asyncio
import time
from html import escape

from aiogram import Router, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, InputMediaAudio

from core.app_logic import downloader, TelegramProgressReporter
from database import actions_db, bot_db, music_db, nav_db, settings_db
from handlers import fsm_classes
from handlers.handlers import render_search
from utils import kbs, messages, routes, utils
from utils.i18n import t, set_user_lang

zrouter = Router()


def _back_kb(origin: str) -> str:
    if origin.startswith('pl:'):
        _, pl_id, page = origin.split(':')
        return f'plv:{pl_id}:{page}'
    if origin.startswith('fav:'):
        return origin  # {page}
    if origin == 'top':
        return 'user_topTracks'
    return 'user_menu'



@zrouter.callback_query(F.data.regexp(r'^n[A-Za-z0-9]{10}$'))
async def packed_callback(call: CallbackQuery):
    payload = await nav_db.unpack(call.data[1:])
    if not payload:
        return await call.answer(t('cb_expired'), show_alert=True)
    await call.answer()
    if not await routes.dispatch(call, payload):
        await call.answer()


@zrouter.callback_query(F.data == 'nt')
async def noop(call: CallbackQuery):
    await call.answer()


@zrouter.callback_query(F.data.startswith('dl|'))
@routes.route('dl|')
async def play_track(call: CallbackQuery, data: str = None):
    data = data or call.data
    if await utils.checkAccount(call, call.from_user.id) != 'OK':
        return
    try:
        _, origin, arg = data.split('|', 2)
    except ValueError:
        return await call.answer()
    await call.answer()

    t_id = arg
    playlist_id = page = index = None
    pl_data = None

    if arg.startswith('next:'):
        if not origin.startswith('pl:'):
            return
        _, playlist_id, page = origin.split(':')
        index = int(arg.split(':')[1])
        pl_data = await music_db.search_playlistByShortId(playlist_id)
        tracks = [tr for tr in (pl_data.get('tracks') or []) if isinstance(tr, dict)] if pl_data else []
        if index >= len(tracks):
            return await call.answer(t('pl_end'), show_alert=True)
        t_id = str(tracks[index].get('_id'))
    elif origin.startswith('pl:'):
        _, playlist_id, page = origin.split(':')

    if await _maybe_show_ad(call):
        return

    next_payload = None
    if playlist_id:
        pl_data = pl_data or await music_db.search_playlistByShortId(playlist_id)
        tracks = [tr for tr in (pl_data.get('tracks') or []) if isinstance(tr, dict)] if pl_data else []
        cur = index if arg.startswith('next:') else next(
            (i for i, tr in enumerate(tracks) if str(tr.get('_id')) == str(t_id)), None)
        if cur is not None and cur + 1 < len(tracks):
            next_payload = f'dl|pl:{playlist_id}:{page}|next:{cur + 1}'

    await _deliver_track(call, t_id, origin, next_payload)


async def _maybe_show_ad(call: CallbackQuery) -> bool:
    ad_every = int(await settings_db.get('AD_EVERY_N'))
    if ad_every <= 0:
        return False

    # параллелим incr_played и get_ads_random — они независимы
    played_task = asyncio.create_task(bot_db.incr_played(call.from_user.id))
    ad_task = asyncio.create_task(bot_db.get_ads_random())

    played = await played_task
    if played % ad_every != 0:
        return False

    ad = await ad_task
    if not ad:
        return False

    ad_text = t('ad_msg', text=ad.get('text', ''), n=ad_every)
    go_payload = await nav_db.get_last_cb(call.from_user.id) or 'user_menu'
    kb = await kbs.ad_continue_kb(go_payload)

    msg = await utils.edit_or_resend(call.message, ad_text)
    await asyncio.sleep(5)
    if msg is not None:
        try:
            await msg.edit_text(ad_text, reply_markup=kb)
        except TelegramBadRequest as e:
            if 'message is not modified' not in str(e).lower():
                try:
                    await msg.edit_reply_markup(reply_markup=kb)
                except Exception:
                    pass
        except Exception:
            pass
    return True


async def _deliver_track(call: CallbackQuery, t_id: str, origin: str, next_payload: str = None):
    _t0 = _tp = time.perf_counter()
    _st = {}

    def _mark(name):
        nonlocal _tp
        _st[name] = round(time.perf_counter() - _tp, 2)
        _tp = time.perf_counter()

    t_id = str(t_id)
    back_cb = _back_kb(origin)
    asyncio.create_task(actions_db.register_view(t_id))

    # кэш-хит: file_id уже есть — мгновенная отдача
    data = await music_db.search_track_musicDb(t_id)
    _mark('db_cache')

    if data and data.get('file'):
        artist = data.get('artist') or data.get('title') or 'Unknown'
        title  = data.get('title') or data.get('name') or 'Unknown'
        caption = f"🎵 <b>{escape(str(artist))}</b> — {escape(str(title))}"
        kb = await kbs.track_kb(t_id, call.from_user.id, back_cb=back_cb, next_payload=next_payload)
        audio = InputMediaAudio(media=data['file'], caption=caption, parse_mode='HTML')
        try:
            await call.message.edit_media(media=audio, reply_markup=kb)
        except Exception:
            # Если это было текстовое сообщение поиска или уже с другим медиа
            try:
                await call.message.delete()
                await call.message.answer_audio(
                    audio=data['file'],
                    performer=str(artist),
                    title=str(title),
                    caption=caption,
                    parse_mode='HTML',
                    reply_markup=kb
                )
            except Exception:
                await call.answer(t('already_open'))
        _mark('tg_send')
        print(f"[perf] cached: {_st}")
        return

    # --- не в кэше: скачиваем с SoundCloud ---
    status_msg = await utils.edit_or_resend(call.message, t('uploading'))
    if status_msg is None:
        return
    kb = await kbs.track_kb(t_id, call.from_user.id, back_cb=back_cb, next_payload=next_payload)
    _mark('prep')

    async def _edit_progress(text: str):
        try:
            await status_msg.edit_text(text, parse_mode='HTML')
        except Exception:
            pass

    reporter = TelegramProgressReporter(_edit_progress, prefix="⏬ <b>Загружаю трек…</b>\n")

    try:
        buf = await downloader.download_track(t_id, progress_cb=reporter.update)
        content = buf.read()
        _mark('download')

        search_data = await music_db.get_request_by_id(t_id)
        artist = (search_data or {}).get('artist') or (search_data or {}).get('name') or 'SoundCloud'
        title  = (search_data or {}).get('title') or 'Unknown'
        cover_url = (search_data or {}).get('cover')
        duration = (search_data or {}).get('duration') or 0
        url = (search_data or {}).get('url') or ''

        img_content = None
        if cover_url:
            try:
                img_content = await downloader.download_with_retry(cover_url)
            except Exception:
                pass

        caption = f"🎵 <b>{escape(str(artist))}</b> — {escape(str(title))}"
        audio = InputMediaAudio(
            media=BufferedInputFile(content, filename=f"{t_id}.mp3"),
            performer=str(artist),
            title=str(title),
            thumbnail=BufferedInputFile(img_content, filename="thumb.jpg") if img_content else None,
            caption=caption,
            parse_mode='HTML',
        )
        sent = await status_msg.edit_media(media=audio, reply_markup=kb)
        _mark('tg_upload')

        try:
            await music_db.register_track(
                shortId=t_id,
                file_id=sent.audio.file_id,
                title=title,
                name=title,
                artist=artist,
                duration=duration,
                cover=cover_url,
                url=url,
            )
        except Exception as db_err:
            print(f"DB Error: {db_err}")
        _mark('db_save')
        print(f"[perf] full: {_st} (total={round(time.perf_counter() - _t0, 2)}s)")

    except Exception as e:
        print(f"Download Error ({t_id}): {e}")
        err_text = "?? ???? ???? ??????? ???????????????? (SoundCloud Go+) ? ?????????? ??? ??????????? ??????????." if "DRM_OR_NOT_FOUND" in str(e) else t('error_generic')
        try:
            await status_msg.edit_text(err_text, parse_mode='HTML')
        except Exception:
            pass
        await call.answer("?? ???? ??????? ????????????????" if "DRM_OR_NOT_FOUND" in str(e) else t('error_generic'), show_alert=True)


@zrouter.callback_query(F.data == 'call_to_bot')
async def caller_bot(call: CallbackQuery):
    await call.answer(t('inline_listen_bot'), show_alert=True)


async def _rebuild_track_kb(call: CallbackQuery, t_id: str, back_cb: str, next_payload: str):
    kb = await kbs.track_kb(
        t_id, call.from_user.id,
        back_cb=back_cb,
        next_payload=None if next_payload == '-' else next_payload,
    )
    try:
        await call.message.edit_reply_markup(reply_markup=kb)
    except Exception:
        pass


@zrouter.callback_query(F.data.startswith('lk|'))
@routes.route('lk|')
async def like_track(call: CallbackQuery, data: str = None):
    data = data or call.data
    parts = data.split('|')
    t_id, back_cb = parts[1], parts[2]
    next_payload = '|'.join(parts[3:]) if len(parts) > 3 else '-'
    is_liked = await actions_db.toggle_like(call.from_user.id, t_id)
    await call.answer(t('liked' if is_liked else 'unliked'))
    await _rebuild_track_kb(call, t_id, back_cb, next_payload)


@zrouter.callback_query(F.data.startswith('dk|'))
@routes.route('dk|')
async def dislike_track(call: CallbackQuery, data: str = None):
    data = data or call.data
    parts = data.split('|')
    t_id, back_cb = parts[1], parts[2]
    next_payload = '|'.join(parts[3:]) if len(parts) > 3 else '-'
    is_disliked = await actions_db.toggle_dislike(call.from_user.id, t_id)
    await call.answer(t('disliked' if is_disliked else 'undisliked'))
    await _rebuild_track_kb(call, t_id, back_cb, next_payload)


@zrouter.callback_query(F.data.startswith('fsv|'))
@routes.route('fsv|')
async def save_track(call: CallbackQuery, data: str = None):
    data = data or call.data
    parts = data.split('|')
    t_id, back_cb = parts[1], parts[2]
    next_payload = '|'.join(parts[3:]) if len(parts) > 3 else '-'
    result = await actions_db.toggle_saved(call.from_user.id, t_id)
    if result == 'full':
        fav_max = int(await settings_db.get('USER_SAVED_LIMIT'))
        await call.answer(t('fav_full', max=fav_max), show_alert=True)
    else:
        await call.answer(t('fav_added' if result == 'added' else 'fav_removed'))
    await _rebuild_track_kb(call, t_id, back_cb, next_payload)


@zrouter.callback_query(F.data.startswith('frem|'))
@routes.route('frem|')
async def remove_from_favs(call: CallbackQuery, data: str = None):
    data = data or call.data
    _, t_id, page = data.split('|')
    await actions_db.toggle_saved(call.from_user.id, t_id)
    await call.answer(t('fav_removed'))
    await _render_favs(call, int(page))


async def _render_favs(call: CallbackQuery, page: int = 1):
    user_id = call.from_user.id
    fav_max = int(await settings_db.get('USER_SAVED_LIMIT'))
    fav_page_size = int(await settings_db.get('FAV_PAGE_SIZE'))
    total = await actions_db.saved_count(user_id)

    if total == 0:
        await utils.edit_or_resend(
            call.message, t('fav_empty'),
            reply_markup=await kbs.back_to('user_menu', t('btn_to_menu')))
        await nav_db.set_last_cb(user_id, 'user_menu')
        return

    pages_all = max(1, -(-total // fav_page_size))
    page = min(max(1, page), pages_all)
    tracks = await actions_db.get_saved(user_id, skip=(page - 1) * fav_page_size, limit=fav_page_size)

    await utils.edit_or_resend(
        call.message,
        t('fav_title', n=total, max=fav_max, p=page, all=pages_all),
        reply_markup=await kbs.fav_kb(page, pages_all, tracks),
    )
    await nav_db.set_last_cb(user_id, f'favs:{page}')


@zrouter.callback_query(F.data.startswith('favs:'))
@routes.route('favs:')
async def favs_page(call: CallbackQuery, data: str = None):
    if await utils.checkAccount(call, call.from_user.id) != 'OK':
        return
    data = data or call.data
    await call.answer()
    await _render_favs(call, int(data.split(':')[1]))



@zrouter.callback_query(F.data == 'prof')
async def my_profile(call: CallbackQuery, state: FSMContext):
    if await utils.checkAccount(call, call.from_user.id) != 'OK':
        return
    await call.answer()
    await state.clear()
    user_data = await bot_db.search_user(call.from_user.id)
    if not user_data:
        return
    await utils.edit_or_resend(
        call.message,
        text=await messages.my_profile(user_data, (await call.bot.get_me()).username),
        reply_markup=await kbs.profile_kb(user_data),
    )


@zrouter.callback_query(F.data == 'prof_vis')
async def toggle_visibility(call: CallbackQuery):
    user_data = await bot_db.search_user(call.from_user.id)
    if not user_data:
        return
    new_hidden = not bool(user_data.get('is_hidden'))
    await bot_db.set_hidden(call.from_user.id, new_hidden)
    await call.answer(t('visibility_off' if new_hidden else 'visibility_on'))

    user_data['is_hidden'] = new_hidden
    try:
        await call.message.edit_reply_markup(reply_markup=await kbs.profile_kb(user_data))
    except TelegramBadRequest:
        pass


@zrouter.callback_query(F.data == 'prof_about')
async def edit_about(call: CallbackQuery, state: FSMContext):
    await state.set_state(fsm_classes.Profile.about)
    await utils.edit_or_resend(
        call.message, t('about_prompt'),
        reply_markup=await kbs.back_to('prof', t('btn_cancel')))
    await call.answer()


@zrouter.callback_query(F.data == 'langsw')
async def switch_lang(call: CallbackQuery):
    user_data = await bot_db.search_user(call.from_user.id)
    current = (user_data or {}).get('lang') or await settings_db.get('DEFAULT_LANG')
    new_lang = 'en' if current == 'ru' else 'ru'
    await set_user_lang(call.from_user.id, new_lang)
    await call.answer(t('lang_switched'))

    user_data = await bot_db.search_user(call.from_user.id)
    try:
        await call.message.edit_reply_markup(reply_markup=await kbs.profile_kb(user_data))
    except TelegramBadRequest:
        pass


@zrouter.callback_query(F.data.startswith('reg_'))
async def user_matcher(call: CallbackQuery):
    match call.data.split('_', maxsplit=1)[-1]:
        case 'rg':
            await call.message.edit_text(t('rules_read'), reply_markup=await kbs.greet_kb())
        case 'reg':
            await call.message.edit_text(t('policy_read'), reply_markup=await kbs.greet_kb_2())
        case 'reg_2':
            await call.message.edit_text(t('sub_prompt'), reply_markup=await kbs.sub_channels())
        case 'reg_3':
            await call.message.answer(t('welcome_menu'))
    await call.answer()


@zrouter.callback_query(F.data.startswith('user_'))
@routes.route('user_')
async def user_section(call: CallbackQuery, data: str = None, state: FSMContext = None):
    data = data or call.data
    if await utils.checkAccount(call, call.from_user.id) != 'OK':
        return

    section = data.split('_', maxsplit=1)[-1]

    if section == 'dev':
        await call.answer('🔺 Данная функция временно недоступна.')
        return
    if section == 'checkSub':
        await utils.edit_or_resend(
            call.message, text=await messages.main_menu(),
            reply_markup=await kbs.main_menu(call.from_user.id))
        await nav_db.set_last_cb(call.from_user.id, 'user_menu')
    elif section == 'menu':
        if state:
            await state.clear()
        await utils.edit_or_resend(
            call.message, text=await messages.main_menu(),
            reply_markup=await kbs.main_menu(call.from_user.id))
        await nav_db.set_last_cb(call.from_user.id, 'user_menu')
    elif section == 'search':
        await utils.edit_or_resend(
            call.message, text=await messages.search_message(),
            reply_markup=await kbs.back_to('user_menu', t('btn_to_menu')))
    elif section == 'topTracks':
        await render_search(call, 'top', 1)
        await nav_db.set_last_cb(call.from_user.id, 'user_topTracks')
    elif section == 'pllists':
        pllists = await music_db.search_playlistById(call.from_user.id)
        text = t('pl_list_ok') if pllists else t('pl_list_empty')
        await utils.edit_or_resend(call.message, text, reply_markup=await kbs.user_playlists(pllists))
        await nav_db.set_last_cb(call.from_user.id, 'user_pllists')

    await call.answer()



async def _render_playlist(call: CallbackQuery, pl_id: str, page: int = 1):
    pl_data = await music_db.search_playlistByShortId(pl_id)
    if not pl_data:
        return await utils.edit_or_resend(
            call.message, t('pl_not_found'),
            reply_markup=await kbs.back_to('user_pllists', t('btn_to_playlists')))
    await music_db.inc_playlist_views(pl_id)
    pl_data['views'] = (pl_data.get('views') or 0) + 1
    await utils.edit_or_resend(
        call.message,
        text=await messages.render_pllist(pl_data, (await call.bot.get_me()).username),
        reply_markup=await kbs.pl_view_kb(pl_data, call.from_user.id, page),
    )
    await nav_db.set_last_cb(call.from_user.id, f'plv:{pl_id}:{page}')


@zrouter.callback_query(F.data.startswith('plv:'))
@routes.route('plv:')
async def view_playlist(call: CallbackQuery, data: str = None):
    if await utils.checkAccount(call, call.from_user.id) != 'OK':
        return
    data = data or call.data
    await call.answer()
    _, pl_id, page = data.split(':')
    await _render_playlist(call, pl_id, int(page))


@zrouter.callback_query(F.data.startswith('plk|'))
async def like_playlist(call: CallbackQuery):
    _, pl_id, page = call.data.split('|')
    is_liked = await music_db.toggle_playlist_like(pl_id, call.from_user.id)
    await call.answer(t('liked' if is_liked else 'unliked'))
    await _render_playlist(call, pl_id, int(page))


@zrouter.callback_query(F.data == 'pl_new')
async def new_playlist(call: CallbackQuery, state: FSMContext):
    if await utils.checkAccount(call, call.from_user.id) != 'OK':
        return
    await state.set_state(fsm_classes.NewPl.title)
    await utils.edit_or_resend(
        call.message, t('pl_name_prompt'),
        reply_markup=await kbs.back_to('user_menu', t('btn_cancel')))
    await call.answer()


@zrouter.callback_query(F.data.startswith('pl_add|'))
@routes.route('pl_add|')
async def choose_playlist_for_track(call: CallbackQuery, data: str = None):
    if await utils.checkAccount(call, call.from_user.id) != 'OK':
        return
    data = data or call.data
    await call.answer()
    t_id = data.split('|', 1)[1]
    pls = await music_db.search_playlistById(call.from_user.id)
    await utils.edit_or_resend(call.message, t('pl_choose'), reply_markup=await kbs.pl_add_kb(pls, t_id))


@zrouter.callback_query(F.data.startswith('pl_up|'))
@routes.route('pl_up|')
async def add_track_to_playlist(call: CallbackQuery, data: str = None):
    data = data or call.data
    _, pl_id, t_id = data.split('|')
    title = await music_db.get_request_by_id(t_id)
    result = await music_db.add_tracks_playlist(pl_id, title, t_id)
    try:
        await call.message.delete()
    except Exception:
        pass
    if result:
        await call.answer(t('pl_track_added'), show_alert=True)
    else:
        tracks_max = int(await settings_db.get('TRACKS_MAX'))
        await call.answer(t('pl_track_full', max=tracks_max), show_alert=True)


@zrouter.callback_query(F.data.startswith('pl_del|'))
@routes.route('pl_del|')
async def del_track_from_playlist(call: CallbackQuery, data: str = None):
    data = data or call.data
    _, pl_id, page, t_id = data.split('|')
    result = await music_db.delete_tracks_playlist(pl_id, t_id)
    await call.answer(t('pl_track_removed') if result else t('error_generic'), show_alert=True)
    await _render_playlist(call, pl_id, int(page))


@zrouter.callback_query(F.data.startswith('pl_pdel|'))
async def delete_playlist(call: CallbackQuery):
    pl_id = call.data.split('|')[1]
    s_id = await music_db.delete_playlist(pl_id)
    await utils.edit_or_resend(
        call.message, t('pl_deleted', id=s_id or pl_id),
        reply_markup=await kbs.back_to('user_pllists', t('btn_to_playlists')))
    await call.answer()


@zrouter.callback_query(F.data.startswith('pl_tdel|'))
async def clear_playlist(call: CallbackQuery):
    pl_id = call.data.split('|')[1]
    s_id = await music_db.delete_allTracks_playlist(pl_id)
    await utils.edit_or_resend(
        call.message, t('pl_cleared', id=s_id or pl_id),
        reply_markup=await kbs.back_to(f'plv:{pl_id}:1', t('btn_to_pl')))
    await call.answer()