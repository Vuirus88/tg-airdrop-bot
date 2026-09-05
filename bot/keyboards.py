from urllib.parse import quote

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def review_keyboard(project_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Approve", callback_data=f"approve:{project_id}"),
                InlineKeyboardButton(text="🔁 Rework", callback_data=f"rework:{project_id}"),
                InlineKeyboardButton(text="🗑 Delete", callback_data=f"delete:{project_id}"),
            ],
            [
                InlineKeyboardButton(
                    text="🎨 Regenerate image",
                    callback_data=f"regen_image:{project_id}",
                )
            ],
        ]
    )


def queue_review_keyboard(project_id: int, position: int, total: int) -> InlineKeyboardMarkup:
    """Review actions with persistent queue navigation."""
    rows = [
        [
            InlineKeyboardButton(text="◀️", callback_data=f"queue:prev:{project_id}"),
            InlineKeyboardButton(text=f"{position}/{total}", callback_data="queue:noop"),
            InlineKeyboardButton(text="▶️", callback_data=f"queue:next:{project_id}"),
        ],
        [
            InlineKeyboardButton(text="✅ Approve", callback_data=f"approve:{project_id}"),
            InlineKeyboardButton(text="🔁 Rework", callback_data=f"rework:{project_id}"),
            InlineKeyboardButton(text="🗑 Delete", callback_data=f"delete:{project_id}"),
        ],
        [
            InlineKeyboardButton(text="🎨 Regenerate image", callback_data=f"regen_image:{project_id}"),
        ],
        [InlineKeyboardButton(text="🗃 Archives", callback_data="archive:menu")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def archive_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📤 Published", callback_data="archive:list:published"),
                InlineKeyboardButton(text="🗑 Deleted", callback_data="archive:list:deleted"),
            ],
            [InlineKeyboardButton(text="⬅️ Queue", callback_data="queue:show")],
        ]
    )


def archive_list_keyboard(kind: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🧹 Clear archive", callback_data=f"archive:clear:{kind}")],
            [InlineKeyboardButton(text="⬅️ Archives", callback_data="archive:menu")],
            [InlineKeyboardButton(text="📥 Queue", callback_data="queue:show")],
        ]
    )


def open_in_x_keyboard(text: str) -> InlineKeyboardMarkup:
    intent_url = f"https://twitter.com/intent/tweet?text={quote(text, safe='')}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Open in X", url=intent_url)],
        ]
    )
