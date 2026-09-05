from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from collections import defaultdict, deque
from contextvars import ContextVar
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import quote

from aiogram import Bot
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import delete, desc, select
from sqlalchemy.orm import selectinload

from config import settings
from db.database import get_session, init_db
from db.models import (
    Draft,
    AuthSession,
    Project,
    ProjectStatus,
    Plan,
    Subscription,
    UsageCounter,
    TelegramIntegration,
    WebSettings,
    Workspace,
    User,
    XIntegration,
)
from publishing.state import archive_project_for_review, claim_project_for_publication, finish_project_publication
from services.ai_rework import rework_draft
from services.fallback_content import fallback_generate_draft
from services.health import collect_system_health
from services.llm_draft import DraftResult
from services.safe_http import SafeHTTPError, safe_get_bytes
from web.integrations import (
    WorkspaceIntegrationError,
    load_workspace_credentials,
    publish_with_workspace,
    rework_with_workspace_ai,
)


ROOT = Path(__file__).resolve().parents[1]
STATIC = Path(__file__).resolve().parent / "static"
CURRENT_WORKSPACE_ID = 1
AUTH_COOKIE = "alpha_radar_session"
CSRF_COOKIE = "alpha_radar_csrf"
_workspace_context: ContextVar[int] = ContextVar("workspace_id", default=CURRENT_WORKSPACE_ID)
_auth_attempts: dict[str, deque[datetime]] = defaultdict(deque)
FREE_PLAN_LIMITS = {"ai_requests": 25, "publishes": 10, "integrations": 3}
FREE_PLAN_FEATURES = {"workspace_ai": True, "telegram_publish": True, "x_publish": True}


def current_workspace_id() -> int:
    return _workspace_context.get()


def _password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16_384, r=8, p=1)
    return f"scrypt$16384$8$1${salt.hex()}${digest.hex()}"


