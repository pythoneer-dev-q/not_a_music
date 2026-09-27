import asyncio
import time
from typing import Union

from aiogram import types
from aiogram.exceptions import TelegramBadRequest

import database.bot_db as bot_db
from utils import kbs
from utils.i18n import t

# Ссылки на фоновые задачи держим здесь же — иначе GC уничтожит задачу до
# завершения и в логах появится «Task was destroyed but it is pending».
_background: set = set()


def spawn(coro):
    """Запуск фоновой задачи с удержанием ссылки. Возвращает Task."""
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


async def edit_or_resend(message: types.Message, text: str, reply_markup=None):
    """Безопасное редактирование сообщения текстом.

    Если сообщение — аудио/медиа (Telegram: 'there is no text in the message
    to edit'), оно НЕ удаляется, а раздел отправляется НОВЫМ сообщением —
    так открытый с поиска/топа трек остается в чате.
    Возвращает сообщение для дальнейших редактирований или None.
    """
    try:
        return await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if 'message is not modified' in str(e).lower():
            return message
    except Exception:
        pass
    try:
        return await message.answer(text, reply_markup=reply_markup)
    except Exception:
        return None


_ACCT_TTL = 60.0  # сек: успешная проверка кэшируется (get_chat_member + БД — дорогой последовательный путь)
_acct_cache: dict = {}


async def checkAccount(instance: Union[types.Message, types.CallbackQuery], user_id: int):
    """Проверка подписки на каналы и статуса пользователя.

    Успешный ('OK') результат кэшируется на _ACCT_TTL секунд — проверка
    выполняется при каждом нажатии кнопки трека, а get_chat_member в
    Telegram + запросы БД дают основной последовательный оверхед.
    Негативные результаты не кэшируются (разбан/подписка подхватываются сразу).
    """
    user_id = int(user_id)
    cached = _acct_cache.get(user_id)
    if cached:
        expires, result = cached
        if time.monotonic() < expires:
            return result
    try:
        async for channel in bot_db.get_channels():
            member = await instance.bot.get_chat_member(chat_id=channel.get('chat_id'), user_id=user_id)
            if member.status not in ['member', 'administrator', 'creator']:
                text, kb = t('sub_needed'), await kbs.sub_channels()
                if isinstance(instance, types.CallbackQuery):
                    try:
                        return await instance.message.edit_text(text, reply_markup=kb)
                    except Exception:
                        return await instance.message.answer(text, reply_markup=kb)
                return await instance.answer(text, reply_markup=kb)

        user = await bot_db.search_user(user_id)
        if user and user.get('user_status') != 'OK':
            text = t('banned')
            if isinstance(instance, types.CallbackQuery):
                return await instance.message.edit_text(text)
            return await instance.answer(text)

        _acct_cache[user_id] = (time.monotonic() + _ACCT_TTL, 'OK')
        return 'OK'
    except Exception:
        return await instance.answer(t('error_generic'))