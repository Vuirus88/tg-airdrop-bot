"""Bounded HTTP downloads for untrusted source and media URLs."""
from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Iterable, Mapping
from urllib.parse import urljoin, urlsplit

import httpx


DEFAULT_USER_AGENT = "Mozilla/5.0 AirdropAlphaBot/1.0"
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
ALLOWED_PORTS = {80, 443}


class SafeHTTPError(RuntimeError):
    """A remote URL failed network policy or bounded download checks."""


@dataclass(frozen=True)
class SafeHTTPResponse:
    url: str
    status_code: int
    headers: Mapping[str, str]
    content: bytes

    @property
    def text(self) -> str:
        content_type = self.headers.get("content-type", "")
        charset = "utf-8"
        for part in content_type.split(";")[1:]:
            key, _, value = part.strip().partition("=")
            if key.lower() == "charset" and value:
                charset = value.strip(" \"'")
                break
        try:
            return self.content.decode(charset, errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")


def _normalized_host(url: str) -> tuple[str, int | None]:
    if not url or len(url) > 4096:
        raise SafeHTTPError("URL is empty or too long")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise SafeHTTPError("URL has an invalid port") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise SafeHTTPError("Only HTTP and HTTPS URLs are allowed")
    if parsed.username or parsed.password:
        raise SafeHTTPError("Credentials embedded in URLs are not allowed")
    host = (parsed.hostname or "").rstrip(".").lower()
    if not host:
        raise SafeHTTPError("URL hostname is missing")
    if port is not None and port not in ALLOWED_PORTS:
        raise SafeHTTPError("Only ports 80 and 443 are allowed")
    return host, port


def _ensure_public_addresses(host: str, addresses: Iterable[str]) -> None:
    resolved = set(addresses)
    if not resolved:
        raise SafeHTTPError(f"Hostname did not resolve: {host}")
    for value in resolved:
        try:
            address = ip_address(value)
        except ValueError as exc:
            raise SafeHTTPError(f"Hostname resolved to an invalid address: {host}") from exc
        if not address.is_global:
            raise SafeHTTPError(f"Private or reserved network address is blocked: {address}")


def _resolve_public_host_sync(host: str, port: int) -> None:
    try:
        literal = ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        _ensure_public_addresses(host, [str(literal)])
        return

    if "." not in host or host.endswith((".local", ".internal", ".localhost")):
        raise SafeHTTPError(f"Local hostname is blocked: {host}")
    try:
        records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise SafeHTTPError(f"Hostname could not be resolved: {host}") from exc
    _ensure_public_addresses(host, (record[4][0] for record in records))


async def validate_public_url(url: str) -> None:
    host, explicit_port = _normalized_host(url)
    default_port = 443 if urlsplit(url).scheme.lower() == "https" else 80
    await asyncio.to_thread(_resolve_public_host_sync, host, explicit_port or default_port)


def validate_public_url_sync(url: str) -> None:
    host, explicit_port = _normalized_host(url)
    default_port = 443 if urlsplit(url).scheme.lower() == "https" else 80
    _resolve_public_host_sync(host, explicit_port or default_port)


def _validate_content_headers(
    headers: Mapping[str, str], max_bytes: int, allowed_content_types: tuple[str, ...]
) -> None:
    declared = headers.get("content-length")
    if declared:
        try:
            if int(declared) > max_bytes:
                raise SafeHTTPError(f"Remote content exceeds the {max_bytes}-byte limit")
        except ValueError:
            pass
    content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type and allowed_content_types and not any(
        content_type.startswith(allowed) if allowed.endswith("/") else content_type == allowed
        for allowed in allowed_content_types
    ):
        raise SafeHTTPError(f"Remote content type is not allowed: {content_type}")


async def safe_get_bytes(
    url: str,
    *,
    max_bytes: int,
    allowed_content_types: tuple[str, ...] = (),
    timeout: float = 15.0,
    max_redirects: int = 4,
    client: httpx.AsyncClient | None = None,
) -> SafeHTTPResponse:
    """Download a public URL with redirect, type, and size enforcement."""
    owned_client = client is None
    http = client or httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        headers={"User-Agent": DEFAULT_USER_AGENT},
    )
    current = url
    try:
        for redirect_count in range(max_redirects + 1):
            await validate_public_url(current)
            try:
                async with http.stream("GET", current) as response:
                    if response.status_code in REDIRECT_STATUSES:
                        location = response.headers.get("location")
                        if not location:
                            raise SafeHTTPError("Redirect response has no Location header")
                        if redirect_count >= max_redirects:
                            raise SafeHTTPError("Too many redirects")
                        current = urljoin(str(response.url), location)
                        continue
                    response.raise_for_status()
                    _validate_content_headers(
                        response.headers, max_bytes, allowed_content_types
                    )
                    chunks: list[bytes] = []
                    downloaded = 0
                    async for chunk in response.aiter_bytes():
                        downloaded += len(chunk)
                        if downloaded > max_bytes:
                            raise SafeHTTPError(
                                f"Remote content exceeds the {max_bytes}-byte limit"
                            )
                        chunks.append(chunk)
                    return SafeHTTPResponse(
                        url=str(response.url),
                        status_code=response.status_code,
                        headers=dict(response.headers),
                        content=b"".join(chunks),
                    )
            except httpx.HTTPError as exc:
                raise SafeHTTPError(f"Remote request failed: {exc}") from exc
        raise SafeHTTPError("Too many redirects")
    finally:
        if owned_client:
            await http.aclose()


async def safe_get_text(
    url: str,
    *,
    max_bytes: int,
    allowed_content_types: tuple[str, ...],
    timeout: float = 15.0,
    max_redirects: int = 4,
    client: httpx.AsyncClient | None = None,
) -> SafeHTTPResponse:
    return await safe_get_bytes(
        url,
        max_bytes=max_bytes,
        allowed_content_types=allowed_content_types,
        timeout=timeout,
        max_redirects=max_redirects,
        client=client,
    )


def safe_get_bytes_sync(
    url: str,
    *,
    max_bytes: int,
    allowed_content_types: tuple[str, ...] = (),
    timeout: float = 20.0,
    max_redirects: int = 4,
) -> SafeHTTPResponse:
    current = url
    headers = {"User-Agent": DEFAULT_USER_AGENT}
    with httpx.Client(timeout=timeout, follow_redirects=False, headers=headers) as http:
        for redirect_count in range(max_redirects + 1):
            validate_public_url_sync(current)
            try:
                with http.stream("GET", current) as response:
                    if response.status_code in REDIRECT_STATUSES:
                        location = response.headers.get("location")
                        if not location:
                            raise SafeHTTPError("Redirect response has no Location header")
                        if redirect_count >= max_redirects:
                            raise SafeHTTPError("Too many redirects")
                        current = urljoin(str(response.url), location)
                        continue
                    response.raise_for_status()
                    _validate_content_headers(
                        response.headers, max_bytes, allowed_content_types
                    )
                    chunks: list[bytes] = []
                    downloaded = 0
                    for chunk in response.iter_bytes():
                        downloaded += len(chunk)
                        if downloaded > max_bytes:
                            raise SafeHTTPError(
                                f"Remote content exceeds the {max_bytes}-byte limit"
                            )
                        chunks.append(chunk)
                    return SafeHTTPResponse(
                        url=str(response.url),
                        status_code=response.status_code,
                        headers=dict(response.headers),
                        content=b"".join(chunks),
                    )
            except httpx.HTTPError as exc:
                raise SafeHTTPError(f"Remote request failed: {exc}") from exc
    raise SafeHTTPError("Too many redirects")
