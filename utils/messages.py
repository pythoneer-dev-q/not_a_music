from aiogram import html
from html import escape

from utils.i18n import t


async def main_greet_sub(user: str, source: str = None):
    return t('greet', name=html.bold(html.unparse(user)), source=source or '🤖')


async def subchannels() -> str:
    return t('sub_needed')


async def thank_you() -> str:
    return "✅"


async def main_menu() -> str:
    return t('main_menu')


async def search_message() -> str:
    return t('search_prompt')


async def search_progress(query: str) -> str:
    return t('searching', query=escape(query))


async def render_pllist(pl_data: dict, bot_name: str) -> str:
    tracks = pl_data.get('tracks', []) or []
    return (
        f"<b>⚡️ Плейлист: {escape(str(pl_data.get('pl_name')))}</b> (<code>{pl_data.get('_id')}</code>)\n\n"
        f"<b>🌐 Информация по плейлисту</b>\n"
        + html.expandable_blockquote(
            f"<b>🎵 {t('pl_tracks_count', n=len(tracks))}</b>\n"
            f"<b>⏰ Создан:</b> <code>{pl_data.get('created_at_msk')}</code>\n"
            f"<b>⏰ Обновлен:</b> {pl_data.get('updated_at_msk')}\n"
            f"<b>👤 Создатель:</b> {pl_data.get('founder_id')}"
        ) + "\n\n"
        f"<b>🔗 Постоянная ссылка на плейлист:</b>\n"
        f"<code>https://t.me/{bot_name}?start=pl_{pl_data.get('_id')}</code>"
    )


def _profile_block(user_data: dict) -> str:
    about = (user_data.get('about') or '').strip()
    about_line = f"\n<b>✏️ О себе:</b> {escape(about)}" if about else ""
    return (
        f"<b>🆔 ID:</b> <code>{user_data.get('_id')}</code>\n"
        f"<b>✈️ Telegram:</b> {user_data.get('user_id')}\n"
        f"<b>🔗 Источник:</b> {escape(str(user_data.get('source')))}\n"
        f"<b>🚥 Статус:</b> {user_data.get('user_status')}"
        f"{about_line}"
    )


async def my_profile(user_data: dict, bot_username: str) -> str:
    """Собственный профиль (с настройками)."""
    text = (
        f"👤 {html.bold(escape(str(user_data.get('user_name'))))}\n\n"
        f"<b>🌐 Информация по пользователю:</b>\n"
        + html.expandable_blockquote(_profile_block(user_data))
        + f"\n\n🖇 <b>Ссылка на профиль:</b>\n"
        f"<code>https://t.me/{bot_username}?start=us_{user_data.get('_id')}</code>"
    )
    if user_data.get('is_hidden'):
        text += f"\n\n{t('profile_hidden_self')}"
    return text


async def show_profile(user_data: dict, bot_username: str) -> str:
    """Чужой профиль (deep-link us_). Уважает скрытие профиля."""
    if user_data.get('is_hidden'):
        return t('profile_hidden')
    return (
        f"👤 {html.bold(escape(str(user_data.get('user_name'))))}\n\n"
        f"<b>🌐 Информация по пользователю:</b>\n"
        + html.expandable_blockquote(_profile_block(user_data))
        + f"\n\n🖇 <b>Ссылка на профиль:</b>\n"
        f"<code>https://t.me/{bot_username}?start=us_{user_data.get('_id')}</code>"
    )