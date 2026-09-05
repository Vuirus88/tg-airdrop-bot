import os
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from unittest.mock import AsyncMock

from aiogram.types import FSInputFile
from PIL import Image


os.environ.setdefault("BOT_TOKEN", "123456:test")
os.environ.setdefault("ADMIN_USER_ID", "1")
os.environ.setdefault("PUBLISH_CHANNEL_ID", "@test")

from services.media import telegram_photo
from services.social_card import _background_brief, _project_label, _steps, generate_social_card


class SocialCardTests(unittest.IsolatedAsyncioTestCase):
    def test_headline_becomes_project_domain_label(self):
        label = _project_label(
            "$DGAI AIRDROP CLAIM OPENS TOMORROW! \u25a1",
            "https://dgrid.ai/arena?code=X1J2ID",
        )

        self.assertEqual(label, "DGRID")

    def test_card_steps_remove_urls_and_unsupported_symbols(self):
        steps = _steps(
            "1. Visit the official claim portal: https://dgrid.ai/arena?code=X1J2ID\n"
            "2. Confirm eligibility \u25a1"
        )

        self.assertEqual(steps[0], "Open the official project page")
        self.assertNotIn("http", " ".join(steps))
        self.assertNotIn("\u25a1", " ".join(steps))

    def test_background_brief_ignores_logo_and_coin_requests(self):
        brief = _background_brief(
            "airdrop",
            "DGrid logo with glowing coins, readable headline, red and teal lighting",
        )

        self.assertNotIn("DGrid", brief)
        self.assertNotIn("logo", brief)
        self.assertNotIn("coins", brief)
        self.assertIn("red, teal", brief)
        self.assertIn("canyon", brief)

    async def test_generates_16_by_9_card_without_cloud_ai(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("services.social_card.settings.SOCIAL_CARD_DIRECTORY", directory):
                card = await generate_social_card(
                    name="Nova Network",
                    category="testnet",
                    chain="Ethereum",
                    instructions=(
                        "1. Open the official testnet page.\n"
                        "2. Review the active missions.\n"
                        "3. Use a separate wallet."
                    ),
                    official_image_url=None,
                )
            self.assertIsNotNone(card)
            self.assertTrue(Path(card.path).is_file())
            with Image.open(card.path) as image:
                self.assertEqual(image.size, (1200, 675))
                self.assertEqual(image.format, "JPEG")

    async def test_generation_key_creates_a_separate_version_file(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch("services.social_card.settings.SOCIAL_CARD_DIRECTORY", directory),
                patch("services.social_card.cloudflare_configured", return_value=False),
            ):
                first = await generate_social_card(
                    "Nova", "airdrop", None, "1. Review the page.", None,
                    generation_key="project-1-v1",
                )
                second = await generate_social_card(
                    "Nova", "airdrop", None, "1. Review the page.", None,
                    generation_key="project-1-v2",
                )

        self.assertNotEqual(first.path, second.path)

    async def test_local_regeneration_changes_background_variant(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch("services.social_card.settings.SOCIAL_CARD_DIRECTORY", directory),
                patch("services.social_card.cloudflare_configured", return_value=False),
            ):
                first = await generate_social_card(
                    "Nova", "airdrop", None, "1. Review the page.", None,
                    generation_key="project-1-v1",
                )
                second = await generate_social_card(
                    "Nova", "airdrop", None, "1. Review the page.", None,
                    generation_key="project-1-v2",
                )

            self.assertNotEqual(Path(first.path).read_bytes(), Path(second.path).read_bytes())

    async def test_local_card_becomes_aiogram_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "card.jpg"
            Image.new("RGB", (20, 20), "black").save(path)
            photo = telegram_photo(str(path))

        self.assertIsInstance(photo, FSInputFile)

    async def test_cloudflare_artwork_is_composed_into_social_card(self):
        artwork_buffer = BytesIO()
        Image.new("RGB", (512, 512), "purple").save(artwork_buffer, format="JPEG")
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch("services.social_card.settings.SOCIAL_CARD_DIRECTORY", directory),
                patch("services.social_card.cloudflare_configured", return_value=True),
                patch(
                    "services.social_card.generate_cloudflare_image",
                    AsyncMock(return_value=artwork_buffer.getvalue()),
                ) as generator,
            ):
                card = await generate_social_card(
                    name="Nova Network",
                    category="airdrop",
                    chain="Ethereum",
                    instructions="1. Review the official page.",
                    official_image_url=None,
                    image_prompt="Editorial cyberpunk explorer, no text",
                )

            self.assertEqual(card.source, "generated_social_card_cloudflare")
            self.assertTrue(Path(card.path).is_file())
            generator.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
