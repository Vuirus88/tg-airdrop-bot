"""Free image discovery from standard page metadata."""
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from config import settings
from services.safe_http import SafeHTTPError, safe_get_text, validate_public_url


@dataclass(frozen=True)
class ProjectImage:
    url: str
    source: str


def _valid_http_url(value: str | None) -> bool:
    if not value:
        return False
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


async def discover_project_image(source_url: str | None) -> ProjectImage | None:
    if not settings.ENABLE_IMAGE_DISCOVERY or not _valid_http_url(source_url):
        return None

    try:
        response = await safe_get_text(
            source_url,
            max_bytes=2 * 1024 * 1024,
            allowed_content_types=("text/html", "text/plain", "application/xhtml+xml"),
            timeout=12.0,
        )
    except Exception:
        return None

    soup = BeautifulSoup(response.text, "html.parser")
    selectors = (
        ('meta[property="og:image"]', "content"),
        ('meta[name="twitter:image"]', "content"),
        ('meta[property="twitter:image"]', "content"),
    )
    for selector, attribute in selectors:
        tag = soup.select_one(selector)
        candidate = urljoin(response.url, tag.get(attribute, "")) if tag else None
        if _valid_http_url(candidate):
            try:
                await validate_public_url(candidate)
                return ProjectImage(url=candidate, source="source_page")
            except SafeHTTPError:
                continue
    return None
