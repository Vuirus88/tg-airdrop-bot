import socket
import unittest
from unittest.mock import patch

import httpx

from services.safe_http import (
    SafeHTTPError,
    safe_get_bytes,
    safe_get_bytes_sync,
    safe_get_text,
)


PUBLIC_DNS = [
    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
]


class SafeHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocks_literal_private_addresses(self):
        for url in (
            "http://127.0.0.1/admin",
            "http://10.0.0.1/metadata",
            "http://169.254.169.254/latest/meta-data/",
            "http://[::1]/admin",
        ):
            with self.subTest(url=url):
                with self.assertRaises(SafeHTTPError):
                    await safe_get_bytes(url, max_bytes=100)

    async def test_blocks_hostname_resolving_to_private_network(self):
        private_dns = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.20", 443)),
        ]
        with patch("services.safe_http.socket.getaddrinfo", return_value=private_dns):
            with self.assertRaisesRegex(SafeHTTPError, "Private or reserved"):
                await safe_get_bytes("https://feed.example/rss", max_bytes=100)

    async def test_revalidates_redirect_destination(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                302,
                headers={"location": "http://127.0.0.1/private"},
                request=request,
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            with patch("services.safe_http.socket.getaddrinfo", return_value=PUBLIC_DNS):
                with self.assertRaises(SafeHTTPError):
                    await safe_get_bytes(
                        "https://public.example/start", max_bytes=100, client=client
                    )
        finally:
            await client.aclose()

    async def test_enforces_content_type(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "application/zip"},
                content=b"archive",
                request=request,
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            with patch("services.safe_http.socket.getaddrinfo", return_value=PUBLIC_DNS):
                with self.assertRaisesRegex(SafeHTTPError, "content type"):
                    await safe_get_bytes(
                        "https://public.example/file",
                        max_bytes=100,
                        allowed_content_types=("image/",),
                        client=client,
                    )
        finally:
            await client.aclose()

    async def test_enforces_streamed_size_limit(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "image/png"},
                content=b"x" * 101,
                request=request,
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            with patch("services.safe_http.socket.getaddrinfo", return_value=PUBLIC_DNS):
                with self.assertRaisesRegex(SafeHTTPError, "exceeds"):
                    await safe_get_bytes(
                        "https://public.example/image.png",
                        max_bytes=100,
                        allowed_content_types=("image/",),
                        client=client,
                    )
        finally:
            await client.aclose()

    async def test_returns_bounded_text(self):
        payload = b"<rss><channel><title>Alpha</title></channel></rss>"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "application/rss+xml; charset=utf-8"},
                content=payload,
                request=request,
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            with patch("services.safe_http.socket.getaddrinfo", return_value=PUBLIC_DNS):
                response = await safe_get_text(
                    "https://public.example/rss",
                    max_bytes=1024,
                    allowed_content_types=("application/rss+xml",),
                    client=client,
                )
        finally:
            await client.aclose()

        self.assertEqual(response.status_code, 200)
        self.assertIn("Alpha", response.text)


class SafeHTTPSyncTests(unittest.TestCase):
    def test_sync_downloader_blocks_private_address_before_request(self):
        with self.assertRaises(SafeHTTPError):
            safe_get_bytes_sync("http://127.0.0.1/image.png", max_bytes=1024)


if __name__ == "__main__":
    unittest.main()
