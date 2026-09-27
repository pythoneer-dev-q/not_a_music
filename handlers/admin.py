import asyncio
import json

from aiogram import Router, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database import settings_db
from database.bot_db import (
    admins, ads_db, channels, users,
    make_admin, del_admin, new_ads, del_ads, add_channel, del_channel,
    search_admin, search_user, search_userById, block_user,
)
from utils.app_config import Config

crouter = Router()

INT_SETTINGS = (
    'USER_SAVED_LIMIT', 'FAV_PAGE_SIZE', 'TRACKS_MAX',
    'PLAYLISTS_MAX', 'SEARCH_LIMIT', 'AD_EVERY_N',
)


class AdminCB(CallbackData, prefix="adm"):
    action: str
    target: str
    id: str = ""


class AdminState(StatesGroup):
    add_admin = State()
    add_ad = State()
    add_channel = State()
    mass_mail = State()
    user_lookup = State()
    ban_reason = State()
    set_setting = State()


# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

async def send_messages(from_admin: int, text_data: str, bot: Bot):
    try:
        text, buttons_raw = text_data.split('|', 1)
        builder = InlineKeyboardBuilder()

        for name, val in json.loads(buttons_raw).items():
            if val.startswith('ur:'):
                builder.row(InlineKeyboardButton(text=name, url=val.replace('ur:', '')))
            else:
                builder.row(InlineKeyboardButton(text=name, callback_data=val))
        kb = builder.as_markup()
    except Exception as e:
        return await bot.send_message(from_admin, f"❌ Ошибка парсинга JSON/Текста: {e}")

    counter = 0
    status_msg = await bot.send_message(from_admin, "🚀 Рассылка запущена...")

    async for user in users.find({}):
        try:
            await bot.send_message(chat_id=user['user_id'], text=text.strip(), reply_markup=kb)
            counter += 1
            if counter % 50 == 0:
                await status_msg.edit_text(f"⏳ Отправлено: {counter}...")
                await asyncio.sleep(0.05)  # пауза для обхода лимитов
        except Exception:
            continue

    await status_msg.edit_text(f"✅ Готово! Сообщение получили {counter} пользователей.")


def _user_card_text(user: dict) -> str:
    ban_info = ''
    if user.get('user_status') == 'BAN':
        ban_info = f"\n<b>Причина бана:</b> {user.get('ban_reason', '—')}"
    return (
        f"👤 <b>Пользователь:</b> {user.get('user_name')}\n\n"
        f"<b>Telegram ID:</b> <code>{user.get('user_id')}</code>\n"
        f"<b>ID профиля:</b> <code>{user.get('_id')}</code>\n"
        f"<b>Источник:</b> {user.get('source', '—')}\n"
        f"<b>Статус:</b> {user.get('user_status')}\n"
        f"<b>Язык:</b> {user.get('lang') or 'auto'}\n"
        f"<b>Прослушано треков:</b> {user.get('played_count', 0)}\n"
        f"<b>Профиль скрыт:</b> {'да' if user.get('is_hidden') else 'нет'}\n"
        f"<b>О себе:</b> {user.get('about') or '—'}"
        f"{ban_info}"
    )


def _user_card_kb(user: dict) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    uid = user.get('user_id')
    if user.get('user_status') == 'BAN':
        kb.button(text='✅ Разблокировать',
                  callback_data=AdminCB(action='unblock', target='user', id=str(uid)))
    else:
        kb.button(text='🚫 Заблокировать',
                  callback_data=AdminCB(action='block', target='user', id=str(uid)))
    kb.button(text='🔍 Найти другого', callback_data=AdminCB(action='nav', target='user'))
    kb.adjust(1)
    return kb


async def _present_user_card(message: Message, user: dict, edit: bool = False):
    text = _user_card_text(user)
    kb = _user_card_kb(user).as_markup()
    if edit:
        try:
            return await message.edit_text(text, reply_markup=kb)
        except Exception:
            pass
    await message.answer(text, reply_markup=kb)


