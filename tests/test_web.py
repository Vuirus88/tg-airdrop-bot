import unittest
import uuid
import os
from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from db.database import init_db
from db.models import Draft, Project, ProjectStatus, TelegramIntegration, User, WebSettings, Workspace, XIntegration
from web.app import app
from web.integrations import WorkspaceCredentials, load_workspace_credentials, rework_with_workspace_ai
from db.database import get_session
from services.llm_draft import DraftResult


class WebSmokeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await init_db()
        self.client = AsyncClient(
            transport=ASGITransport(app=app, client=("127.0.0.1", 50000)),
            base_url="http://127.0.0.1",
        )

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_index_and_dashboard_load(self):
        index = await self.client.get("/")
        dashboard = await self.client.get("/api/dashboard")

        self.assertEqual(index.status_code, 200)
        self.assertIn("Alpha Radar", index.text)
        self.assertEqual(dashboard.status_code, 200)
        payload = dashboard.json()
        self.assertIn("posts", payload)
        self.assertIn("archive_count", payload)

    async def test_settings_never_return_secrets(self):
        response = await self.client.get("/api/settings")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("connections", payload)
        self.assertNotIn("encrypted_credentials", payload)

    async def test_quick_health_does_not_require_network(self):
        response = await self.client.get("/api/health")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["live"])
        self.assertGreaterEqual(len(response.json()["items"]), 5)

    async def test_private_dashboard_rejects_remote_clients(self):
        remote = AsyncClient(
            transport=ASGITransport(app=app, client=("203.0.113.10", 50000)),
            base_url="http://dashboard.example",
        )
        try:
            response = await remote.get("/api/dashboard")
        finally:
            await remote.aclose()

        self.assertEqual(response.status_code, 403)
        self.assertIn("private", response.json()["detail"].lower())

    async def test_default_workspace_and_integrations_are_tenant_scoped(self):
        async with get_session() as session:
            user = await session.get(User, 1)
            workspace = await session.get(Workspace, 1)
            self.assertIsNotNone(user)
            self.assertIsNotNone(workspace)

            project = await session.scalar(select(Project).limit(1))
            if project:
                self.assertEqual(project.workspace_id, workspace.id)

            settings = await session.get(WebSettings, 1)
            self.assertEqual(settings.workspace_id, workspace.id)
            self.assertIsNone(
                await session.scalar(
                    select(TelegramIntegration).where(TelegramIntegration.workspace_id == 999)
                )
            )
            self.assertIsNone(
                await session.scalar(select(XIntegration).where(XIntegration.workspace_id == 999))
            )

    async def test_personal_auth_lifecycle(self):
        email = f"test-{uuid.uuid4().hex}@example.com"
        register = await self.client.post(
            "/api/auth/register",
            json={"email": email, "password": "correct horse battery staple"},
        )
        self.assertEqual(register.status_code, 200)
        self.assertGreater(register.json()["workspace_id"], 1)

        me = await self.client.get("/api/auth/me")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["email"], email)

        workspace_settings = await self.client.get("/api/settings")
        self.assertEqual(workspace_settings.status_code, 200)

        logout = await self.client.post("/api/auth/logout")
        self.assertEqual(logout.status_code, 200)
        self.assertEqual((await self.client.get("/api/auth/me")).status_code, 401)

    async def test_subscription_is_created_with_free_limits(self):
        response = await self.client.get("/api/subscription")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["plan_code"], "free")
        self.assertEqual(payload["status"], "active")
        self.assertFalse(payload["billing_enabled"])
        self.assertEqual(payload["limits"]["ai_requests"], 25)
        self.assertEqual(payload["usage"]["ai_requests"]["used"], 0)

    async def test_workspace_credentials_are_used_by_web_context(self):
        previous = os.environ.get("WEB_SECRET_KEY")
        os.environ["WEB_SECRET_KEY"] = "test-web-secret-for-workspace-context"
        try:
            response = await self.client.put(
                "/api/settings",
                json={
                    "filter_prompt": "test",
                    "rss_feeds": [],
                    "social_accounts": [],
                    "enabled_platforms": ["telegram", "x"],
                    "credentials": {
                        "telegram": '{"token":"workspace-token","chat_id":"-100123"}',
                        "x": '{"api_key":"key","api_secret":"secret","access_token":"access","access_token_secret":"access-secret"}',
                        "groq": "workspace-groq-key",
                    },
                },
            )
            self.assertEqual(response.status_code, 200)
            async with get_session() as session:
                credentials = await load_workspace_credentials(session, 1)
            self.assertEqual(credentials.telegram_token, "workspace-token")
            self.assertEqual(credentials.telegram_chat_id, "-100123")
            self.assertEqual(credentials.groq, "workspace-groq-key")
            self.assertTrue(credentials.has_x)
        finally:
            if previous is None:
                os.environ.pop("WEB_SECRET_KEY", None)
            else:
                os.environ["WEB_SECRET_KEY"] = previous

    async def test_workspace_gemini_key_is_used_for_rework(self):
        valid = DraftResult(
            "Atlas update", "A verified update is available.", "1. Open the official page.",
            None, "Verify every transaction.", "Atlas update https://atlas.example #testnet",
            "Editorial crypto visual",
        )
        response = type("GeminiResponse", (), {"text": __import__("json").dumps(valid.__dict__)})()
        client = type("Client", (), {"aio": type("Aio", (), {"models": type("Models", (), {"generate_content": AsyncMock(return_value=response)})()})()})()
        with patch("web.integrations.genai.Client", return_value=client) as factory:
            result, provider = await rework_with_workspace_ai(
                WorkspaceCredentials(gemini="workspace-gemini-key"), name="Atlas", raw_text="raw",
                chain=None, project_url="https://atlas.example", previous=valid, feedback="Make it shorter",
            )
        self.assertEqual(provider, "Workspace Gemini")
        self.assertEqual(result.title, valid.title)
        factory.assert_called_once_with(api_key="workspace-gemini-key")

    async def test_deleted_posts_cannot_be_deleted_or_reworked_again(self):
        async with get_session() as session:
            project = Project(
                workspace_id=1, dedup_hash=uuid.uuid4().hex, name="Archived", source="test",
                status=ProjectStatus.DELETED,
            )
            session.add(project)
            await session.flush()
            session.add(Draft(project_id=project.id, version=1, title="Archived", summary="Summary", instructions="1. Step"))
            await session.commit()
            project_id = project.id
        deleted = await self.client.post(f"/api/posts/{project_id}/delete")
        reworked = await self.client.post(f"/api/posts/{project_id}/rework", json={"feedback": "Make it clearer"})
        self.assertEqual(deleted.status_code, 409)
        self.assertEqual(reworked.status_code, 409)


if __name__ == "__main__":
    unittest.main()
