import asyncio

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from utils import app_config
from utils.i18n import I18nMiddleware
from handlers import admin, handlers, service_handlers, inline_handlers
from core import app_logic
from database import music_db


bot = Bot(token=app_config.Config.tg_token, default=DefaultBotProperties(parse_mode='HTML'))
dp = Dispatcher()


async def main():
    # создаём индексы MongoDB при старте (idempotent)
    await music_db.init_indexes()

    async with app_logic.downloader:  # aiohttp-сессия живёт весь цикл бота
        dp.include_routers(admin.crouter, handlers.arouter, service_handlers.zrouter, inline_handlers.xrouter)

        dp.message.middleware(I18nMiddleware())
        dp.callback_query.middleware(I18nMiddleware())
        dp.inline_query.middleware(I18nMiddleware())

        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
