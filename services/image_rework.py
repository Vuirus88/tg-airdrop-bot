"""Detect whether reviewer feedback explicitly asks for a new image."""
from __future__ import annotations

import re


IMAGE_INTENT_RE = re.compile(
    r"(?:"
    r"картин\w*|изображен\w*|фот\w*|фон\w*|визуал\w*|облож\w*|карточк\w*|"
    r"цвет\w*|перерис\w*|image|picture|photo|visual|background|artwork|thumbnail|"
    r"colou?r|regenerate|social\s+card"
    r")",
    re.IGNORECASE,
)

IMAGE_TEXT_INTENT_RE = re.compile(
    r"(?:заголовw*|названw*|текст\s+(?:на|для)\s+(?:фото|картинw*)|"
    r"title|headline|text\s+(?:on|for)\s+(?:the\s+)?(?:image|photo|card))",
    re.IGNORECASE,
)

QUOTED_IMAGE_TITLE_RE = re.compile(
    r"(?:заголовw*|названw*|текст|title|headline)[^\n\"«]{0,100}[\"«]([^\"»\n]{2,80})[\"»]",
    re.IGNORECASE,
)


def requests_image_text_rework(feedback: str | None) -> bool:
    return bool(IMAGE_TEXT_INTENT_RE.search(feedback or ""))


def extract_image_title(feedback: str | None) -> str | None:
    """Extract a quoted title only when the reviewer explicitly mentions image text."""
    text = feedback or ""
    if not requests_image_text_rework(text):
        return None
    match = QUOTED_IMAGE_TITLE_RE.search(text)
    if not match:
        return None
    title = re.sub(r"\s+", " ", match.group(1)).strip(" .,:;!?-_")
    return title[:48] or None


def requests_image_rework(feedback: str | None) -> bool:
    return bool(IMAGE_INTENT_RE.search(feedback or ""))