async def _render_settings(call: CallbackQuery):
    vals = await settings_db.get_all()
    kb = InlineKeyboardBuilder()
    for key in settings_db.ALL_KEYS:
        kb.button(text=f"⚙️ {key}: <b>{vals.get(key)}</b>",
                  callback_data=AdminCB(action='set', target=key))
    kb.button(text='♻️ Сбросить к значениям .env',
              callback_data=AdminCB(action='reset', target='settings'))
    kb.adjust(1)
    await call.message.edit_text(
        "⚙️ <b>Настройки бота</b>\n\n<i>Значения хранятся в БД и перекрывают .env. "
        "Нажмите на параметр, чтобы изменить.</i>",
        reply_markup=kb.as_markup(),
    )


# --- ХЕНДЛЕРЫ ---

@crouter.message(F.text == '!admin')
async def admin_main_menu(message: Message):
    if message.from_user.id != Config.SUDO_ADMIN and not await search_admin(message.from_user.id):
        return

    kb = InlineKeyboardBuilder()
    kb.button(text="👥 Админы", callback_data=AdminCB(action='list', target='admin'))
    kb.button(text="🔍 Пользователи", callback_data=AdminCB(action='nav', target='user'))
    kb.button(text="📢 Реклама", callback_data=AdminCB(action='list', target='ad'))
    kb.button(text="📺 Каналы", callback_data=AdminCB(action='list', target='channel'))
    kb.button(text="✉️ Рассылка", callback_data=AdminCB(action='add', target='mail'))
    kb.button(text="⚙️ Настройки", callback_data=AdminCB(action='list', target='settings'))
    kb.adjust(1)
    await message.answer("🛠 Панель управления:", reply_markup=kb.as_markup())


@crouter.callback_query(AdminCB.filter())
async def admin_callbacks(call: CallbackQuery, callback_data: AdminCB, state: FSMContext):
    await state.clear()
    action, target, cb_id = callback_data.action, callback_data.target, callback_data.id

    if action == 'del':
        match target:
            case 'admin': await del_admin(int(cb_id), call.from_user.id)
            case 'ad': await del_ads(cb_id)
            case 'channel': await del_channel(cb_id)
        action, target = 'list', target  

    if action == 'list':
        if target == 'settings':
            return await _render_settings(call)

        kb = InlineKeyboardBuilder()
        match target:
            case 'admin':
                text = "Список администраторов:"
                async for doc in admins.find({}):
                    kb.button(text=f"❌ {doc['user_id']}",
                              callback_data=AdminCB(action='del', target='admin', id=str(doc['user_id'])))
            case 'ad':
                text = "Рекламные кнопки:"
                async for doc in ads_db.find({}):
                    kb.button(text=f"❌ {doc['text']}",
                              callback_data=AdminCB(action='del', target='ad', id=doc['_id']))
            case 'channel':
                text = "Список каналов (ОП):"
                async for doc in channels.find({}):
                    kb.button(text=f"❌ {doc.get('url', 'Канал')}",
                              callback_data=AdminCB(action='del', target='channel', id=doc['_id']))

        kb.button(text="➕ Добавить", callback_data=AdminCB(action='add', target=target))
        kb.adjust(1)
        return await call.message.edit_text(text, reply_markup=kb.as_markup())

    if action == 'nav' and target == 'user':
        await state.set_state(AdminState.user_lookup)
        return await call.message.edit_text(
            "🔍 Отправьте ID пользователя:\n"
            "<i>Telegram ID (число) или ID профиля (из deep-link us_...)</i>")

    if action == 'card' and target == 'user':
        user = await _find_user(cb_id)
        if not user:
            return await call.message.edit_text("❌ Пользователь не найден")
        return await _present_user_card(call.message, user, edit=True)

    if action == 'block' and target == 'user':
        await state.update_data(ban_target=cb_id)
        await state.set_state(AdminState.ban_reason)
        return await call.message.edit_text(f"🚫 Введите причину блокировки для <code>{cb_id}</code>:")

    if action == 'unblock' and target == 'user':
        await users.update_one(
            {'user_id': int(cb_id)},
            {'$set': {'user_status': 'OK', 'ban_reason': None, 'ban_id': None}})
        user = await search_user(int(cb_id))
        if user:
            return await _present_user_card(call.message, user, edit=True)
        return await call.message.edit_text("❌ Пользователь не найден")
    
    if action == 'set' and target in settings_db.ALL_KEYS:
        vals = await settings_db.get_all()
        await state.update_data(setting_key=target)
        await state.set_state(AdminState.set_setting)
        return await call.message.edit_text(
            f"⚙️ Настройка: <code>{target}</code>\n"
            f"Текущее значение: <b>{vals.get(target)}</b>\n\n"
            "<i>Отправьте новое значение (для числовых — целое число).</i>")

    if action == 'reset' and target == 'settings':
        await settings_db.reset()
        return await _render_settings(call)

    if action == 'add':
        match target:
            case 'admin':
                await state.set_state(AdminState.add_admin)
                msg = "Введите ID нового админа:"
            case 'ad':
                await state.set_state(AdminState.add_ad)
                msg = "Формат: Текст кнопки | ссылка"
            case 'channel':
                await state.set_state(AdminState.add_channel)
                msg = "Формат: ссылка | chat_id"
            case 'mail':
                await state.set_state(AdminState.mass_mail)
                msg = "Введите текст и кнопки.\nПример: Текст | {\"Кнопка\": \"ur:https://ya.ru\"}"
        await call.message.edit_text(msg)


