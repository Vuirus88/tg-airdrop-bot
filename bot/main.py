import asyncio
import logging

from aiogram import Bot, Dispatcher

from config import settings, validate_bot_settings
from db.database import init_db
from bot.handlers import admin_review
from ingestion.scheduler import source_scanner

logging.basicConfig(level=logging.INFO)

dp = Dispatcher()
dp.include_router(admin_review.router)


async def main():
    validate_bot_settings()
    bot = Bot(token=settings.BOT_TOKEN)
    await init_db()
    await admin_review.initialize_review_queue(bot)
    source_scanner.configure(bot)
    source_scanner.start()
    logging.info("Database ready. Starting bot polling...")
    try:
        await dp.start_polling(bot)
    finally:
        await source_scanner.stop()


if __name__ == "__main__":
    asyncio.run(main())
