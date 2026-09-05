import os
import unittest


os.environ.setdefault("BOT_TOKEN", "123456:test")
os.environ.setdefault("ADMIN_USER_ID", "1")
os.environ.setdefault("PUBLISH_CHANNEL_ID", "@test")

from services.fallback_content import fallback_generate_draft, fallback_score_project


class FallbackContentTests(unittest.TestCase):
    def test_actionable_testnet_goes_to_review(self):
        result = fallback_score_project(
            "Atlas Testnet",
            "The incentivized testnet is now live. Join and complete tasks on Ethereum.",
        )

        self.assertTrue(result.passes)
        self.assertEqual(result.category, "testnet")
        self.assertEqual(result.chain, "Ethereum")
        self.assertIn("Локальный режим", result.reasoning)

    def test_critical_wallet_risk_is_rejected(self):
        result = fallback_score_project(
            "Unsafe Airdrop",
            "The airdrop is live. Send funds to claim and provide your seed phrase.",
        )

        self.assertFalse(result.passes)
        self.assertTrue(result.critical_risk)
        self.assertEqual(result.score, 0.0)

    def test_editorial_article_is_rejected(self):
        result = fallback_score_project(
            "Airdrop Explained",
            "What is an airdrop? This educational article explains the basics.",
        )

        self.assertFalse(result.passes)

    def test_draft_is_english_and_x_copy_contains_project_url(self):
        project_url = "https://atlas.example/testnet"
        draft = fallback_generate_draft(
            "Atlas",
            "The testnet is now live. Complete tasks to participate.",
            "Ethereum",
            "testnet",
            project_url,
        )

        self.assertIn(project_url, draft.twitter_text)
        self.assertLessEqual(len(draft.twitter_text), 280)
        self.assertIn("without AI", draft.summary)
        self.assertNotRegex(draft.summary, r"[А-Яа-яЁё]")


if __name__ == "__main__":
    unittest.main()
