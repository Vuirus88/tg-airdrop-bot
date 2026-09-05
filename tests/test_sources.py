import unittest

from ingestion.sources import _rejection_reason


class SourcePrefilterTests(unittest.TestCase):
    def test_blog_feed_is_not_the_default_airdropalert_source(self):
        from config import settings

        self.assertNotEqual(settings.AIRDROPALERT_FEED_URL, "https://airdropalert.com/feed/")
        self.assertIn("/feed/rssfeed", settings.AIRDROPALERT_FEED_URL)

    def test_airdropalert_accepts_only_campaign_pages(self):
        from ingestion import sources

        self.assertTrue(sources._is_airdropalert_campaign("https://airdropalert.com/airdrops/example/"))
        self.assertFalse(sources._is_airdropalert_campaign("https://airdropalert.com/blogs/blockchain/testnet/"))
        self.assertFalse(sources._is_airdropalert_campaign("https://example.com/airdrops/example/"))

    def test_security_warning_is_not_treated_as_opportunity(self):
        reason = _rejection_reason(
            "Warning: fake airdrop clone is a wallet drainer",
            "https://airdropalert.com/blogs/fake-warning/",
        )
        self.assertEqual(reason, "security_warning")

    def test_editorial_article_has_a_reason(self):
        reason = _rejection_reason(
            "Airdrop Explained: This beginner guide covers the basics",
            "https://airdropalert.com/blogs/what-is-an-airdrop/",
        )
        self.assertEqual(reason, "editorial_article")

    def test_live_campaign_is_not_rejected_for_missing_article_path(self):
        reason = _rejection_reason(
            "Atlas testnet is now live. Complete quests to earn points",
            "https://atlas.example/testnet",
        )
        self.assertIsNone(reason)


if __name__ == "__main__":
    unittest.main()
