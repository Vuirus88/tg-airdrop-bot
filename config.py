"""
Central configuration, loaded from environment variables (.env file).
"""
import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"Copy .env.example to .env and fill it in."
        )
    return value


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name, str(default)).strip().lower()
    return value in {"1", "true", "yes", "y", "on"}


def _int(name: str, default: int) -> int:
    value = os.getenv(name, str(default)).strip()
    try:
        return int(value)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    value = os.getenv(name, str(default)).strip()
    try:
        return float(value)
    except ValueError:
        return default


def _choice(name: str, default: str, allowed: set[str]) -> str:
    value = os.getenv(name, default).strip().lower()
    return value if value in allowed else default


def _list(name: str, default: str = "") -> list[str]:
    value = os.getenv(name, default).strip()
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _database_url() -> str:
    """Accept Neon’s standard PostgreSQL URL with SQLAlchemy’s async driver."""
    value = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./airdrop_bot.db").strip()
    if value.startswith("postgresql://"):
        value = "postgresql+asyncpg://" + value.removeprefix("postgresql://")
    elif value.startswith("postgres://"):
        value = "postgresql+asyncpg://" + value.removeprefix("postgres://")
    if value.startswith("postgresql+asyncpg://"):
        parts = urlsplit(value)
        parameters = parse_qsl(parts.query, keep_blank_values=True)
        sslmode = next((item[1] for item in parameters if item[0] == "sslmode"), None)
        # SQLAlchemy passes URL query parameters to asyncpg as keyword args.
        # ``sslmode`` and Neon’s optional ``channel_binding`` are libpq-only;
        # asyncpg expects the SSL setting under ``ssl`` instead.
        parameters = [
            item for item in parameters if item[0] not in {"sslmode", "channel_binding"}
        ]
        if sslmode and not any(item[0] == "ssl" for item in parameters):
            parameters.append(("ssl", sslmode))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(parameters), parts.fragment))
    return value


