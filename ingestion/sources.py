"""
Free/limited discovery sources.

Sources return raw candidates only. The main pipeline still owns deduping,
Gemini scoring, draft generation, and the admin review flow.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from html import unescape
from urllib.parse import parse_qs, unquote, urlparse

import feedparser
from bs4 import BeautifulSoup

from config import settings
from services.safe_http import safe_get_text


ACTIONABLE_KEYWORDS = (
    "airdrop",
    "air drop",
    "retrodrop",
    "retroactive",
    "testnet",
    "incentivized",
    "claim",
    "snapshot",
    "quest",
    "campaign",
    "points",
    "xp",
    "faucet",
    "missions",
    "waitlist",
)

EDITORIAL_KEYWORDS = (
    "report",
    "analysis",
    "explained",
    "price prediction",
    "market update",
    "weekly update",
    "newsletter",
    "airdrop definition",
    "named global exchange of the year",
    "technical analysis",
)

CURRENT_ACTION_MARKERS = (
    "is live",
    "now live",
    "launched",
    "launches",
    "open now",
    "now open",
    "claim now",
    "starts on",
    "ending",
    "deadline",
    "join the",
    "complete tasks",
    "earn points",
)

SECURITY_WARNING_MARKERS = (
    "fake",
    "scam",
    "drainer",
    "wallet drainer",
    "phishing",
    "warning:",
    "security alert",
)

NON_PROJECT_HOST_MARKERS = (
    "twitter.com",
    "x.com",
    "t.me",
    "telegram.me",
    "discord.gg",
    "discord.com",
    "facebook.com",
    "instagram.com",
    "medium.com",
    "mirror.xyz",
    "substack.com",
    "reddit.com",
    "nitter.",
)


@dataclass(frozen=True)
class RawSignal:
    name: str
    raw_text: str
    source: str
    source_url: str | None = None


@dataclass(frozen=True)
class SourceReport:
    source: str
    fetched: bool
    entries: int = 0
    candidates: int = 0
    rejected: int = 0
    reason_counts: dict[str, int] | None = None
    error: str | None = None


@dataclass(frozen=True)
class SourceCollection:
    signals: list[RawSignal]
    reports: list[SourceReport]


def _clean_text(value: str) -> str:
    decoded = unescape(value or "")
    text = (
        BeautifulSoup(decoded, "html.parser").get_text(" ", strip=True)
        if "<" in decoded and ">" in decoded
        else decoded
    )
    return re.sub(r"\s+", " ", text).strip()


def _looks_actionable(text: str, link: str | None = None) -> bool:
    lowered = (text or "").lower()
    has_action_keyword = any(
        re.search(rf"(?<![a-z]){re.escape(keyword)}(?![a-z])", lowered)
        for keyword in ACTIONABLE_KEYWORDS
    )
    if not has_action_keyword:
        return False
    if any(keyword in lowered for keyword in EDITORIAL_KEYWORDS) and not any(
        marker in lowered for marker in CURRENT_ACTION_MARKERS
    ):
        return False

    if not link:
        return True

    path = urlparse(link).path.lower()
    source_markers = ("airdrop", "airdrops", "retrodrop", "testnet", "quest", "campaign", "points")
    return any(marker in path for marker in source_markers) or _is_project_link(link)


def _rejection_reason(text: str, link: str | None = None) -> str | None:
    lowered = (text or "").lower()
    if any(marker in lowered for marker in SECURITY_WARNING_MARKERS):
        return "security_warning"
    if not any(
        re.search(rf"(?<![a-z]){re.escape(keyword)}(?![a-z])", lowered)
        for keyword in ACTIONABLE_KEYWORDS
    ):
        return "no_action_keyword"
    if any(keyword in lowered for keyword in EDITORIAL_KEYWORDS) and not any(
        marker in lowered for marker in CURRENT_ACTION_MARKERS
    ):
        return "editorial_article"
    if link:
        path = urlparse(link).path.lower()
        source_markers = (
            "airdrop", "airdrops", "retrodrop", "testnet", "quest", "campaign", "points"
        )
        if not any(marker in path for marker in source_markers) and not _is_project_link(link):
            return "content_page_without_project_link"
    return None


def _is_project_link(url: str | None) -> bool:
    if not url:
        return False

    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    if not host:
        return False
    if any(marker in path for marker in ("/blog/", "/blogs/", "/article/", "/news/", "/learn/")):
        return False
    return not any(marker in host for marker in NON_PROJECT_HOST_MARKERS)


def _normalize_external_url(url: str | None) -> str | None:
    if not url:
        return None

    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    for key in ("url", "u", "q", "target"):
        if key in query and query[key]:
            candidate = unquote(query[key][0])
            if candidate.startswith("http"):
                return candidate

    if parsed.scheme in {"http", "https"}:
        return url
    return None


def _entry_links(entry) -> list[str]:
    links: list[str] = []
    for item in getattr(entry, "links", []) or []:
        href = item.get("href") if isinstance(item, dict) else getattr(item, "href", None)
        normalized = _normalize_external_url(href)
        if normalized:
            links.append(normalized)

    summary = getattr(entry, "summary", "") or ""
    soup = BeautifulSoup(summary, "html.parser")
    for anchor in soup.find_all("a", href=True):
        normalized = _normalize_external_url(anchor["href"])
        if normalized:
            links.append(normalized)

    deduped: list[str] = []
    seen: set[str] = set()
    for link in links:
        if link not in seen:
            seen.add(link)
            deduped.append(link)
    return deduped


def _best_project_link(entry, fallback: str | None) -> str | None:
    for link in _entry_links(entry):
        if _is_project_link(link):
            return link
    return fallback


def _is_airdropalert_campaign(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    return parsed.netloc.lower().endswith("airdropalert.com") and "/airdrops/" in parsed.path.lower()


def _stable_name(title: str, source: str) -> str:
    cleaned = re.sub(r"^(airdrop|testnet|retrodrop)\s*:\s*", "", title, flags=re.IGNORECASE)
    cleaned = cleaned.split(":")[0].strip(" -")
    if cleaned:
        return cleaned[:100]

    digest = hashlib.sha256(f"{source}:{title}".encode("utf-8")).hexdigest()[:8]
    return f"Candidate {digest}"


async def _fetch_text(url: str) -> str | None:
    response = await safe_get_text(
        url,
        max_bytes=2 * 1024 * 1024,
        allowed_content_types=(
            "application/rss+xml",
            "application/atom+xml",
            "application/xml",
            "text/xml",
            "text/plain",
            "text/html",
        ),
    )
    return response.text


async def _signals_from_feed(
    feed_url: str, source: str, limit: int = 20
) -> SourceCollection:
    try:
        payload = await _fetch_text(feed_url)
    except Exception as exc:
        return SourceCollection(
            signals=[],
            reports=[SourceReport(source, fetched=False, error=str(exc)[:180])],
        )

    feed = feedparser.parse(payload or "")
    signals: list[RawSignal] = []
    entries = (getattr(feed, "entries", []) or [])[:limit]
    reasons = Counter()
    for entry in entries:
        title = _clean_text(getattr(entry, "title", "") or "")
        summary = _clean_text(getattr(entry, "summary", "") or "")
        link = getattr(entry, "link", None)
        best_link = _best_project_link(entry, link)
        if source == "airdropalert" and not _is_airdropalert_campaign(best_link or link):
            reasons["not_campaign_page"] += 1
            continue
        combined = f"{title}\n\n{summary}\n\nLink: {best_link or link or feed_url}"
        reason = _rejection_reason(f"{title}\n{summary}", best_link or link)
        if reason:
            reasons[reason] += 1
            continue

        signals.append(
            RawSignal(
                name=_stable_name(title, source),
                raw_text=combined,
                source=source,
                source_url=best_link or link or feed_url,
            )
        )
    return SourceCollection(
        signals=signals,
        reports=[
            SourceReport(
                source=source,
                fetched=True,
                entries=len(entries),
                candidates=len(signals),
                rejected=sum(reasons.values()),
                reason_counts=dict(reasons),
            )
        ],
    )


async def collect_airdropalert() -> list[RawSignal]:
    if not settings.ENABLE_AIRDROPALERT_SOURCE:
        return []
    return (await _signals_from_feed(settings.AIRDROPALERT_FEED_URL, "airdropalert", limit=30)).signals


async def collect_rss_feeds() -> list[RawSignal]:
    if not settings.ENABLE_RSS_SOURCE:
        return []

    signals: list[RawSignal] = []
    for feed_url in settings.RSS_FEEDS:
        signals.extend((await _signals_from_feed(feed_url, "rss_feed", limit=20)).signals)
    return signals


async def collect_trusted_x() -> list[RawSignal]:
    if not settings.ENABLE_TRUSTED_X_SOURCE or not settings.ENABLE_FREE_X_FALLBACK:
        return []

    signals: list[RawSignal] = []
    for username in settings.TRUSTED_X_ACCOUNTS:
        for base_url in settings.FREE_X_RSS_BASE_URLS:
            feed_url = f"{base_url.rstrip('/')}/{username}/rss"
            items = (await _signals_from_feed(feed_url, f"trusted_x:{username}", limit=8)).signals
            if items:
                signals.extend(items)
                break
    return signals


async def collect_all_signals() -> list[RawSignal]:
    return (await collect_all_signals_detailed()).signals


async def collect_all_signals_detailed() -> SourceCollection:
    all_signals: list[RawSignal] = []
    reports: list[SourceReport] = []
    feed_jobs: list[tuple[str, str, int]] = []
    if settings.ENABLE_AIRDROPALERT_SOURCE:
        feed_jobs.append((settings.AIRDROPALERT_FEED_URL, "airdropalert", 30))
    if settings.ENABLE_RSS_SOURCE:
        feed_jobs.extend((url, "rss_feed", 20) for url in settings.RSS_FEEDS)

    for feed_url, source, limit in feed_jobs:
        collection = await _signals_from_feed(feed_url, source, limit=limit)
        all_signals.extend(collection.signals)
        reports.extend(collection.reports)

    if settings.ENABLE_TRUSTED_X_SOURCE and settings.ENABLE_FREE_X_FALLBACK:
        for username in settings.TRUSTED_X_ACCOUNTS:
            for base in settings.FREE_X_RSS_BASE_URLS:
                collection = await _signals_from_feed(
                    f"{base.rstrip('/')}/{username}/rss",
                    f"trusted_x:{username}",
                    limit=8,
                )
                reports.extend(collection.reports)
                if collection.signals:
                    all_signals.extend(collection.signals)
                    break

    unique: list[RawSignal] = []
    seen: set[str] = set()
    for signal in all_signals:
        key = (signal.source_url or signal.name).lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(signal)
    return SourceCollection(signals=unique, reports=reports)
