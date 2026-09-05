import unittest
from unittest.mock import patch

from config import settings, validate_bot_settings


class ConfigTests(unittest.TestCase):
    def test_personal_bot_validation_lists_missing_fields(self):
        with (
            patch.object(settings, "BOT_TOKEN", ""),
            patch.object(settings, "ADMIN_USER_ID", 0),
            patch.object(settings, "PUBLISH_CHANNEL_ID", ""),
        ):
            with self.assertRaisesRegex(
                RuntimeError, "BOT_TOKEN, ADMIN_USER_ID, PUBLISH_CHANNEL_ID"
            ):
                validate_bot_settings()

    def test_personal_bot_validation_accepts_complete_settings(self):
        with (
            patch.object(settings, "BOT_TOKEN", "123456:test"),
            patch.object(settings, "ADMIN_USER_ID", 1),
            patch.object(settings, "PUBLISH_CHANNEL_ID", "@test"),
        ):
            validate_bot_settings()


if __name__ == "__main__":
    unittest.main()
