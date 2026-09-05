"""Telegram webhook entry point for the Vercel serverless deployment.

The local ``bot.main`` process deliberately remains unchanged for desktop use.
This module is used only by ``api/index.py`` on Vercel and never starts polling.
"""
from contextlib import asynccontextmanager
import logging

from aiogram import Bot, Dispatcher
from aiogram.types import Update
from fastapi import FastAPI, Header, HTTPException, Request, Response

from bot.handlers import admin_review
from config import settings, validate_bot_settings
from db.database import init_db

logger = logging.getLogger(__name__)

dp = Dispatcher()
dp.include_router(admin_review.router)
bot: Bot | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Initialise database metadata once per warm Vercel function instance."""
    global bot
    validate_bot_settings()
    bot = Bot(token=settings.BOT_TOKEN)
    await init_db()
    try:
        yield
    finally:
        await bot.session.close()
        bot = None


app = FastAPI(title="Airdrop bot webhook", docs_url=None, redoc_url=None, lifespan=lifespan)


@app.get("/")
async def health() -> dict[str, bool]:
    """A non-sensitive health endpoint for the Vercel dashboard."""
    return {"ok": True}


@app.post("/")
async def telegram_update(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> Response:
    """Accept updates only from Telegram, then hand them to existing handlers."""
    if not settings.TELEGRAM_WEBHOOK_SECRET:
        logger.error("TELEGRAM_WEBHOOK_SECRET is not configured")
        raise HTTPException(status_code=503, detail="Webhook is not configured")
    if x_telegram_bot_api_secret_token != settings.TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid Telegram webhook secret")
    if bot is None:
        raise HTTPException(status_code=503, detail="Bot is starting")

    update = Update.model_validate(await request.json(), context={"bot": bot})
    await dp.feed_update(bot, update)
    return Response(status_code=200)
