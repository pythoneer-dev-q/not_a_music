import asyncio
from hashlib import md5
from html import escape

from aiogram import Router, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    BufferedInputFile, CallbackQuery, InlineQuery, InlineQueryResultArticle,
    InputMediaAudio, InputTextMessageContent,
)

from core.app_logic import downloader, TelegramProgressReporter
from database import music_db
from utils import kbs, messages
from utils.i18n import t
from utils.kbs import DEFAULT_COVER

xrouter = Router()


@xrouter.inline_query(F.query.startswith("playlist_"))
async def inline_playlist_handler(inline_query: InlineQuery):
    playlist_id = inline_query.query.replace("playlist_", "", 1)
    playlist = await music_db.search_playlistByShortId(playlist_id)
    if not playlist or not isinstance(playlist, dict):
        return

    title = playlist.get('pl_name', 'Без названия')
    tracks_count = len(playlist.get('tracks') or [])
    bot_user = (await inline_query.bot.get_me()).username

    results = [
        InlineQueryResultArticle(
            id=md5(playlist_id.encode()).hexdigest(),
            title=f"🎵 Плейлист: {title}",
            description=t('pl_tracks_count', n=tracks_count),
            input_message_content=InputTextMessageContent(
                message_text=t('pl_share_text', name=escape(str(title)), n=tracks_count),
                parse_mode="HTML",
            ),
            reply_markup=await kbs.inline_pllist(playlist, bot_user),
        )
    ]
    await inline_query.answer(results, cache_time=10, is_personal=True)


@xrouter.inline_query(F.query)
async def searcher_inline(inline_query: InlineQuery):
    query = inline_query.query.strip()
    if len(query) < 2:
        return await inline_query.answer([], cache_time=5, is_personal=True)

    cached_task = asyncio.create_task(music_db.search_downloaded_tracks(query, limit=3))
    sc_task = asyncio.create_task(downloader.search_track(query))
    cached_tracks = await cached_task
    sc_result = await sc_task

    sc_items = []
    if sc_result and isinstance(sc_result, dict):
        for term in sc_result.get('items', []):
            track_id = str(term.get('fileId') or term.get('id') or '')
            if any(str(c['id']) == track_id for c in cached_tracks):
                continue
            sc_items.append(term)

    for term in sc_items:
        track_id = str(term.get('fileId') or term.get('id') or '')
        asyncio.create_task(music_db.register_request(
            title=term.get('title'),
            name=term.get('artist'),
            cover=(term.get('imageInfo') or {}).get('imageUrl', DEFAULT_COVER),
            query_str=term.get('query_str') or f"{term.get('artist')} - {term.get('title')}",
            _id=track_id,
            artist=term.get('artist'),
            duration=term.get('duration') or 0,
            url=term.get('url') or '',
        ))

    res_list = []

    for track in cached_tracks:
        track_id = str(track['id'])
        artist = escape(str(track.get('artist') or track.get('title') or 'Unknown'))
        title = escape(str(track.get('title') or track.get('name') or 'Unknown'))
        cover = (track.get('imageInfo') or {}).get('imageUrl') or DEFAULT_COVER
        label = f"⚡ {artist} — {title}"

        res_list.append(InlineQueryResultArticle(
            id=md5(f"c_{track_id}".encode()).hexdigest(),
            title=label[:60],
            description="✅ Уже загружен — мгновенно",
            thumbnail_url=cover,
            input_message_content=InputTextMessageContent(
                message_text=t('inline_press_to_load', query=escape(f"{artist} — {title}")),
                parse_mode="HTML",
            ),
            reply_markup=await kbs.back_to(f'in_dl_{track_id}', t('btn_dl')),
        ))

    for term in sc_items:
        track_id = str(term.get('fileId') or term.get('id') or '')
        title_raw = term.get('title') or 'Unknown'
        artist_raw = term.get('artist') or 'SoundCloud'
        cover = (term.get('imageInfo') or {}).get('imageUrl', DEFAULT_COVER)
        dur = int(term.get('duration', 0))
        label = f"🎵 {artist_raw} — {title_raw}"

        res_list.append(InlineQueryResultArticle(
            id=md5(track_id.encode()).hexdigest(),
            title=label[:60],
            description=f"SoundCloud · {dur // 60}:{dur % 60:02d}",
            thumbnail_url=cover,
            input_message_content=InputTextMessageContent(
                message_text=t('inline_press_to_load', query=escape(f"{artist_raw} — {title_raw}")),
                parse_mode="HTML",
            ),
            reply_markup=await kbs.back_to(f'in_dl_{track_id}', t('btn_dl')),
        ))

    await inline_query.answer(res_list, cache_time=10, is_personal=True)