class Settings:
    # Telegram is required only by the personal bot process. The web process
    # must be able to start without a global Telegram account.
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "").strip()
    ADMIN_USER_ID: int = _int("ADMIN_USER_ID", 0)
    PUBLISH_CHANNEL_ID: str = os.getenv("PUBLISH_CHANNEL_ID", "").strip()

    # Public mode is safe only with mandatory authentication and workspace isolation.
    WEB_ACCESS_MODE: str = _choice("WEB_ACCESS_MODE", "private", {"private", "public"})
    WEB_AUTH_MODE: str = _choice("WEB_AUTH_MODE", "off", {"off", "required"})
    WEB_COOKIE_SECURE: bool = _bool("WEB_COOKIE_SECURE", WEB_ACCESS_MODE == "public")
    WEB_HOST: str = os.getenv("WEB_HOST", "127.0.0.1").strip() or "127.0.0.1"
    WEB_PORT: int = min(65535, max(1, _int("WEB_PORT", 8000)))

    # Gemini is retained as an optional cloud fallback for Groq.
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "").strip()
    LLM_MODEL: str = os.getenv("LLM_MODEL", "gemini-flash-lite-latest")
    LLM_MIN_REQUEST_INTERVAL_SECONDS: float = max(
        0.0, _float("LLM_MIN_REQUEST_INTERVAL_SECONDS", 13.0)
    )
    LLM_MAX_RATE_RETRIES: int = max(0, _int("LLM_MAX_RATE_RETRIES", 2))
    # Groq is the primary cloud model for filtering, drafting, and rework.
    GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "").strip()
    GROQ_MODEL: str = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
    GROQ_MIN_REQUEST_INTERVAL_SECONDS: float = max(
        0.0, _float("GROQ_MIN_REQUEST_INTERVAL_SECONDS", 2.1)
    )
    GROQ_MAX_RATE_RETRIES: int = max(0, _int("GROQ_MAX_RATE_RETRIES", 2))
    FILTER_MIN_SCORE: float = _float("FILTER_MIN_SCORE", 4.0)
    FILTER_VERSION: int = max(1, _int("FILTER_VERSION", 2))
    ENABLE_IMAGE_DISCOVERY: bool = _bool("ENABLE_IMAGE_DISCOVERY", True)
    ENABLE_SOCIAL_CARD_GENERATION: bool = _bool("ENABLE_SOCIAL_CARD_GENERATION", True)
    SOCIAL_CARD_DIRECTORY: str = os.getenv("SOCIAL_CARD_DIRECTORY", "images/generated")
    SOCIAL_CARD_MASCOT_PATH: str = os.getenv(
        "SOCIAL_CARD_MASCOT_PATH", "images/brand/anime-ninja-mascot.png"
    )
    CLOUDFLARE_API_TOKEN: str = os.getenv("CLOUDFLARE_API_TOKEN", "").strip()
    CLOUDFLARE_ACCOUNT_ID: str = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
    CLOUDFLARE_IMAGE_MODEL: str = os.getenv(
        "CLOUDFLARE_IMAGE_MODEL", "@cf/black-forest-labs/flux-1-schnell"
    )
    CLOUDFLARE_IMAGE_STEPS: int = min(8, max(1, _int("CLOUDFLARE_IMAGE_STEPS", 4)))
    ENABLE_PROJECT_LINK_DISCOVERY: bool = _bool("ENABLE_PROJECT_LINK_DISCOVERY", True)

    # Database
    DATABASE_URL: str = _database_url()
    # Used only by the serverless Telegram webhook. Keep this value secret: Telegram
    # sends it back in a header with every update.
    TELEGRAM_WEBHOOK_SECRET: str = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
    WEBHOOK_URL: str = os.getenv("WEBHOOK_URL", "").strip()

    # Free/limited ingestion sources.
    SOURCE_SCAN_INTERVAL_MINUTES: int = max(5, _int("SOURCE_SCAN_INTERVAL_MINUTES", 60))
    SOURCE_COLLECTION_TIMEOUT_SECONDS: int = max(15, _int("SOURCE_COLLECTION_TIMEOUT_SECONDS", 120))
    SIGNAL_PROCESS_TIMEOUT_SECONDS: int = max(30, _int("SIGNAL_PROCESS_TIMEOUT_SECONDS", 180))
    JOB_WORKER_INTERVAL_SECONDS: int = max(1, _int("JOB_WORKER_INTERVAL_SECONDS", 2))
    JOB_MAX_ATTEMPTS: int = max(1, _int("JOB_MAX_ATTEMPTS", 3))
    CLEANUP_INTERVAL_HOURS: int = max(1, _int("CLEANUP_INTERVAL_HOURS", 24))
    JOB_RETENTION_DAYS: int = max(1, _int("JOB_RETENTION_DAYS", 30))
    REVIEW_REQUEST_RETENTION_DAYS: int = max(1, _int("REVIEW_REQUEST_RETENTION_DAYS", 90))
    AUDIT_RETENTION_DAYS: int = max(7, _int("AUDIT_RETENTION_DAYS", 180))
    RUN_SCAN_ON_START: bool = _bool("RUN_SCAN_ON_START", True)
    ENABLE_AIRDROPALERT_SOURCE: bool = _bool("ENABLE_AIRDROPALERT_SOURCE", True)
    ENABLE_RSS_SOURCE: bool = _bool("ENABLE_RSS_SOURCE", True)
    ENABLE_TRUSTED_X_SOURCE: bool = _bool("ENABLE_TRUSTED_X_SOURCE", True)
    ENABLE_FREE_X_FALLBACK: bool = _bool("ENABLE_FREE_X_FALLBACK", True)
    AIRDROPALERT_FEED_URL: str = os.getenv(
        "AIRDROPALERT_FEED_URL", "https://airdropalert.com/feed/rssfeed"
    )
    RSS_FEEDS: list[str] = _list(
        "RSS_FEEDS",
        "https://cryptonews.com/feed/,https://www.theblock.co/rss.xml,https://www.coindesk.com/arc/outboundfeeds/rss/",
    )
    TRUSTED_X_ACCOUNTS: list[str] = _list(
        "TRUSTED_X_ACCOUNTS",
        "airdropalertcom,arbitrum,optimismFND,Starknet,zksync",
    )
    FREE_X_RSS_BASE_URLS: list[str] = _list(
        "FREE_X_RSS_BASE_URLS",
        "https://nitter.net,https://xcancel.com",
    )

    # Stage 4 (optional for now)
    X_API_BEARER_TOKEN: str = os.getenv("X_API_BEARER_TOKEN", "")
    X_API_KEY: str = os.getenv("X_API_KEY", "")
    X_API_SECRET: str = os.getenv("X_API_SECRET", "")
    X_ACCESS_TOKEN: str = os.getenv("X_ACCESS_TOKEN", "")
    X_ACCESS_TOKEN_SECRET: str = os.getenv("X_ACCESS_TOKEN_SECRET", "")
    X_AUTO_PUBLISH: bool = _bool("X_AUTO_PUBLISH", True)
    INSTAGRAM_ACCESS_TOKEN: str = os.getenv("INSTAGRAM_ACCESS_TOKEN", "")
    INSTAGRAM_BUSINESS_ACCOUNT_ID: str = os.getenv("INSTAGRAM_BUSINESS_ACCOUNT_ID", "")


settings = Settings()


def validate_bot_settings() -> None:
    """Fail clearly when the personal Telegram process is misconfigured."""
    missing = []
    if not settings.BOT_TOKEN:
        missing.append("BOT_TOKEN")
    if settings.ADMIN_USER_ID <= 0:
        missing.append("ADMIN_USER_ID")
    if not settings.PUBLISH_CHANNEL_ID:
        missing.append("PUBLISH_CHANNEL_ID")
    if missing:
        raise RuntimeError(
            "Personal Telegram bot is not configured. Missing: " + ", ".join(missing)
        )
