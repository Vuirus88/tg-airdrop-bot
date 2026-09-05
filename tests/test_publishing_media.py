import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image


os.environ.setdefault("BOT_TOKEN", "123456:test")
os.environ.setdefault("ADMIN_USER_ID", "1")
os.environ.setdefault("PUBLISH_CHANNEL_ID", "@test_channel")

from db.models import Draft
from publishing.dispatcher import _publish_telegram, _telegram_photo_caption
from publishing.x import _publish_sync


def _long_draft() -> Draft:
    return Draft(
        title="Atlas Testnet",
        summary="A detailed opportunity summary. " * 35,
        instructions="1. Open the page. 2. Complete the tasks. " * 25,
        potential_reward="Rewards are unconfirmed.",
        risk_note="Use a separate wallet and verify every transaction before signing.",
        project_url="https://atlas.example/testnet",
        twitter_text="Atlas testnet is live. https://atlas.example/testnet",
        image_path="https://atlas.example/image.png",
    )


class FakeTelegramBot:
    def __init__(self):
        self.photos = []
        self.messages = []

    async def send_photo(self, **kwargs):
        self.photos.append(kwargs)
        return SimpleNamespace(message_id=42)

    async def send_message(self, **kwargs):
        self.messages.append(kwargs)
        return SimpleNamespace(message_id=43)


class TelegramMediaTests(unittest.IsolatedAsyncioTestCase):
    def test_compact_caption_preserves_project_url(self):
        caption = _telegram_photo_caption(_long_draft())

        self.assertLessEqual(len(caption), 1024)
        self.assertIn("https://atlas.example/testnet", caption)

    async def test_photo_and_caption_are_sent_as_one_message(self):
        bot = FakeTelegramBot()
        result = await _publish_telegram(bot, _long_draft())

        self.assertTrue(result.success)
        self.assertEqual(len(bot.photos), 1)
        self.assertEqual(len(bot.messages), 0)
        self.assertLessEqual(len(bot.photos[0]["caption"]), 1024)


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class XMediaTests(unittest.TestCase):
    def test_x_uploads_image_before_creating_post(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "project.png"
            Image.new("RGB", (32, 32), "blue").save(image_path)
            responses = [
                FakeResponse(200, {"data": {"id": "media-123"}}),
                FakeResponse(201, {"data": {"id": "post-456"}}),
            ]

            with patch("publishing.x.requests.post", side_effect=responses) as request:
                post_id, url = _publish_sync("Ready to test", str(image_path))

        self.assertEqual(post_id, "post-456")
        self.assertEqual(url, "https://x.com/i/web/status/post-456")
        self.assertEqual(request.call_count, 2)
        create_payload = request.call_args_list[1].kwargs["json"]
        self.assertEqual(create_payload["media"]["media_ids"], ["media-123"])


if __name__ == "__main__":
    unittest.main()