def _password_matches(password: str, encoded: str | None) -> bool:
    try:
        _, n, r, p, salt_hex, digest_hex = (encoded or "").split("$")
        actual = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt_hex),
            n=int(n), r=int(r), p=int(p),
        )
        return hmac.compare_digest(actual.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _rate_limit(request: Request, action: str, *, limit: int, window: timedelta) -> None:
    """Small in-process guard for expensive unauthenticated auth endpoints."""
    key = f"{action}:{_client_ip(request)}"
    now = datetime.now(timezone.utc)
    attempts = _auth_attempts[key]
    cutoff = now - window
    while attempts and attempts[0] <= cutoff:
        attempts.popleft()
    if len(attempts) >= limit:
        raise HTTPException(429, "Too many attempts. Please try again later.")
    attempts.append(now)


def _set_auth_cookies(response: JSONResponse, token: str) -> None:
    response.set_cookie(
        AUTH_COOKIE, token, httponly=True, samesite="lax", secure=settings.WEB_COOKIE_SECURE,
        max_age=30 * 86400,
    )


def _billing_period_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def _subscription_for_workspace(session, workspace_id: int) -> tuple[Subscription, Plan]:
    """Create the Free tariff and one active subscription when they do not exist."""
    plan = await session.scalar(select(Plan).where(Plan.code == "free"))
    if not plan:
        plan = Plan(
            code="free", name="Free", description="Starter workspace plan", monthly_price_cents=0,
            limits_json=json.dumps(FREE_PLAN_LIMITS), features_json=json.dumps(FREE_PLAN_FEATURES),
        )
        session.add(plan)
        await session.flush()
    subscription = await session.scalar(
        select(Subscription).where(Subscription.workspace_id == workspace_id)
    )
    if not subscription:
        start = _billing_period_start()
        subscription = Subscription(
            workspace_id=workspace_id, plan_id=plan.id, status="active",
            current_period_start=start, current_period_end=(start + timedelta(days=32)).replace(day=1),
        )
        session.add(subscription)
        await session.flush()
    active_plan = await session.get(Plan, subscription.plan_id) if subscription.plan_id else plan
    return subscription, active_plan or plan


async def _quota(session, metric: str) -> tuple[int, int | None]:
    _, plan = await _subscription_for_workspace(session, current_workspace_id())
    limits = json.loads(plan.limits_json or "{}")
    limit = limits.get(metric)
    row = await session.scalar(
        select(UsageCounter).where(
            UsageCounter.workspace_id == current_workspace_id(),
            UsageCounter.period_start == _billing_period_start(),
            UsageCounter.metric == metric,
        )
    )
    return (row.count if row else 0), int(limit) if limit is not None else None


async def _require_quota(session, metric: str) -> None:
    used, limit = await _quota(session, metric)
    if limit is not None and used >= limit:
        raise HTTPException(429, f"Monthly {metric.replace('_', ' ')} limit reached ({limit}).")


async def _consume_quota(session, metric: str) -> None:
    period_start = _billing_period_start()
    row = await session.scalar(
        select(UsageCounter).where(
            UsageCounter.workspace_id == current_workspace_id(),
            UsageCounter.period_start == period_start,
            UsageCounter.metric == metric,
        )
    )
    if not row:
        row = UsageCounter(workspace_id=current_workspace_id(), period_start=period_start, metric=metric, count=0)
        session.add(row)
    row.count += 1


async def _integration_count(session) -> int:
    row = await _settings_row(session)
    names = set(json.loads(row.encrypted_credentials or "{}"))
    telegram = await session.scalar(
        select(TelegramIntegration).where(TelegramIntegration.workspace_id == current_workspace_id())
    )
    x_integration = await session.scalar(
        select(XIntegration).where(XIntegration.workspace_id == current_workspace_id())
    )
    if telegram and telegram.encrypted_bot_token:
        names.add("telegram")
    if x_integration and x_integration.encrypted_credentials:
        names.add("x")
    return len(names)
    response.set_cookie(
        CSRF_COOKIE, secrets.token_urlsafe(32), httponly=False, samesite="lax",
        secure=settings.WEB_COOKIE_SECURE, max_age=30 * 86400,
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    if settings.WEB_ACCESS_MODE == "public" and settings.WEB_AUTH_MODE != "required":
        raise RuntimeError("WEB_ACCESS_MODE=public requires WEB_AUTH_MODE=required")
    await init_db()
    yield


app = FastAPI(title="Alpha Radar", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def private_mvp_access(request, call_next):
    """Keep the unauthenticated MVP dashboard local until SaaS auth exists."""
    if settings.WEB_ACCESS_MODE == "private":
        host = request.client.host if request.client else ""
        try:
            is_loopback = ip_address(host).is_loopback
        except ValueError:
            is_loopback = False
        if not is_loopback:
            return JSONResponse(
                status_code=403,
                content={
                    "detail": (
                        "The MVP dashboard is private. Run it on localhost; "
                        "public access requires user authentication and tenant isolation."
                    )
                },
            )

    token = request.cookies.get(AUTH_COOKIE)
    context_token = _workspace_context.set(CURRENT_WORKSPACE_ID)
    try:
        if settings.WEB_AUTH_MODE == "required" and request.url.path.startswith("/api/"):
            is_auth_bootstrap = request.url.path in {"/api/auth/register", "/api/auth/login", "/api/auth/status"}
            if request.method not in {"GET", "HEAD", "OPTIONS"} and not is_auth_bootstrap:
                csrf_cookie = request.cookies.get(CSRF_COOKIE)
                csrf_header = request.headers.get("X-CSRF-Token")
                if not csrf_cookie or not csrf_header or not hmac.compare_digest(csrf_cookie, csrf_header):
                    return JSONResponse(status_code=403, content={"detail": "CSRF validation failed"})
            if request.url.path.startswith("/api/auth/"):
                pass
            elif not token:
                return JSONResponse(status_code=401, content={"detail": "Authentication required"})
            else:
                async with get_session() as session:
                    auth = await session.scalar(
                        select(AuthSession).where(
                            AuthSession.token_hash == _token_hash(token),
                            AuthSession.expires_at > datetime.now(timezone.utc),
                        )
                    )
                    user = await session.get(User, auth.user_id) if auth else None
                    workspace = await session.scalar(
                        select(Workspace).where(Workspace.owner_id == user.id).order_by(Workspace.id)
                    ) if user else None
                if not user or not workspace:
                    return JSONResponse(status_code=401, content={"detail": "Invalid or expired session"})
                _workspace_context.set(workspace.id)

        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        return response
    finally:
        _workspace_context.reset(context_token)


class ReworkRequest(BaseModel):
    feedback: str = Field(min_length=3, max_length=2000)


class SettingsRequest(BaseModel):
    filter_prompt: str = Field(default="", max_length=6000)
    rss_feeds: list[str] = Field(default_factory=list, max_length=50)
    social_accounts: list[str] = Field(default_factory=list, max_length=100)
    enabled_platforms: list[str] = Field(default_factory=list)
    credentials: dict[str, str] = Field(default_factory=dict)


class AuthRequest(BaseModel):
    email: str = Field(min_length=5, max_length=320)
    password: str = Field(min_length=8, max_length=128)


def _project_query(workspace_id: int = CURRENT_WORKSPACE_ID):
    return select(Project).options(
        selectinload(Project.drafts), selectinload(Project.published_posts)
    ).where(Project.workspace_id == workspace_id)


def _draft_json(draft: Draft | None) -> dict | None:
    if not draft:
        return None
    return {
        "id": draft.id,
        "version": draft.version,
        "title": draft.title,
        "summary": draft.summary,
        "instructions": draft.instructions,
        "potential_reward": draft.potential_reward,
        "risk_note": draft.risk_note,
        "twitter_text": draft.twitter_text,
        "project_url": draft.project_url,
        "source_url": draft.source_url,
        "image_url": f"/api/posts/{draft.project_id}/image" if draft.image_path else None,
        "rework_feedback": draft.rework_feedback,
        "created_at": draft.created_at.isoformat() if draft.created_at else None,
    }


def _project_json(project: Project) -> dict:
    draft = project.latest_draft()
    publications = [
        {
            "platform": item.platform,
            "success": item.success,
            "url": item.url,
            "error": item.error,
            "published_at": item.published_at.isoformat() if item.published_at else None,
        }
        for item in project.published_posts
    ]
    return {
        "id": project.id,
        "name": project.name,
        "chain": project.chain,
        "category": project.category,
        "source": project.source,
        "source_url": project.source_url,
        "project_url": project.project_url,
        "status": project.status.value,
        "score": project.legitimacy_score,
        "score_reasoning": project.score_reasoning,
        "created_at": project.created_at.isoformat() if project.created_at else None,
        "updated_at": project.updated_at.isoformat() if project.updated_at else None,
        "draft": _draft_json(draft),
        "publications": publications,
    }


async def _load_project(project_id: int) -> Project:
    async with get_session() as session:
        result = await session.execute(_project_query().where(Project.id == project_id))
        project = result.scalar_one_or_none()
        if not project:
            raise HTTPException(404, "Post not found")
        return project


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.post("/api/auth/register")
async def register(payload: AuthRequest, request: Request):
    _rate_limit(request, "register", limit=5, window=timedelta(hours=1))
    email = payload.email.strip().lower()
    async with get_session() as session:
        existing = await session.scalar(select(User).where(User.email == email))
        if existing:
            raise HTTPException(409, "An account with this email already exists")
        user = User(email=email, password_hash=_password_hash(payload.password))
        session.add(user)
        await session.flush()
        workspace = Workspace(
            owner_id=user.id,
            name=f"{email.split('@')[0]}'s workspace",
            slug=f"personal-{user.id}",
            plan="free",
        )
        session.add(workspace)
        await session.flush()
        await _subscription_for_workspace(session, workspace.id)
        token = secrets.token_urlsafe(32)
        session.add(
            AuthSession(
                user_id=user.id,
                token_hash=_token_hash(token),
                expires_at=datetime.now(timezone.utc) + timedelta(days=30),
            )
        )
        await session.commit()
    response = JSONResponse({"ok": True, "email": email, "workspace_id": workspace.id})
    _set_auth_cookies(response, token)
    return response


@app.get("/api/auth/status")
async def auth_status():
    return {"required": settings.WEB_AUTH_MODE == "required"}


@app.post("/api/auth/login")
async def login(payload: AuthRequest, request: Request):
    _rate_limit(request, "login", limit=10, window=timedelta(minutes=15))
    email = payload.email.strip().lower()
    async with get_session() as session:
        await session.execute(delete(AuthSession).where(AuthSession.expires_at <= datetime.now(timezone.utc)))
        user = await session.scalar(select(User).where(User.email == email))
        if not user or not _password_matches(payload.password, user.password_hash):
            raise HTTPException(401, "Invalid email or password")
        workspace = await session.scalar(
            select(Workspace).where(Workspace.owner_id == user.id).order_by(Workspace.id)
        )
        if not workspace:
            raise HTTPException(503, "Personal workspace is not initialized")
        token = secrets.token_urlsafe(32)
        session.add(
            AuthSession(
                user_id=user.id,
                token_hash=_token_hash(token),
                expires_at=datetime.now(timezone.utc) + timedelta(days=30),
            )
        )
        await session.commit()
    response = JSONResponse({"ok": True, "email": email, "workspace_id": workspace.id})
    _set_auth_cookies(response, token)
    return response


@app.post("/api/auth/logout")
async def logout(request: Request):
    token = request.cookies.get(AUTH_COOKIE)
    if token:
        async with get_session() as session:
            auth = await session.scalar(select(AuthSession).where(AuthSession.token_hash == _token_hash(token)))
            if auth:
                await session.delete(auth)
                await session.commit()
    response = JSONResponse({"ok": True})
    response.delete_cookie(AUTH_COOKIE, secure=settings.WEB_COOKIE_SECURE, samesite="lax")
    response.delete_cookie(CSRF_COOKIE, secure=settings.WEB_COOKIE_SECURE, samesite="lax")
    return response


@app.post("/api/auth/logout-all")
async def logout_all(request: Request):
    token = request.cookies.get(AUTH_COOKIE)
    if not token:
        raise HTTPException(401, "Not authenticated")
    async with get_session() as session:
        auth = await session.scalar(select(AuthSession).where(AuthSession.token_hash == _token_hash(token)))
        if not auth:
            raise HTTPException(401, "Invalid or expired session")
        await session.execute(delete(AuthSession).where(AuthSession.user_id == auth.user_id))
        await session.commit()
    response = JSONResponse({"ok": True})
    response.delete_cookie(AUTH_COOKIE, secure=settings.WEB_COOKIE_SECURE, samesite="lax")
    response.delete_cookie(CSRF_COOKIE, secure=settings.WEB_COOKIE_SECURE, samesite="lax")
    return response


@app.get("/api/auth/me")
async def auth_me(request: Request):
    token = request.cookies.get(AUTH_COOKIE)
    if not token:
        raise HTTPException(401, "Not authenticated")
    async with get_session() as session:
        auth = await session.scalar(
            select(AuthSession).where(
                AuthSession.token_hash == _token_hash(token),
                AuthSession.expires_at > datetime.now(timezone.utc),
            )
        )
        user = await session.get(User, auth.user_id) if auth else None
        workspace = await session.scalar(
            select(Workspace).where(Workspace.owner_id == user.id).order_by(Workspace.id)
        ) if user else None
    if not user or not workspace:
        raise HTTPException(401, "Invalid or expired session")
    return {"id": user.id, "email": user.email, "workspace_id": workspace.id, "plan": workspace.plan}


@app.get("/api/dashboard")
async def dashboard():
    async with get_session() as session:
        result = await session.execute(
            _project_query()
            .where(Project.status.in_([ProjectStatus.PENDING_REVIEW, ProjectStatus.DRAFTED]))
            .order_by(desc(Project.updated_at))
        )
        posts = [_project_json(item) for item in result.scalars().unique().all()]
        archive_count = await session.scalar(
            select(__import__("sqlalchemy").func.count(Project.id)).where(
                Project.workspace_id == current_workspace_id(),
                Project.status.in_([ProjectStatus.DELETED, ProjectStatus.PUBLISHED])
            )
        )
    return {"posts": posts, "count": len(posts), "archive_count": archive_count or 0}


@app.get("/api/archive")
async def archive():
    async with get_session() as session:
        result = await session.execute(
            _project_query()
            .where(Project.status.in_([ProjectStatus.DELETED, ProjectStatus.PUBLISHED]))
            .order_by(desc(Project.updated_at))
        )
        posts = [_project_json(item) for item in result.scalars().unique().all()]
    return {"posts": posts, "count": len(posts)}


@app.get("/api/posts/{project_id}")
async def post_detail(project_id: int):
    return _project_json(await _load_project(project_id))


@app.get("/api/posts/{project_id}/image")
async def post_image(project_id: int):
    project = await _load_project(project_id)
    draft = project.latest_draft()
    if not draft or not draft.image_path:
        raise HTTPException(404, "Image not found")
    if draft.image_path.startswith(("http://", "https://")):
        try:
            remote = await safe_get_bytes(
                draft.image_path,
                max_bytes=8 * 1024 * 1024,
                allowed_content_types=("image/",),
            )
        except SafeHTTPError as exc:
            raise HTTPException(502, f"Remote image was rejected: {exc}") from exc
        media_type = remote.headers.get("content-type", "image/jpeg").split(";", 1)[0]
        return Response(
            content=remote.content,
            media_type=media_type,
            headers={"Cache-Control": "private, max-age=300"},
        )
    path = Path(draft.image_path)
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()
    if not path.is_file() or ROOT.resolve() not in path.parents:
        raise HTTPException(404, "Image not found")
    return FileResponse(path)


@app.post("/api/posts/{project_id}/delete")
async def delete_post(project_id: int):
    async with get_session() as session:
        project = await session.scalar(
            select(Project).where(
                Project.id == project_id,
                Project.workspace_id == current_workspace_id(),
            )
        )
        if not project:
            raise HTTPException(404, "Post not found")
        deleted = await archive_project_for_review(session, project.id)
        if not deleted:
            raise HTTPException(409, "Only unpublished review posts can be deleted")
        await session.commit()
    return {"ok": True, "status": "deleted"}


@app.post("/api/posts/{project_id}/restore")
async def restore_post(project_id: int):
    async with get_session() as session:
        project = await session.scalar(
            select(Project).where(
                Project.id == project_id,
                Project.workspace_id == current_workspace_id(),
            )
        )
        if not project:
            raise HTTPException(404, "Post not found")
        if project.status != ProjectStatus.DELETED:
            raise HTTPException(409, "Only deleted posts can be restored")
        project.status = ProjectStatus.PENDING_REVIEW
        await session.commit()
    return {"ok": True, "status": "pending_review"}


@app.post("/api/posts/{project_id}/rework")
async def rework_post(project_id: int, payload: ReworkRequest):
    async with get_session() as session:
        result = await session.execute(
            select(Project).options(selectinload(Project.drafts)).where(
                Project.id == project_id,
                Project.workspace_id == current_workspace_id(),
            )
        )
        project = result.scalar_one_or_none()
        if not project or not project.latest_draft():
            raise HTTPException(404, "Post or draft not found")
        if project.status not in (ProjectStatus.PENDING_REVIEW, ProjectStatus.DRAFTED):
            raise HTTPException(409, "Only unpublished review posts can be reworked")
        await _require_quota(session, "ai_requests")
        old = project.latest_draft()
        previous = DraftResult(
            title=old.title,
            summary=old.summary,
            instructions=old.instructions,
            potential_reward=old.potential_reward,
            risk_note=old.risk_note,
            twitter_text=old.twitter_text,
            image_prompt=old.image_prompt,
        )
        try:
            credentials = await load_workspace_credentials(session, current_workspace_id())
            generated, provider = await rework_with_workspace_ai(
                credentials,
                name=project.name,
                raw_text=project.raw_data or "",
                chain=project.chain,
                project_url=project.project_url,
                previous=previous,
                feedback=payload.feedback,
            )
        except WorkspaceIntegrationError:
            generated = fallback_generate_draft(
                project.name, project.raw_data or old.summary, project.chain,
                project.category, project.project_url,
            )
            provider = "local fallback"
        new_draft = Draft(
            project_id=project.id,
            version=old.version + 1,
            title=generated.title,
            summary=generated.summary,
            instructions=generated.instructions,
            potential_reward=generated.potential_reward,
            risk_note=generated.risk_note,
            twitter_text=generated.twitter_text,
            image_path=old.image_path,
            image_source=old.image_source,
            image_prompt=generated.image_prompt,
            source_url=project.source_url,
            project_url=project.project_url,
            rework_feedback=payload.feedback,
        )
        project.drafts.append(new_draft)
        project.status = ProjectStatus.PENDING_REVIEW
        await _consume_quota(session, "ai_requests")
        await session.commit()
        await session.refresh(new_draft)
    return {"ok": True, "provider": provider, "draft": _draft_json(new_draft)}


@app.post("/api/posts/{project_id}/approve")
async def approve_post(project_id: int):
    async with get_session() as session:
            result = await session.execute(
                select(Project).options(selectinload(Project.drafts)).where(
                    Project.id == project_id,
                    Project.workspace_id == current_workspace_id(),
                )
            )
            project = result.scalar_one_or_none()
            if not project or not project.latest_draft():
                raise HTTPException(404, "Post or draft not found")
            if not project.latest_draft().project_url:
                raise HTTPException(409, "A verified project URL is required before publishing")
            if project.status == ProjectStatus.PUBLISHED:
                raise HTTPException(409, "Post is already published")
            await _require_quota(session, "publishes")
            claimed = await claim_project_for_publication(session, project.id)
            if not claimed:
                raise HTTPException(409, "Post is already being published or was processed")
            await session.commit()
            credentials = await load_workspace_credentials(session, current_workspace_id())
            results = await publish_with_workspace(credentials, project, project.latest_draft())
            telegram = next(item for item in results if item["platform"] == "telegram")
            await finish_project_publication(session, project.id, telegram["success"])
            if any(item["success"] for item in results):
                await _consume_quota(session, "publishes")
                await session.commit()
    return {"ok": telegram["success"], "results": results}


def _fernet() -> Fernet:
    secret = os.getenv("WEB_SECRET_KEY", "").strip()
    if not secret:
        raise HTTPException(503, "WEB_SECRET_KEY is required to save credentials")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


async def _settings_row(session) -> WebSettings:
    row = await session.scalar(
        select(WebSettings).where(WebSettings.workspace_id == current_workspace_id())
    )
    if not row:
        row = WebSettings(
            workspace_id=current_workspace_id(),
            filter_prompt=(
                "Find actionable airdrops, testnets and quests. Reject price news, "
                "generic market analysis and projects with critical scam signals."
            ),
            rss_feeds=json.dumps(settings.RSS_FEEDS),
            social_accounts=json.dumps(settings.TRUSTED_X_ACCOUNTS),
        )
        session.add(row)
        await session.flush()
    return row


@app.get("/api/settings")
async def get_settings():
    async with get_session() as session:
        row = await _settings_row(session)
        encrypted = json.loads(row.encrypted_credentials or "{}")
        telegram = await session.scalar(
            select(TelegramIntegration).where(
                TelegramIntegration.workspace_id == current_workspace_id()
            )
        )
        x_integration = await session.scalar(
            select(XIntegration).where(XIntegration.workspace_id == current_workspace_id())
        )
        await session.commit()
    return {
            "filter_prompt": row.filter_prompt,
            "rss_feeds": json.loads(row.rss_feeds or "[]"),
            "social_accounts": json.loads(row.social_accounts or "[]"),
            "enabled_platforms": json.loads(row.enabled_platforms or "[]"),
            "connections": {
                name: True for name in encrypted
            } | {
                "groq": bool(settings.GROQ_API_KEY) or "groq" in encrypted,
                "gemini": bool(settings.GEMINI_API_KEY) or "gemini" in encrypted,
                "telegram": bool(settings.BOT_TOKEN) or bool(telegram and telegram.encrypted_bot_token),
                "x": bool(settings.X_API_KEY) or bool(x_integration and x_integration.encrypted_credentials),
            },
        }


@app.get("/api/workspace")
async def get_workspace():
    """Return the current workspace shell; auth will select it in the SaaS stage."""
    async with get_session() as session:
        workspace = await session.get(Workspace, current_workspace_id())
        if not workspace:
            raise HTTPException(503, "Workspace is not initialized")
        return {
            "id": workspace.id,
            "name": workspace.name,
            "slug": workspace.slug,
            "plan": workspace.plan,
            "owner_id": workspace.owner_id,
        }


@app.get("/api/subscription")
async def get_subscription():
    """Return the active plan and its current monthly usage."""
    async with get_session() as session:
        subscription, plan = await _subscription_for_workspace(session, current_workspace_id())
        limits = json.loads(plan.limits_json or "{}")
        usage = {}
        for metric in limits:
            used, limit = await _quota(session, metric)
            if metric == "integrations":
                used = await _integration_count(session)
            usage[metric] = {"used": used, "limit": limit}
        await session.commit()
    return {
        "plan_code": plan.code,
        "plan_name": plan.name,
        "status": subscription.status,
        "limits": limits,
        "usage": usage,
        "features": json.loads(plan.features_json or "{}"),
        "billing_enabled": False,
    }


@app.put("/api/settings")
async def save_settings(payload: SettingsRequest):
    async with get_session() as session:
        row = await _settings_row(session)
        row.filter_prompt = payload.filter_prompt.strip()
        row.rss_feeds = json.dumps([item.strip() for item in payload.rss_feeds if item.strip()])
        row.social_accounts = json.dumps(
            [item.strip().lstrip("@") for item in payload.social_accounts if item.strip()]
        )
        row.enabled_platforms = json.dumps(payload.enabled_platforms)
        if payload.credentials:
            cipher = _fernet()
            encrypted = json.loads(row.encrypted_credentials or "{}")
            telegram = await session.scalar(
                select(TelegramIntegration).where(TelegramIntegration.workspace_id == current_workspace_id())
            )
            x_integration = await session.scalar(
                select(XIntegration).where(XIntegration.workspace_id == current_workspace_id())
            )
            connected = set(encrypted)
            if telegram and telegram.encrypted_bot_token:
                connected.add("telegram")
            if x_integration and x_integration.encrypted_credentials:
                connected.add("x")
            _, plan = await _subscription_for_workspace(session, current_workspace_id())
            integration_limit = json.loads(plan.limits_json or "{}").get("integrations")
            requested = connected | {name for name, value in payload.credentials.items() if value.strip()}
            if integration_limit is not None and len(requested) > int(integration_limit):
                raise HTTPException(429, f"Monthly plan allows up to {integration_limit} integrations")
            for name, value in payload.credentials.items():
                clean = value.strip()
                if clean:
                    encrypted_value = cipher.encrypt(clean.encode()).decode()
                    if name == "telegram":
                        try:
                            parsed = json.loads(clean)
                        except json.JSONDecodeError:
                            parsed = {"token": clean}
                        integration = await session.scalar(
                            select(TelegramIntegration).where(
                                TelegramIntegration.workspace_id == current_workspace_id()
                            )
                        )
                        if not integration:
                            integration = TelegramIntegration(workspace_id=current_workspace_id())
                            session.add(integration)
                        token = str(parsed.get("token") or parsed.get("bot_token") or "").strip()
                        if not token:
                            raise HTTPException(422, "Telegram bot token is required")
                        parsed["token"] = token
                        integration.encrypted_bot_token = cipher.encrypt(json.dumps(parsed).encode()).decode()
                        integration.publish_chat_id = int(parsed["chat_id"]) if str(parsed.get("chat_id", "")).lstrip("-").isdigit() else None
                        integration.status = "connected"
                    elif name == "x":
                        try:
                            parsed = json.loads(clean)
                        except json.JSONDecodeError:
                            parsed = {"api_key": clean}
                        integration = await session.scalar(
                            select(XIntegration).where(
                                XIntegration.workspace_id == current_workspace_id()
                            )
                        )
                        if not integration:
                            integration = XIntegration(workspace_id=current_workspace_id())
                            session.add(integration)
                        encrypted_fields = {
                            key: cipher.encrypt(str(value).strip().encode()).decode()
                            for key, value in parsed.items()
                            if str(value).strip()
                        }
                        required = {"api_key", "api_secret", "access_token", "access_token_secret"}
                        if not required.issubset(encrypted_fields):
                            raise HTTPException(422, "X requires api_key, api_secret, access_token and access_token_secret")
                        integration.encrypted_credentials = json.dumps(encrypted_fields)
                        integration.status = "connected"
                    else:
                        encrypted[name] = encrypted_value
            row.encrypted_credentials = json.dumps(encrypted)
        await session.commit()
    return {"ok": True}


@app.get("/api/health")
async def health(live: bool = Query(False)):
    if not live:
        items = [
            {"name": "Database", "working": True, "detail": "SQLite connected"},
            {"name": "Groq", "working": bool(settings.GROQ_API_KEY), "detail": "Configured" if settings.GROQ_API_KEY else "Local fallback enabled"},
            {"name": "Gemini", "working": bool(settings.GEMINI_API_KEY), "detail": "Configured" if settings.GEMINI_API_KEY else "Optional"},
            {"name": "Telegram", "working": bool(settings.BOT_TOKEN), "detail": "Bot token configured"},
            {"name": "X / Twitter", "working": bool(settings.X_API_KEY), "detail": "Automatic publishing" if settings.X_API_KEY else "Open in X fallback"},
        ]
        return {"items": items, "live": False}
    if not settings.BOT_TOKEN:
        raise HTTPException(
            503,
            "Live Telegram healthcheck is unavailable because Telegram is not connected.",
        )
    bot = Bot(settings.BOT_TOKEN)
    try:
        status = await collect_system_health(bot)
    finally:
        await bot.session.close()
    items = [item.__dict__ for item in status.sources]
    items += [item.__dict__ for item in (status.telegram, status.x, status.groq, status.gemini, status.cloudflare)]
    return {"items": items, "recommendations": status.recommendations, "live": True}


@app.get("/api/open-in-x/{project_id}")
async def open_in_x(project_id: int):
    project = await _load_project(project_id)
    draft = project.latest_draft()
    if not draft or not draft.twitter_text:
        raise HTTPException(404, "X draft not found")
    return RedirectResponse(f"https://twitter.com/intent/tweet?text={quote(draft.twitter_text)}")
