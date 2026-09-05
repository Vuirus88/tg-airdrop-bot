import unittest

from services.telegram_cleanup import remember_message, tracked_message_ids


class TelegramCleanupTests(unittest.TestCase):
    def test_message_ids_are_deduplicated_and_persisted(self):
        class Request:
            message_ids_json = "[]"

        request = Request()
        remember_message(request, 10)
        remember_message(request, 10)
        remember_message(request, 11)
        self.assertEqual(tracked_message_ids(request), [10, 11])

    def test_invalid_message_state_is_safe(self):
        class Request:
            message_ids_json = "invalid"

        self.assertEqual(tracked_message_ids(Request()), [])


if __name__ == "__main__":
    unittest.main()
