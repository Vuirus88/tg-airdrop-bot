import json
import os
import unittest
from unittest.mock import AsyncMock, patch

import httpx


os.environ.setdefault("BOT_TOKEN", "123456:test")
os.environ.setdefault("ADMIN_USER_ID", "1")
os.environ.setdefault("PUBLISH_CHANNEL_ID", "@test")

from services.groq_client import generate_json


SCHEMA = {
    "type": "object",
    "properties": {"result": {"type": "string"}},
    "required": ["result"],
    "additionalProperties": False,
}


def _success(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        request=request,
        json={"choices": [{"message": {"content": '{"result":"ok"}'}}]},
    )


class GroqClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_falls_back_to_json_object_after_schema_failure(self):
        formats = []

        def handler(request: httpx.Request):
            payload = json.loads(request.content)
            formats.append(payload["response_format"]["type"])
            if len(formats) == 1:
                return httpx.Response(
                    400,
                    request=request,
                    json={"error": {"message": "Failed to validate JSON"}},
                )
            return _success(request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with (
            patch("services.groq_client.settings.GROQ_API_KEY", "token"),
            patch("services.groq_client.settings.GROQ_MIN_REQUEST_INTERVAL_SECONDS", 0),
            patch("services.groq_client.settings.GROQ_MAX_RATE_RETRIES", 0),
            patch("services.groq_client.httpx.AsyncClient", return_value=client),
        ):
            result = await generate_json(
                system_instruction="Return JSON",
                contents="Test",
                temperature=0.1,
                schema_name="test_result",
                response_schema=SCHEMA,
            )

        self.assertEqual(result, '{"result":"ok"}')
        self.assertEqual(formats, ["json_schema", "json_object"])

    async def test_uses_strict_json_schema(self):
        captured = {}

        def handler(request: httpx.Request):
            captured.update(json.loads(request.content))
            return _success(request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with (
            patch("services.groq_client.settings.GROQ_API_KEY", "token"),
            patch("services.groq_client.settings.GROQ_MIN_REQUEST_INTERVAL_SECONDS", 0),
            patch("services.groq_client.httpx.AsyncClient", return_value=client),
        ):
            result = await generate_json(
                system_instruction="Return JSON",
                contents="Test",
                temperature=0.1,
                schema_name="test_result",
                response_schema=SCHEMA,
            )

        self.assertEqual(result, '{"result":"ok"}')
        response_format = captured["response_format"]
        self.assertEqual(response_format["type"], "json_schema")
        self.assertTrue(response_format["json_schema"]["strict"])
        self.assertEqual(response_format["json_schema"]["schema"], SCHEMA)

    async def test_retries_failed_json_generation(self):
        requests = 0

        def handler(request: httpx.Request):
            nonlocal requests
            requests += 1
            if requests == 1:
                return httpx.Response(
                    400,
                    request=request,
                    json={
                        "error": {
                            "message": "Failed to generate JSON. Please adjust your prompt.",
                            "failed_generation": {"reason": "invalid JSON"},
                        }
                    },
                )
            return _success(request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with (
            patch("services.groq_client.settings.GROQ_API_KEY", "token"),
            patch("services.groq_client.settings.GROQ_MIN_REQUEST_INTERVAL_SECONDS", 0),
            patch("services.groq_client.settings.GROQ_MAX_RATE_RETRIES", 2),
            patch("services.groq_client.httpx.AsyncClient", return_value=client),
            patch("services.groq_client.asyncio.sleep", AsyncMock()),
        ):
            result = await generate_json(
                system_instruction="Return JSON",
                contents="Test",
                temperature=0.1,
                schema_name="test_result",
                response_schema=SCHEMA,
            )

        self.assertEqual(result, '{"result":"ok"}')
        self.assertEqual(requests, 2)


if __name__ == "__main__":
    unittest.main()
