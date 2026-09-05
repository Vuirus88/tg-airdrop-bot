"""Register the Vercel endpoint with Telegram. Run manually after first deploy."""
import asyncio

from aiogram import Bot

from config import settings, validate_bot_settings


async def main() -> None:
    validate_bot_settings()
    if not settings.WEBHOOK_URL or not settings.TELEGRAM_WEBHOOK_SECRET:
        raise RuntimeError("WEBHOOK_URL and TELEGRAM_WEBHOOK_SECRET must be configured.")
    bot = Bot(token=settings.BOT_TOKEN)
    try:
        await bot.set_webhook(
            url=settings.WEBHOOK_URL,
            secret_token=settings.TELEGRAM_WEBHOOK_SECRET,
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=False,
        )
        info = await bot.get_webhook_info()
        print(f"Webhook registered: {info.url}")
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