@xrouter.callback_query(F.data.startswith("in_dl_"), F.inline_message_id)
async def play_track_inline(call: CallbackQuery):
    await call.answer()

    t_id = call.data[len('in_dl_'):]
    imid = call.inline_message_id

    try:
        data = await music_db.search_track_musicDb(t_id)

        if data and data.get('file'):
            artist = escape(str(data.get('artist') or data.get('title') or 'Unknown'))
            track = escape(str(data.get('title') or data.get('name') or ''))
            caption = f"🎵 <b>{artist}</b> — {track}"
            audio = InputMediaAudio(media=data['file'], caption=caption, parse_mode="HTML")
            try:
                await call.bot.edit_message_media(inline_message_id=imid, media=audio)
                return
            except TelegramBadRequest as e:
                return await call.bot.edit_message_text(
                    inline_message_id=imid,
                    text=f"{caption}\n<i>Не удалось прикрепить аудио: {str(e)[:60]}</i>",
                    parse_mode="HTML",
                )

        async def _edit_text(text: str):
            try:
                await call.bot.edit_message_text(
                    inline_message_id=imid, text=text, parse_mode="HTML"
                )
            except Exception:
                pass

        reporter = TelegramProgressReporter(_edit_text, prefix="⏬ <b>Загружаю трек…</b>\n")
        await _edit_text(t('uploading'))

        buf = await downloader.download_track(t_id, progress_cb=reporter.update)
        content = buf.read()

        search_data = await music_db.get_request_by_id(t_id)
        artist_val = (search_data or {}).get('artist') or (search_data or {}).get('name') or 'SoundCloud'
        title_val = (search_data or {}).get('title') or 'Unknown'
        cover_url = (search_data or {}).get('cover')

        img_content = None
        if cover_url:
            try:
                img_content = await downloader.download_with_retry(cover_url)
            except Exception:
                pass

        temp_msg = await call.bot.send_audio(
            chat_id=call.from_user.id,
            audio=BufferedInputFile(content, filename=f"{t_id}.mp3"),
            performer=str(artist_val),
            title=str(title_val),
            thumbnail=BufferedInputFile(img_content, filename="thumb.jpg") if img_content else None,
        )

        f_id = temp_msg.audio.file_id
        try:
            await call.bot.delete_message(chat_id=call.from_user.id, message_id=temp_msg.message_id)
        except Exception:
            pass

        caption = f"🎵 <b>{escape(str(artist_val))}</b> — {escape(str(title_val))}"
        audio_media = InputMediaAudio(media=f_id, caption=caption, parse_mode="HTML")

        try:
            await call.bot.edit_message_media(inline_message_id=imid, media=audio_media)
        except TelegramBadRequest as e:
            await call.bot.edit_message_text(
                inline_message_id=imid,
                text=f"{caption}\n<i>Не удалось прикрепить аудио: {str(e)[:60]}</i>",
                parse_mode="HTML",
            )

        await music_db.register_track(
            shortId=t_id,
            file_id=f_id,
            title=title_val,
            artist=artist_val,
            name=title_val,
            duration=(search_data or {}).get('duration') or 0,
            cover=cover_url,
            url=(search_data or {}).get('url') or '',
        )

    except Exception as e:
        await call.answer(f"⚠️ {str(e)[:50]}", show_alert=True)
