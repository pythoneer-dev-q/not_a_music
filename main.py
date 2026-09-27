import asyncio
import re
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.utils.token import TokenValidationError

from utils import app_config
from utils.i18n import I18nMiddleware
from handlers import admin, handlers, service_handlers, inline_handlers
from core import app_logic
from database import music_db


dp = Dispatcher()

TOKEN_RE = re.compile(r'^\d{6,}:[A-Za-z0-9_-]{30,}$')

TOKEN_ERROR_TEXT = (
    "❌ BOT_TOKEN недействителен: Telegram ответил «Unauthorized».\n"
    "Что делать:\n"
    "  1. Получите новый токен у @BotFather (/mybots → API Token).\n"
    "  2. Пропишите его в .env:  BOT_TOKEN='123456:AA...'\n"
    "     либо выполните:  BOT_TOKEN='123456:AA...' bash deploy.sh\n"
    "  3. Перезапустите бота:  systemctl restart not_a_music\n"
)


def _validate_token() -> bool:
    token = (app_config.Config.tg_token or "").strip()
    if not token:
        print("❌ BOT_TOKEN не задан. Укажите его в .env (BOT_TOKEN='...') и повторите запуск.")
        return False
    if not TOKEN_RE.match(token):
        print("❌ BOT_TOKEN имеет неверный формат. Ожидается строка вида 123456:AA... (см. @BotFather).")
        return False
    return True


async def main() -> int:
    if not _validate_token():
        return 1

    try:
        bot = Bot(token=app_config.Config.tg_token,
                  default=DefaultBotProperties(parse_mode='HTML'))
    except (TokenValidationError, ValueError) as e:
        print(f"❌ BOT_TOKEN отклонён Telegram-библиотекой: {e}")
        print("❌ Проверьте значение BOT_TOKEN в .env.")
        return 1

    # создаём индексы MongoDB при старте (idempotent)
    await music_db.init_indexes()

    try:
        async with app_logic.downloader:  # aiohttp-сессия живёт весь цикл бота
            dp.include_routers(admin.crouter, handlers.arouter, service_handlers.zrouter, inline_handlers.xrouter)

            dp.message.middleware(I18nMiddleware())
            dp.callback_query.middleware(I18nMiddleware())
            dp.inline_query.middleware(I18nMiddleware())

            try:
                await bot.delete_webhook(drop_pending_updates=True)
            except TelegramUnauthorizedError:
                print(TOKEN_ERROR_TEXT)
                return 1
            except Exception as e:
                # Вебхук не критичен для long-polling — просто предупреждаем.
                print(f"⚠ Не удалось сбросить вебхук: {e}")

            try:
                await dp.start_polling(bot)
            except TelegramUnauthorizedError:
                print(TOKEN_ERROR_TEXT)
                return 1
    finally:
        # иначе aiohttp ругается «Unclosed client session»
        await bot.session.close()
    return 0


if __name__ == '__main__':
    exit_code = 0
    try:
        exit_code = asyncio.run(main()) or 0
    except KeyboardInterrupt:
        exit_code = 0
    sys.exit(exit_code)

