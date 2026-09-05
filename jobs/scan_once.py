"""Run one complete scan from a scheduled GitHub Actions job, then exit."""
import asyncio
import logging
import os
import time

from aiogram import Bot

from config import settings, validate_bot_settings
from db.database import init_db
from ingestion.scheduler import source_scanner

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def main() -> None:
    validate_bot_settings()
    max_seconds = max(60, int(os.getenv("SCAN_JOB_MAX_SECONDS", "3000")))
    started = time.monotonic()
    bot = Bot(token=settings.BOT_TOKEN)
    try:
        await init_db()
        source_scanner.configure(bot)
        summary = await source_scanner.scan_once()
        logger.info("Scan collected %(collected)s signals and queued %(queued)s jobs", summary)

        while time.monotonic() - started < max_seconds:
            processed = await source_scanner.process_pending_jobs()
            if not processed:
                break
        else:
            raise TimeoutError(f"Scan job exceeded {max_seconds} seconds")
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
