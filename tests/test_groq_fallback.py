import json
import os
import unittest
from unittest.mock import AsyncMock, patch


os.environ.setdefault("BOT_TOKEN", "123456:test")
os.environ.setdefault("ADMIN_USER_ID", "1")
os.environ.setdefault("PUBLISH_CHANNEL_ID", "@test")

from services.groq_provider import generate_draft as generate_groq_draft
from services.groq_provider import score_project as score_groq_project
from services.ai_rework import rework_draft as rework_with_cloud_fallback
from services.llm_draft import DraftResult
from services.llm_draft import _needs_repair
from services.llm_filter import FilterResult
from services.pipeline import _score_with_fallbacks


class GroqProviderTests(unittest.IsolatedAsyncioTestCase):
    def test_draft_with_url_in_telegram_instructions_needs_repair(self):
        draft = DraftResult(
            title="Atlas Testnet",
            summary="The campaign is live.",
            instructions="1. Open https://atlas.example/testnet",
            potential_reward=None,
            risk_note="Use a separate wallet.",
            twitter_text="Atlas is live. https://atlas.example/testnet #testnet",
            image_prompt="Abstract testnet environment",
        )

        self.assertTrue(_needs_repair(draft, "https://atlas.example/testnet"))

    async def test_groq_filter_parses_json_response(self):
        response = json.dumps(
            {
                "is_opportunity": True,
                "has_current_action": True,
                "score": 7.0,
                "confidence": "medium",
                "critical_risk": False,
                "verdict": "review",
                "reasoning": "Актуальная тестовая сеть. Критических рисков не найдено.",
                "chain": "Ethereum",
                "category": "testnet",
            }
        )
        with patch("services.groq_provider.generate_json", AsyncMock(return_value=response)):
            result = await score_groq_project("Atlas", "Testnet is now live")

        self.assertTrue(result.passes)
        self.assertEqual(result.category, "testnet")

    async def test_groq_draft_keeps_project_url_in_x_copy(self):
        project_url = "https://atlas.example/testnet"
        response = json.dumps(
            {
                "title": "Atlas Testnet Is Live",
                "summary": "Atlas has opened a testnet campaign. Verify all tasks on the official page.",
                "instructions": "1. Open the official page.\n2. Review and complete the listed tasks.",
                "potential_reward": "Rewards are unconfirmed.",
                "risk_note": "Use a separate wallet and verify transactions.",
                "twitter_text": f"Atlas testnet is live. Rewards are unconfirmed. Would you test it? #testnet\n{project_url}",
                "image_prompt": "A clean 16:9 editorial visual for Atlas, no readable text",
            }
        )
        with patch("services.groq_provider.generate_json", AsyncMock(return_value=response)):
            draft = await generate_groq_draft(
                "Atlas", "Testnet is now live", "Ethereum", None, project_url
            )

        self.assertIn(project_url, draft.twitter_text)
        self.assertLessEqual(len(draft.twitter_text), 280)

    async def test_pipeline_uses_groq_before_gemini(self):
        groq_result = FilterResult(
            score=7.0,
            verdict="review",
            reasoning="ok",
            chain=None,
            category="airdrop",
            is_opportunity=True,
            has_current_action=True,
            confidence="medium",
            critical_risk=False,
        )
        with (
            patch("services.pipeline._gemini_available_for_scan", return_value=True),
            patch("services.pipeline._groq_available_for_scan", return_value=True),
            patch("services.pipeline.score_project", AsyncMock()) as gemini_score,
            patch("services.pipeline.score_groq_project", AsyncMock(return_value=groq_result)),
        ):
            result, provider = await _score_with_fallbacks("Atlas", "Airdrop is live", None)

        self.assertIs(result, groq_result)
        self.assertEqual(provider, "groq")
        gemini_score.assert_not_awaited()

    async def test_pipeline_tries_gemini_only_after_groq_error(self):
        gemini_result = FilterResult(
            score=6.0,
            verdict="review",
            reasoning="backup ok",
            chain=None,
            category="testnet",
            is_opportunity=True,
            has_current_action=True,
            confidence="medium",
            critical_risk=False,
        )
        with (
            patch("services.pipeline._groq_available_for_scan", return_value=True),
            patch("services.pipeline._gemini_available_for_scan", return_value=True),
            patch("services.pipeline.score_groq_project", AsyncMock(side_effect=RuntimeError("quota"))),
            patch("services.pipeline.score_project", AsyncMock(return_value=gemini_result)),
        ):
            result, provider = await _score_with_fallbacks("Atlas", "Testnet is live", None)

        self.assertIs(result, gemini_result)
        self.assertEqual(provider, "gemini")

    async def test_rework_uses_groq_without_calling_gemini(self):
        previous = DraftResult(
            title="Old",
            summary="Old summary",
            instructions="1. Old step",
            potential_reward=None,
            risk_note=None,
            twitter_text="Old X copy",
            image_prompt=None,
        )
        improved = DraftResult(
            title="Improved",
            summary="Improved summary",
            instructions="1. Improved step",
            potential_reward=None,
            risk_note=None,
            twitter_text="Improved X copy",
            image_prompt=None,
        )
        with (
            patch("services.ai_rework.rework_with_groq", AsyncMock(return_value=improved)),
            patch("services.ai_rework.rework_with_gemini", AsyncMock()) as gemini_rework,
        ):
            result, provider = await rework_with_cloud_fallback(
                "Atlas", "raw", None, None, None, previous, "Make it shorter"
            )

        self.assertIs(result, improved)
        self.assertEqual(provider, "Groq")
        gemini_rework.assert_not_awaited()

    async def test_rework_uses_backup_gemini_after_groq_error(self):
        previous = DraftResult("Old", "Summary", "1. Step", None, None, "X copy", None)
        improved = DraftResult("New", "Summary", "1. Step", None, None, "New X", None)
        with (
            patch("services.ai_rework.rework_with_groq", AsyncMock(side_effect=RuntimeError("quota"))),
            patch("services.ai_rework.rework_with_gemini", AsyncMock(return_value=improved)),
        ):
            result, provider = await rework_with_cloud_fallback(
                "Atlas", "raw", None, None, None, previous, "Improve"
            )

        self.assertIs(result, improved)
        self.assertEqual(provider, "Gemini (резерв)")


if __name__ == "__main__":
    unittest.main()
