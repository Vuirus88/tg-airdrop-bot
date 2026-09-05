"""Render branded 16:9 social cards for Telegram and X."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

from config import settings
from services.cloudflare_image import configured as cloudflare_configured
from services.cloudflare_image import generate_image as generate_cloudflare_image
from services.safe_http import safe_get_bytes


logger = logging.getLogger(__name__)
WIDTH = 1200
HEIGHT = 675
CARD_STYLE_VERSION = "ninja-editorial-v4-approved-mascot"
URL_RE = re.compile(r"https?://[^\s)\]}>,]+", re.IGNORECASE)
HEADLINE_WORDS = re.compile(
    r"\b(?:airdrop|claim|opens?|launch(?:es|ed)?|tomorrow|today|live|alert|reward|campaign)\b",
    re.IGNORECASE,
)
SCENE_BRIEFS = {
    "airdrop": "a luminous gateway opening above a deep futuristic canyon, energy particles and flowing data ribbons",
    "testnet": "a futuristic systems laboratory with interconnected light pathways, modular architecture and visible depth",
    "quest": "a bold digital expedition route through a geometric landscape with illuminated checkpoints",
    "points": "ascending pathways of light through abstract architecture with clear forward motion and layered milestones",
    "waitlist": "a sealed luminous gateway inside minimal futuristic architecture, anticipation and discovery",
}
COLOR_WORDS = (
    "red",
    "cyan",
    "teal",
    "blue",
    "green",
    "lime",
    "yellow",
    "orange",
    "magenta",
    "violet",
    "white",
    "black",
    "silver",
    "gold",
)
ENVIRONMENT_CUES = (
    "city",
    "canyon",
    "forest",
    "desert",
    "space",
    "temple",
    "laboratory",
    "gateway",
    "network",
    "landscape",
    "architecture",
    "ocean",
    "mountains",
)

PALETTES = {
    "airdrop": ((245, 241, 232), (22, 24, 24), (220, 55, 46), (255, 255, 255)),
    "testnet": ((17, 20, 25), (244, 246, 248), (48, 201, 176), (30, 35, 43)),
    "quest": ((225, 247, 70), (18, 25, 31), (235, 62, 52), (245, 247, 239)),
    "points": ((48, 13, 23), (255, 244, 238), (255, 111, 57), (82, 26, 39)),
    "waitlist": ((239, 243, 250), (17, 25, 39), (54, 105, 214), (255, 255, 255)),
}

FONT_BOLD = (
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/impact.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)
FONT_DISPLAY = (
    "C:/Windows/Fonts/impact.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
)
FONT_REGULAR = (
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


@dataclass(frozen=True)
class SocialCard:
    path: str
    source: str = "generated_social_card"


def _font(size: int, bold: bool = False):
    for candidate in FONT_BOLD if bold else FONT_REGULAR:
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, size=size)
    return ImageFont.load_default()


def _display_font(size: int):
    for candidate in FONT_DISPLAY:
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, size=size)
    return _font(size, bold=True)


def _fit_font(
    draw: ImageDraw.ImageDraw,
    text: str,
    max_width: int,
    start: int,
    minimum: int,
    display: bool = False,
):
    for size in range(start, minimum - 1, -2):
        font = _display_font(size) if display else _font(size, bold=True)
        if draw.textbbox((0, 0), text, font=font)[2] <= max_width:
            return font
    return _display_font(minimum) if display else _font(minimum, bold=True)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int, max_lines: int) -> list[str]:
    words = re.sub(r"\s+", " ", text).strip().split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textbbox((0, 0), candidate, font=font)[2] <= max_width:
            current = candidate
            continue
        if current:
            lines.append(current)
        current = word
        if len(lines) == max_lines:
            break
    if current and len(lines) < max_lines:
        lines.append(current)
    if len(lines) == max_lines:
        lines[-1] = lines[-1].rstrip(".,;:")
    return lines


def _ascii_display(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text or "")
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", ascii_text).strip(" \t\r\n-|:;,.!?")


def _project_label(name: str, project_url: str | None = None) -> str:
    clean_name = _ascii_display(URL_RE.sub("", name)).lstrip("$")
    looks_like_headline = (
        len(clean_name) > 34
        or len(clean_name.split()) > 4
        or bool(HEADLINE_WORDS.search(clean_name))
    )
    if project_url and looks_like_headline:
        host = urlparse(project_url).hostname or ""
        labels = [label for label in host.lower().split(".") if label and label != "www"]
        if len(labels) >= 2:
            domain_label = labels[-2]
            if domain_label not in {"twitter", "x", "t", "medium", "telegram", "linktr"}:
                return _ascii_display(domain_label).upper()
    ticker = re.search(r"\$([A-Za-z][A-Za-z0-9]{1,11})", name or "")
    if ticker:
        return ticker.group(1).upper()
    return (clean_name or "NEW OPPORTUNITY").upper()


def _clean_step(step: str) -> str:
    had_url = bool(URL_RE.search(step))
    cleaned = URL_RE.sub("", step)
    cleaned = _ascii_display(cleaned)
    cleaned = re.sub(r"\s+([:;,.])", r"\1", cleaned).rstrip(" :;,-")
    if had_url and re.search(r"\b(?:visit|open|go to)\b", cleaned, re.IGNORECASE):
        return "Open the official project page"
    return cleaned


def _background_brief(category: str, image_prompt: str | None) -> str:
    scene = SCENE_BRIEFS.get(category, "an abstract futuristic gateway with layered light and architectural depth")
    prompt_lower = (image_prompt or "").lower()
    colors = [color for color in COLOR_WORDS if re.search(rf"\b{color}\b", prompt_lower)][:2]
    cues = [cue for cue in ENVIRONMENT_CUES if re.search(rf"\b{cue}\b", prompt_lower)][:2]
    palette = f" Palette accents: {', '.join(colors)}." if colors else ""
    environment = f" Additional environment cues: {', '.join(cues)}." if cues else ""
    return (
        f"Science-fiction editorial environment: {scene}. Dynamic cinematic perspective, premium poster lighting, "
        f"clean focal area for a foreground character.{palette}{environment} Natural and abstract forms only."
    )


def _steps(instructions: str, limit: int = 3) -> list[str]:
    raw_steps = [part.strip() for part in re.split(r"\n+|(?=\d+\.\s)", instructions or "")]
    cleaned: list[str] = []
    for step in raw_steps:
        step = _clean_step(re.sub(r"^\d+[.)]\s*", "", step).strip())
        if step and step not in cleaned:
            cleaned.append(step)
        if len(cleaned) == limit:
            break
    return cleaned or ["Review the official page", "Verify campaign requirements"]


async def _download_image(url: str | None) -> bytes | None:
    if not url or not url.startswith(("https://", "http://")):
        return None
    try:
        response = await safe_get_bytes(
            url,
            max_bytes=8 * 1024 * 1024,
            allowed_content_types=("image/",),
        )
        return response.content
    except Exception as exc:
        logger.warning("Could not download official image for social card: %s", exc)
        return None


def _draw_fallback_art(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    accent,
    ink,
    name: str,
) -> None:
    left, top, right, bottom = box
    center_x = (left + right) // 2
    center_y = (top + bottom) // 2
    for radius, width in ((185, 4), (135, 3), (85, 2)):
        draw.ellipse(
            (center_x - radius, center_y - radius, center_x + radius, center_y + radius),
            outline=accent,
            width=width,
        )
    initials = "".join(word[0] for word in name.split()[:2]).upper() or "?"
    font = _fit_font(draw, initials, 150, 92, 48, display=True)
    draw.text((center_x, center_y), initials, fill=ink, font=font, anchor="mm")


def _paste_mascot(canvas: Image.Image, mascot_path: str, accent) -> bool:
    path = Path(mascot_path).resolve()
    if not path.is_file():
        logger.warning("Social card mascot is missing: %s", path)
        return False
    try:
        mascot = Image.open(path).convert("RGBA")
        # Keep the approved source as a transparent foreground asset. A subtle
        # shadow improves separation without creating a visible cutout halo.
        mascot.thumbnail((560, 600), Image.Resampling.LANCZOS)
        x = WIDTH - mascot.width - 18
        y = HEIGHT - mascot.height + 8
        alpha = mascot.getchannel("A")
        shadow_alpha = alpha.filter(ImageFilter.GaussianBlur(9)).point(
            lambda value: value * 72 // 255
        )
        shadow = Image.new("RGBA", mascot.size, (0, 0, 0, 0))
        shadow.putalpha(shadow_alpha)
        canvas.alpha_composite(shadow, (x + 5, y + 7))
        canvas.alpha_composite(mascot, (x, y))
        return True
    except Exception as exc:
        logger.warning("Could not compose ninja mascot: %s", exc)
        return False


def _render(
    output_path: Path,
    name: str,
    category: str,
    chain: str | None,
    instructions: str,
    official_image: bytes | None,
    project_url: str | None,
    variation_key: str = "initial",
) -> None:
    background, ink, accent, panel = PALETTES.get(category, PALETTES["waitlist"])
    canvas = Image.new("RGBA", (WIDTH, HEIGHT), (*background, 255))
    draw = ImageDraw.Draw(canvas)

    # Approved concept: cream editorial layout, burgundy visual panel, and the
    # transparent anime ninja as the only permanent mascot element.
    visual_left = 590
    variation = int(hashlib.sha256(variation_key.encode("utf-8")).hexdigest()[:8], 16)
    panel_variants = ((43, 13, 19), (55, 16, 24), (39, 15, 29), (47, 12, 22))
    visual_background = panel_variants[variation % len(panel_variants)]
    if official_image:
        try:
            source = Image.open(BytesIO(official_image)).convert("RGB")
            fitted = ImageOps.fit(source, (WIDTH - visual_left, HEIGHT))
            fitted = ImageEnhance.Color(fitted).enhance(1.08)
            fitted = ImageEnhance.Contrast(fitted).enhance(1.08)
            canvas.paste(fitted, (visual_left, 0))
        except Exception:
            draw.rectangle((visual_left, 0, WIDTH, HEIGHT), fill=(*visual_background, 255))
    else:
        draw.rectangle((visual_left, 0, WIDTH, HEIGHT), fill=(*visual_background, 255))
    if official_image:
        tint = Image.new("RGBA", (WIDTH - visual_left, HEIGHT), (*visual_background, 178))
        canvas.alpha_composite(tint, (visual_left, 0))
    circle_x = 760 + variation % 120
    circle_y = 36 + (variation // 11) % 70
    circle_size = 260 + (variation // 17) % 80
    draw.ellipse(
        (circle_x, circle_y, circle_x + circle_size, circle_y + circle_size),
        outline=(113, 48, 58, 255),
        width=2,
    )
    draw.ellipse(
        (circle_x + 90, circle_y + 88, circle_x + 300, circle_y + 298),
        outline=(113, 48, 58, 255),
        width=2,
    )
    for index in range(2):
        offset = 20 + ((variation >> (index * 5)) % 90)
        draw.line(
            (visual_left + offset, HEIGHT, WIDTH - offset, 0),
            fill=(92, 35, 47, 180),
            width=1,
        )
    draw.polygon(((590, 0), (670, 0), (590, HEIGHT)), fill=background)

    display_name = _project_label(name, project_url)
    logo_font = _display_font(22)
    draw.rounded_rectangle((48, 42, 96, 90), radius=10, fill=(28, 28, 28, 255))
    draw.text((72, 66), display_name[:1] or "M", fill=background, font=logo_font, anchor="mm")
    name_font = _fit_font(draw, display_name, 430, 46, 30, display=True)
    draw.text((110, 66), display_name, fill=(28, 28, 28, 255), font=name_font, anchor="lm")

    category_label = (category or "opportunity").upper()
    badge_font = _display_font(17)
    badge_width = draw.textbbox((0, 0), category_label, font=badge_font)[2] + 32
    draw.rounded_rectangle((48, 122, 48 + badge_width, 154), radius=4, fill=accent)
    draw.text((64, 128), category_label, fill=background, font=badge_font)

    card_box = (48, 182, 668, 482)
    draw.rounded_rectangle(card_box, radius=10, fill=(28, 28, 28, 255))
    draw.text((78, 208), "Complete the essential steps", fill=background, font=_font(24, bold=True))
    steps = _steps(instructions)
    step_font = _font(15, bold=True)
    number_font = _font(15, bold=True)
    first_y = 270
    row_height = 56
    gap = 10
    line_x = 98
    line_top = first_y + 20
    line_bottom = first_y + (len(steps) - 1) * (row_height + gap) + 36
    draw.line((line_x, line_top, line_x, line_bottom), fill=accent, width=2)
    for index, step in enumerate(steps, start=1):
        row_y = first_y + (index - 1) * (row_height + gap)
        draw.ellipse((78, row_y, 118, row_y + 40), fill=accent)
        draw.text((98, row_y + 20), f"0{index}", fill=background, font=number_font, anchor="mm")
        lines = _wrap(draw, step, step_font, 480, 2)
        for line_index, line in enumerate(lines):
            draw.text((136, row_y + 5 + line_index * 20), line, fill=(229, 221, 208, 255), font=step_font)

    draw.rounded_rectangle((278, 502, 438, 548), radius=6, fill=accent)
    draw.text((358, 512), "CLAIM", fill=background, font=_display_font(19), anchor="ma")

    mascot_visible = _paste_mascot(canvas, settings.SOCIAL_CARD_MASCOT_PATH, accent)
    draw = ImageDraw.Draw(canvas)
    if not mascot_visible:
        draw.text((900, 340), "MASCOT", fill=accent, font=_display_font(42), anchor="mm")
    social_font = _font(12, bold=True)
    # Both social marks occupy the same 18px icon slot; labels start at one
    # shared x-coordinate so their visual spacing stays identical.
    draw.text((57, 607), "X", fill=(74, 74, 74, 255), font=_font(16, bold=True), anchor="mm")
    draw.text((72, 602), "@CryptoVirus88", fill=(74, 74, 74, 255), font=social_font)
    # Draw the Telegram paper-plane mark so the card does not depend on a
    # platform font containing the Unicode glyph.
    social_gray = (74, 74, 74, 255)
    draw.polygon(((48, 638), (66, 631), (59, 647), (56, 642)), fill=social_gray)
    draw.line((56, 642, 64, 634), fill=background, width=1)
    draw.text((72, 631), "@janjezcrypto", fill=(74, 74, 74, 255), font=social_font)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(output_path, format="JPEG", quality=93, optimize=True)


async def generate_social_card(
    name: str,
    category: str,
    chain: str | None,
    instructions: str,
    official_image_url: str | None,
    image_prompt: str | None = None,
    project_url: str | None = None,
    generation_key: str | None = None,
) -> SocialCard | None:
    if not settings.ENABLE_SOCIAL_CARD_GENERATION:
        return None
    use_cloudflare = cloudflare_configured() and bool(image_prompt)
    fingerprint_source = (
        f"{name}|{category}|{chain}|{instructions}|{official_image_url}|{image_prompt}|{project_url}|"
        f"{generation_key or 'initial'}|"
        f"{settings.CLOUDFLARE_IMAGE_MODEL if use_cloudflare else 'local'}|"
        f"{settings.SOCIAL_CARD_MASCOT_PATH}|{CARD_STYLE_VERSION}"
    )
    fingerprint = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()[:16]
    output_path = Path(settings.SOCIAL_CARD_DIRECTORY).resolve() / f"{fingerprint}.jpg"
    if output_path.is_file() and not use_cloudflare:
        return SocialCard(str(output_path))

    artwork = None
    source = "generated_social_card"
    if use_cloudflare:
        try:
            artwork = await generate_cloudflare_image(_background_brief(category, image_prompt))
            source = "generated_social_card_cloudflare"
        except Exception as exc:
            logger.warning("Cloudflare artwork failed for %s; using free local fallback: %s", name, exc)
    if artwork is None:
        artwork = await _download_image(official_image_url)
    try:
        await asyncio.to_thread(
            _render,
            output_path,
            name,
            category,
            chain,
            instructions,
            artwork,
            project_url,
            generation_key or "initial",
        )
        return SocialCard(str(output_path), source)
    except Exception as exc:
        logger.exception("Could not generate social card for %s: %s", name, exc)
        return None
