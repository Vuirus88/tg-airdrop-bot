"""Publish approved Telegram posts and record manual X copy readiness."""
from dataclasses import dataclass

from aiogram import Bot

from config import settings
from db.database import get_session
from db.models import Draft, Project, PublishedPost
from publishing.instagram import InstagramNotReady, publish_to_instagram
from publishing.x import XPublishError, publish_to_x
from services.media import telegram_photo


TELEGRAM_CAPTION_LIMIT = 1024


@dataclass
class PublishResult:
    platform: str
    success: bool
    url: str | None = None
    platform_post_id: str | None = None
    error: str | None = None


def _telegram_post_url(message_id: int) -> str | None:
    channel = settings.PUBLISH_CHANNEL_ID.strip()
    if channel.startswith("@"):
        return f"https://t.me/{channel[1:]}/{message_id}"
    return None


def _shorten(value: str | None, limit: int) -> str:
    text = " ".join((value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0].rstrip(".,;:") + "."


def _telegram_photo_caption(draft: Draft) -> str:
    """Keep a photo post inside Telegram's 1024-character caption limit."""
    full_text = draft.rendered_text()
    if len(full_text) <= TELEGRAM_CAPTION_LIMIT:
        return full_text

    parts = [
        f"🚀 {_shorten(draft.title, 140)}",
        "",
        _shorten(draft.summary, 280),
        "",
        "📝 What to do:",
        _shorten(draft.instructions, 300),
    ]
    if draft.risk_note:
        parts += ["", f"⚠️ Risk: {_shorten(draft.risk_note, 140)}"]
    if draft.project_url:
        parts += ["", f"🔗 Start here: {draft.project_url}"]

    caption = "\n".join(parts)
    if len(caption) <= TELEGRAM_CAPTION_LIMIT:
        return caption

    # Preserve the project URL even when an unusually long source draft is stored.
    link = f"🔗 Start here: {draft.project_url}" if draft.project_url else ""
    reserved = len(link) + (2 if link else 0)
    body = caption[: TELEGRAM_CAPTION_LIMIT - reserved].rstrip()
    return f"{body}\n\n{link}" if link else body


async def _publish_telegram(bot: Bot, draft: Draft) -> PublishResult:
    text = draft.rendered_text()
    try:
        if draft.image_path:
            try:
                message = await bot.send_photo(
                    chat_id=settings.PUBLISH_CHANNEL_ID,
                    photo=telegram_photo(draft.image_path),
                    caption=_telegram_photo_caption(draft),
                )
            except Exception as exc:
                return PublishResult(
                    platform="telegram",
                    success=False,
                    error=f"Telegram не смог опубликовать фото вместе с постом: {exc}",
                )
        else:
            message = await bot.send_message(
                chat_id=settings.PUBLISH_CHANNEL_ID,
                text=text,
            )
        return PublishResult(
            platform="telegram",
            success=True,
            url=_telegram_post_url(message.message_id),
            platform_post_id=str(message.message_id),
        )
    except Exception as exc:
        return PublishResult(platform="telegram", success=False, error=str(exc))


async def _publish_x(draft: Draft) -> PublishResult:
    if not draft.twitter_text:
        return PublishResult(
            platform="x",
            success=False,
            error="Текст для X/Twitter не был сгенерирован.",
        )
    try:
        post_id, url = await publish_to_x(draft.twitter_text, draft.image_path)
        return PublishResult(
            platform="x",
            success=True,
            url=url,
            platform_post_id=post_id,
        )
    except XPublishError as exc:
        return PublishResult(platform="x", success=False, error=str(exc))
    except Exception as exc:
        return PublishResult(platform="x", success=False, error=f"Неожиданная ошибка X: {exc}")


async def _publish_instagram(draft: Draft) -> PublishResult:
    try:
        result = await publish_to_instagram(caption=draft.rendered_text(), image_url=draft.image_path)
        return PublishResult(
            platform="instagram", success=True, platform_post_id=result.get("id")
        )
    except InstagramNotReady as exc:
        return PublishResult(platform="instagram", success=False, error=str(exc))
    except Exception as exc:
        return PublishResult(platform="instagram", success=False, error=str(exc))


async def publish_project(bot: Bot, project: Project, draft: Draft) -> list[PublishResult]:
    async with get_session() as session:
        existing = await session.execute(
            __import__("sqlalchemy").select(PublishedPost).where(
                PublishedPost.draft_id == draft.id
            )
        )
        previous = {item.platform: item for item in existing.scalars()}

    results: list[PublishResult] = []
    if previous.get("telegram") and previous["telegram"].success:
        record = previous["telegram"]
        results.append(
            PublishResult(
                platform="telegram",
                success=True,
                url=record.url,
                platform_post_id=record.platform_post_id,
                error="Уже опубликовано ранее; повторная отправка пропущена.",
            )
        )
    else:
        results.append(await _publish_telegram(bot, draft))

    if previous.get("x") and previous["x"].success:
        record = previous["x"]
        results.append(
            PublishResult(
                platform="x",
                success=True,
                url=record.url,
                platform_post_id=record.platform_post_id,
                error="Уже опубликовано ранее; повторная отправка пропущена.",
            )
        )
    else:
        results.append(await _publish_x(draft))
    if settings.INSTAGRAM_ACCESS_TOKEN and settings.INSTAGRAM_BUSINESS_ACCOUNT_ID:
        if previous.get("instagram") and previous["instagram"].success:
            record = previous["instagram"]
            results.append(
                PublishResult(
                    platform="instagram",
                    success=True,
                    url=record.url,
                    platform_post_id=record.platform_post_id,
                    error="Уже опубликовано ранее; повторная отправка пропущена.",
                )
            )
        else:
            results.append(await _publish_instagram(draft))

    async with get_session() as session:
        for result in results:
            record = await session.scalar(
                __import__("sqlalchemy").select(PublishedPost).where(
                    PublishedPost.draft_id == draft.id,
                    PublishedPost.platform == result.platform,
                )
            )
            if record is None:
                record = PublishedPost(
                    project_id=project.id,
                    draft_id=draft.id,
                    platform=result.platform,
                )
                session.add(record)
            record.platform_post_id = result.platform_post_id
            record.url = result.url
            record.success = result.success
            record.error = result.error
        await session.commit()
    return results
