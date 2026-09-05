import os
import unittest


os.environ.setdefault("BOT_TOKEN", "123456:test")
os.environ.setdefault("ADMIN_USER_ID", "1")
os.environ.setdefault("PUBLISH_CHANNEL_ID", "@test")

from bot.keyboards import review_keyboard
from services.image_rework import extract_image_title, requests_image_rework, requests_image_text_rework


class ImageReworkTests(unittest.TestCase):
    def test_detects_russian_image_request(self):
        self.assertTrue(requests_image_rework("Поменяй картинку и сделай фон красным"))

    def test_detects_english_image_request(self):
        self.assertTrue(requests_image_rework("Replace the image with a futuristic city"))

    def test_text_only_feedback_does_not_request_image(self):
        self.assertFalse(requests_image_rework("Сократи текст Telegram и улучши пост для X"))

    def test_extracts_explicit_image_title_change(self):
        feedback = 'Замени заголовок на фото для постов на этот текст "TRENCH"'
        self.assertTrue(requests_image_rework(feedback))
        self.assertTrue(requests_image_text_rework(feedback))
        self.assertEqual(extract_image_title(feedback), "TRENCH")

    def test_image_regeneration_without_text_request_has_no_title_override(self):
        self.assertTrue(requests_image_rework("Поменяй фон и сделай его темнее"))
        self.assertFalse(requests_image_text_rework("Поменяй фон и сделай его темнее"))
        self.assertIsNone(extract_image_title("Поменяй фон и сделай его темнее"))

    def test_review_keyboard_has_regenerate_image_action(self):
        keyboard = review_keyboard(52)
        callbacks = [
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
            if button.callback_data
        ]

        self.assertIn("regen_image:52", callbacks)


if __name__ == "__main__":
    unittest.main()
