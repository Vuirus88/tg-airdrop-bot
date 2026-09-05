"""Safe local launcher for the owner-only web dashboard."""
from ipaddress import ip_address

import uvicorn

from config import settings


def main() -> None:
    try:
        is_loopback = ip_address(settings.WEB_HOST).is_loopback
    except ValueError:
        is_loopback = settings.WEB_HOST.lower() == "localhost"
    if settings.WEB_ACCESS_MODE == "private" and not is_loopback:
        raise RuntimeError(
            "WEB_HOST must be localhost/127.0.0.1 while WEB_ACCESS_MODE=private. "
            "Public hosting is disabled until authentication and tenant isolation exist."
        )
    uvicorn.run("web.app:app", host=settings.WEB_HOST, port=settings.WEB_PORT)


if __name__ == "__main__":
    main()

