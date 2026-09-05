"""Workspace-scoped integrations used only by the web panel.

The personal Telegram bot keeps using its existing environment-backed clients.
This module is intentionally imported by ``web.app`` only, so SaaS credentials
cannot silently change the bot process behaviour.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from aiogram import Bot
from cryptography.fernet import Fernet
from google import genai
from google.genai import types
from requests_oauthlib import OAuth1
import requests

from db.models import Draft, Project, PublishedPost, TelegramIntegration, XIntegration
from services.llm_draft import DraftResult, SYSTEM_PROMPT, _needs_repair, _parse_draft
from services.groq_provider import DRAFT_RESPONSE_SCHEMA
from services.media import telegram_photo


class WorkspaceIntegrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkspaceCredentials:
    groq: str | None = None
    gemini: str | None = None
    telegram_token: str | None = None
    telegram_chat_id: str | None = None
    x_api_key: str | None = None
    x_api_secret: str | None = None
    x_access_token: str | None = None
    x_access_token_secret: str | None = None

    @property
    def has_x(self) -> bool:
        return all((self.x_api_key, self.x_api_secret, self.x_access_token, self.x_access_token_secret))


def _cipher() -> Fernet:
    secret = os.getenv("WEB_SECRET_KEY", "").strip()
    if not secret:
        raise WorkspaceIntegrationError("WEB_SECRET_KEY is required for workspace integrations")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))


def _decrypt(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return _cipher().decrypt(value.encode()).decode()
    except Exception as exc:
        raise WorkspaceIntegrationError("Stored workspace credential cannot be decrypted") from exc


def _json_secret(value: str | None) -> dict[str, str]:
    if not value:
        return {}
    try:
        payload = json.loads(value)
        return payload if isinstance(payload, dict) else {}
    except json.JSONDecodeError:
        # Backward compatible with the original one-secret UI.
        return {"token": value}


async def load_workspace_credentials(session, workspace_id: int) -> WorkspaceCredentials:
    from sqlalchemy import select
    from db.models import WebSettings

    settings_row = await session.scalar(select(WebSettings).where(WebSettings.workspace_id == workspace_id))
    stored = json.loads(settings_row.encrypted_credentials or "{}") if settings_row else {}
    telegram = await session.scalar(select(TelegramIntegration).where(TelegramIntegration.workspace_id == workspace_id))
    x = await session.scalar(select(XIntegration).where(XIntegration.workspace_id == workspace_id))
    x_payload = _json_secret(x.encrypted_credentials if x else None)
    telegram_payload = _json_secret(_decrypt(telegram.encrypted_bot_token) if telegram else None)
    # AI credentials are stored as individually encrypted values in WebSettings.
    return WorkspaceCredentials(
        groq=_decrypt(stored.get("groq")),
        gemini=_decrypt(stored.get("gemini")),
        telegram_token=telegram_payload.get("token") or telegram_payload.get("bot_token"),
        telegram_chat_id=str(telegram.publish_chat_id or telegram_payload.get("chat_id") or "") or None,
        x_api_key=_decrypt(x_payload.get("api_key")) or _decrypt(x_payload.get("token")),
        x_api_secret=_decrypt(x_payload.get("api_secret")),
        x_access_token=_decrypt(x_payload.get("access_token")),
        x_access_token_secret=_decrypt(x_payload.get("access_token_secret")),
    )


def _groq_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


async def _groq_json(api_key: str, contents: str) -> str:
    payload = {
        "model": os.getenv("GROQ_MODEL", "openai/gpt-oss-20b"),
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": contents},
        ],
        "temperature": 0.5,
        "response_format": {"type": "json_schema", "json_schema": {"name": "web_draft", "strict": True, "schema": DRAFT_RESPONSE_SCHEMA}},
    }
    async with httpx.AsyncClient(timeout=45) as client:
        response = await client.post("https://api.groq.com/openai/v1/chat/completions", headers=_groq_headers(api_key), json=payload)
    if response.status_code != 200:
        raise WorkspaceIntegrationError(f"Workspace Groq returned HTTP {response.status_code}")
    try:
        return response.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise WorkspaceIntegrationError("Workspace Groq returned no draft") from exc


def _draft_prompt(name: str, raw_text: str, chain: str | None, project_url: str | None, previous: DraftResult, feedback: str) -> str:
    return (
        "Public draft language: English only.\n"
        f"Project: {name}\nChain: {chain or 'unknown'}\n"
        "The source URL is private metadata. Do not include it in public copy.\n"
        f"Verified public project/action URL: {project_url or 'not found'}\n"
        "Include that project URL exactly once in twitter_text when provided.\n\n"
        f"Source content:\n{raw_text[:5000]}\n\nPrevious draft:\n{previous.title}\n{previous.summary}\n{previous.instructions}\n"
        f"Previous X copy:\n{previous.twitter_text or 'N/A'}\n\nReviewer feedback:\n{feedback[:1600]}\n"
    )


async def rework_with_workspace_ai(credentials: WorkspaceCredentials, *, name: str, raw_text: str, chain: str | None, project_url: str | None, previous: DraftResult, feedback: str) -> tuple[DraftResult, str]:
    if credentials.groq:
        result = _parse_draft(await _groq_json(credentials.groq, _draft_prompt(name, raw_text, chain, project_url, previous, feedback)))
        if not _needs_repair(result, project_url):
            return result, "Workspace Groq"
    if credentials.gemini:
        client = genai.Client(api_key=credentials.gemini)
        response = await client.aio.models.generate_content(
            model=os.getenv("LLM_MODEL", "gemini-flash-lite-latest"),
            contents=_draft_prompt(name, raw_text, chain, project_url, previous, feedback),
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                response_mime_type="application/json",
                temperature=0.5,
            ),
        )
        result = _parse_draft(response.text)
        if not _needs_repair(result, project_url):
            return result, "Workspace Gemini"
    # A missing/failed workspace key deliberately falls back to local content.
    raise WorkspaceIntegrationError("Workspace AI did not return a valid draft")


def _telegram_caption(draft: Draft) -> str:
    text = draft.rendered_text()
    return text if len(text) <= 1024 else text[:950].rstrip() + (f"\n\n🔗 Start here: {draft.project_url}" if draft.project_url else "")


async def _publish_workspace_telegram(credentials: WorkspaceCredentials, draft: Draft) -> dict[str, Any]:
    if not credentials.telegram_token or not credentials.telegram_chat_id:
        return {"platform": "telegram", "success": False, "error": "Workspace Telegram bot token or publish chat is not configured"}
    bot = Bot(credentials.telegram_token)
    try:
        if draft.image_path:
            message = await bot.send_photo(chat_id=credentials.telegram_chat_id, photo=telegram_photo(draft.image_path), caption=_telegram_caption(draft))
        else:
            message = await bot.send_message(chat_id=credentials.telegram_chat_id, text=draft.rendered_text())
        return {"platform": "telegram", "success": True, "platform_post_id": str(message.message_id), "url": None, "error": None}
    except Exception as exc:
        return {"platform": "telegram", "success": False, "error": str(exc)[:500]}
    finally:
        await bot.session.close()


def _publish_workspace_x_sync(credentials: WorkspaceCredentials, text: str) -> dict[str, Any]:
    if not credentials.has_x:
        return {"platform": "x", "success": False, "error": "Workspace X OAuth 1.0a credentials are incomplete"}
    try:
        response = requests.post("https://api.x.com/2/tweets", json={"text": text}, auth=OAuth1(credentials.x_api_key, credentials.x_api_secret, credentials.x_access_token, credentials.x_access_token_secret), timeout=20)
        if response.status_code != 201:
            return {"platform": "x", "success": False, "error": f"Workspace X returned HTTP {response.status_code}"}
        post_id = str(response.json().get("data", {}).get("id", ""))
        return {"platform": "x", "success": bool(post_id), "platform_post_id": post_id, "url": f"https://x.com/i/web/status/{post_id}" if post_id else None, "error": None if post_id else "X returned no post id"}
    except requests.RequestException as exc:
        return {"platform": "x", "success": False, "error": str(exc)[:500]}


async def publish_with_workspace(credentials: WorkspaceCredentials, project: Project, draft: Draft) -> list[dict[str, Any]]:
    results = [await _publish_workspace_telegram(credentials, draft)]
    if draft.twitter_text:
        results.append(await asyncio.to_thread(_publish_workspace_x_sync, credentials, draft.twitter_text))
    else:
        results.append({"platform": "x", "success": False, "error": "X draft is empty"})
    return results