async def _find_user(ident: str):
    if ident.isdigit():
        user = await search_user(int(ident))
        if user:
            return user
    return await search_userById(ident)



@crouter.message(AdminState.user_lookup)
async def process_user_lookup(message: Message, state: FSMContext):
    await state.clear()
    user = await _find_user(message.text.strip())
    if not user:
        return await message.answer("❌ Пользователь не найден. Проверьте ID и попробуйте заново (!admin).")
    await _present_user_card(message, user)


@crouter.message(AdminState.ban_reason)
async def process_ban(message: Message, state: FSMContext):
    data = await state.get_data()
    target = data.get('ban_target')
    await state.clear()
    if not target or not target.isdigit():
        return await message.answer("❌ Ошибка: пользователь не выбран. Начните заново через !admin")
    await block_user(int(target), message.text.strip())
    user = await search_user(int(target))
    if user:
        await _present_user_card(message, user)
    else:
        await message.answer(f"✅ Пользователь <code>{target}</code> заблокирован.")


@crouter.message(AdminState.set_setting)
async def process_setting(message: Message, state: FSMContext):
    data = await state.get_data()
    key = data.get('setting_key')
    if not key:
        await state.clear()
        return await message.answer("❌ Ошибка: настройка не выбрана. Начните заново через !admin")

    raw = message.text.strip()
    if key in INT_SETTINGS:
        if not raw.lstrip('-').isdigit():
            return await message.answer("❌ Введите целое число.")
        value = int(raw)
    elif key == 'DEFAULT_LANG':
        if raw not in ('ru', 'en'):
            return await message.answer("❌ Допустимые значения: ru, en")
        value = raw
    else:
        value = raw

    await settings_db.set_key(key, value)
    await state.clear()
    await message.answer(
        f"✅ <code>{key}</code> = <b>{value}</b>\n\n<i>Применяется сразу, перезапуск не нужен.</i>")


@crouter.message(AdminState.add_admin)
async def process_add_admin(message: Message, state: FSMContext):
    if message.text.isdigit():
        await make_admin(int(message.text), message.from_user.id)
        await message.answer("✅ Админ добавлен")
    await state.clear()


@crouter.message(AdminState.add_ad)
async def process_add_ad(message: Message, state: FSMContext):
    if (len(tmp := message.text.split('|')) == 2):
        await new_ads(tmp[1].strip(), tmp[0].strip())
        await message.answer("✅ Реклама добавлена")
    await state.clear()


@crouter.message(AdminState.add_channel)
async def process_add_channel(message: Message, state: FSMContext):
    if (len(tmp := message.text.split('|')) == 2) and tmp[1].strip().lstrip('-').isdigit():
        await add_channel(tmp[0].strip(), int(tmp[1].strip()))
        await message.answer("✅ Канал добавлен")
    await state.clear()


@crouter.message(AdminState.mass_mail)
async def process_mass_mail(message: Message, state: FSMContext):
    await state.clear()
    if '|' not in message.text:
        return await message.answer("❌ Неверный формат (отсутствует |)")

    await send_messages(message.from_user.id, message.text, message.bot)