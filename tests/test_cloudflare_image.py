import base64
import os
import unittest
from unittest.mock import patch

import httpx
from PIL import Image
from io import BytesIO


os.environ.setdefault("BOT_TOKEN", "123456:test")
os.environ.setdefault("ADMIN_USER_ID", "1")
os.environ.setdefault("PUBLISH_CHANNEL_ID", "@test")

from services.cloudflare_image import generate_image


class CloudflareImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_decodes_workers_ai_base64_image(self):
        buffer = BytesIO()
        Image.new("RGB", (32, 32), "red").save(buffer, format="JPEG")
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")

        def handler(request: httpx.Request):
            return httpx.Response(
                200,
                request=request,
                json={"success": True, "result": {"image": encoded}, "errors": []},
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with (
            patch("services.cloudflare_image.settings.CLOUDFLARE_API_TOKEN", "token"),
            patch("services.cloudflare_image.settings.CLOUDFLARE_ACCOUNT_ID", "account"),
            patch("services.cloudflare_image.httpx.AsyncClient", return_value=client),
        ):
            result = await generate_image("Crypto editorial art")

        self.assertEqual(result, buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
