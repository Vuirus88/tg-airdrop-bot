"""Best-effort removal of obsolete private review messages."""
import json
import logging

logger = logging.getLogger(__name__)


def tracked_message_ids(request) -> list[int]:
    try:
        values = json.loads(request.message_ids_json or "[]")
        return [int(value) for value in values]
    except (TypeError, ValueError, json.JSONDecodeError):
        return []


def remember_message(request, message_id: int) -> None:
    values = tracked_message_ids(request)
    if message_id not in values:
        values.append(message_id)
    request.message_ids_json = json.dumps(values)


async def delete_messages(bot, chat_id: int, message_ids: list[int]) -> None:
    for message_id in dict.fromkeys(message_ids):
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception as exc:
            # A message may already be deleted or outside Telegram's delete window.
            logger.debug("Could not delete review message %s: %s", message_id, exc)
