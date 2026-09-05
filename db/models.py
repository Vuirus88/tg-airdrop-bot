"""Database models for discovered projects, drafts, and publications."""
import enum
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), nullable=True, unique=True, index=True)
    telegram_user_id: Mapped[int] = mapped_column(Integer, nullable=True, unique=True, index=True)
    auth_subject: Mapped[str] = mapped_column(String(255), nullable=True, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    workspaces: Mapped[list["Workspace"]] = relationship(back_populates="owner")


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped["User"] = relationship()


class Workspace(Base):
    __tablename__ = "workspaces"
    __table_args__ = (UniqueConstraint("owner_id", "slug", name="uq_workspace_owner_slug"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(120))
    plan: Mapped[str] = mapped_column(String(32), default="free")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    owner: Mapped["User"] = relationship(back_populates="workspaces")
    projects: Mapped[list["Project"]] = relationship(back_populates="workspace")
    settings: Mapped["WebSettings | None"] = relationship(back_populates="workspace", uselist=False)
    telegram_integrations: Mapped[list["TelegramIntegration"]] = relationship(back_populates="workspace")
    x_integrations: Mapped[list["XIntegration"]] = relationship(back_populates="workspace")
    subscriptions: Mapped[list["Subscription"]] = relationship(back_populates="workspace")


class Plan(Base):
    """Tariff definition; limits are intentionally data-driven and not enforced yet."""

    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(Text, nullable=True)
    monthly_price_cents: Mapped[int] = mapped_column(Integer, default=0)
    limits_json: Mapped[str] = mapped_column(Text, default="{}")
    features_json: Mapped[str] = mapped_column(Text, default="{}")
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    subscriptions: Mapped[list["Subscription"]] = relationship(back_populates="plan")


class Subscription(Base):
    """Workspace subscription state; payment provider integration comes later."""

    __tablename__ = "subscriptions"
    __table_args__ = (UniqueConstraint("workspace_id", name="uq_subscription_workspace"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="trial")
    provider: Mapped[str] = mapped_column(String(32), nullable=True)
    provider_subscription_id: Mapped[str] = mapped_column(String(255), nullable=True)
    current_period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    current_period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    workspace: Mapped["Workspace"] = relationship(back_populates="subscriptions")
    plan: Mapped["Plan"] = relationship(back_populates="subscriptions")


class UsageCounter(Base):
    """Monthly, workspace-scoped quota counters used by the web SaaS API."""

    __tablename__ = "usage_counters"
    __table_args__ = (UniqueConstraint("workspace_id", "period_start", "metric", name="uq_usage_counter_period"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    metric: Mapped[str] = mapped_column(String(64), index=True)
    count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ReviewRequest(Base):
    """Persistent Telegram review action waiting for administrator feedback."""

    __tablename__ = "review_requests"
    __table_args__ = (UniqueConstraint("project_id", name="uq_review_request_project"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    prompt_chat_id: Mapped[int] = mapped_column(Integer, nullable=True)
    prompt_message_id: Mapped[int] = mapped_column(Integer, nullable=True, index=True)
    message_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    status: Mapped[str] = mapped_column(String(32), default="awaiting_feedback", index=True)
    feedback: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    resolved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)

    project: Mapped["Project"] = relationship()


class AuditEvent(Base):
    """Append-only operational history for bot and web actions."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True, default=1)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=True, index=True)
    actor_type: Mapped[str] = mapped_column(String(32), default="system")
    actor_id: Mapped[str] = mapped_column(String(128), nullable=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    success: Mapped[bool] = mapped_column(default=True)
    detail: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class BackgroundJob(Base):
    """Durable queue item for slow AI, image, and publication-adjacent work."""

    __tablename__ = "background_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True, default=1)
    job_type: Mapped[str] = mapped_column(String(64), index=True)
    payload_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    locked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ProjectStatus(str, enum.Enum):
    NEW = "new"
    FILTERED_OUT = "filtered_out"
    DRAFTED = "drafted"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    DELETED = "deleted"
    PUBLISHED = "published"


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("workspace_id", "dedup_hash", name="uq_project_workspace_dedup_hash"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True, default=1)
    dedup_hash: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(255))
    chain: Mapped[str] = mapped_column(String(64), nullable=True)
    category: Mapped[str] = mapped_column(String(32), default="airdrop")
    source: Mapped[str] = mapped_column(String(64))
    source_url: Mapped[str] = mapped_column(Text, nullable=True)
    project_url: Mapped[str] = mapped_column(Text, nullable=True)
    raw_data: Mapped[str] = mapped_column(Text, nullable=True)
    status: Mapped[ProjectStatus] = mapped_column(
        Enum(ProjectStatus), default=ProjectStatus.NEW, index=True
    )
    legitimacy_score: Mapped[float] = mapped_column(Float, nullable=True)
    score_reasoning: Mapped[str] = mapped_column(Text, nullable=True)
    filter_version: Mapped[int] = mapped_column(Integer, default=1)
    review_chat_id: Mapped[int] = mapped_column(Integer, nullable=True)
    review_message_id: Mapped[int] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    workspace: Mapped["Workspace"] = relationship(back_populates="projects")
    drafts: Mapped[list["Draft"]] = relationship(back_populates="project", order_by="Draft.version")
    published_posts: Mapped[list["PublishedPost"]] = relationship(back_populates="project")

    def latest_draft(self) -> "Draft | None":
        return self.drafts[-1] if self.drafts else None


class Draft(Base):
    __tablename__ = "drafts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    title: Mapped[str] = mapped_column(String(255))
    summary: Mapped[str] = mapped_column(Text)
    instructions: Mapped[str] = mapped_column(Text)
    potential_reward: Mapped[str] = mapped_column(Text, nullable=True)
    risk_note: Mapped[str] = mapped_column(Text, nullable=True)
    twitter_text: Mapped[str] = mapped_column(Text, nullable=True)
    image_path: Mapped[str] = mapped_column(String(512), nullable=True)
    image_prompt: Mapped[str] = mapped_column(Text, nullable=True)
    image_source: Mapped[str] = mapped_column(String(64), nullable=True)
    source_url: Mapped[str] = mapped_column(Text, nullable=True)
    project_url: Mapped[str] = mapped_column(Text, nullable=True)
    rework_feedback: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    project: Mapped["Project"] = relationship(back_populates="drafts")

    def rendered_text(self) -> str:
        parts = [f"🚀 {self.title}", "", self.summary, "", "📝 What to do:", self.instructions]
        if self.potential_reward:
            parts += ["", f"💰 Potential reward: {self.potential_reward}"]
        if self.risk_note:
            parts += ["", f"⚠️ Risk: {self.risk_note}"]
        if self.project_url:
            parts += ["", f"🔗 Start here: {self.project_url}"]
        return "\n".join(parts)

    def rendered_review_text(self) -> str:
        parts = [
            "🔒 Источник (виден только администратору, в пост не попадет):",
            self.source_url or "Источник не указан",
            "",
            "Ссылка на проект (попадет в публичные посты):",
            self.project_url or "⚠️ Не найдена — автоматическая публикация заблокирована",
            "",
            "1. Черновик для телеграмм канала",
            "",
            self.rendered_text(),
        ]
        if self.twitter_text:
            parts += ["", "2. Черновик для твиттера", "", self.twitter_text]
        parts += ["", "----------", "", "3. Изображение"]
        if self.image_path:
            if self.image_source == "generated_social_card_cloudflare":
                label = "AI social card (Cloudflare Workers AI + локальный макет)"
            elif self.image_source == "generated_social_card":
                label = "Бесплатно сгенерированная social card"
            else:
                label = "Рекомендуемое изображение со страницы источника"
            parts.append(f"{label}: {self.image_path}")
        else:
            parts.append("Подходящее официальное изображение автоматически не найдено.")
        if self.image_prompt:
            parts += ["", "Промпт для генерации:", self.image_prompt]
        return "\n".join(parts)


class PublishedPost(Base):
    __tablename__ = "published_posts"
    __table_args__ = (
        UniqueConstraint(
            "draft_id", "platform", name="uq_published_post_draft_platform"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("drafts.id"))
    platform: Mapped[str] = mapped_column(String(32))
    platform_post_id: Mapped[str] = mapped_column(String(255), nullable=True)
    url: Mapped[str] = mapped_column(Text, nullable=True)
    success: Mapped[bool] = mapped_column(default=True)
    error: Mapped[str] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    project: Mapped["Project"] = relationship(back_populates="published_posts")


class WebSettings(Base):
    """Single-tenant website preferences.

    The MVP deliberately keeps this separate from environment-owned bot settings.
    A public SaaS migration should add a user_id and one row per workspace.
    """

    __tablename__ = "web_settings"
    __table_args__ = (UniqueConstraint("workspace_id", name="uq_web_settings_workspace"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True, default=1)
    filter_prompt: Mapped[str] = mapped_column(Text, default="")
    rss_feeds: Mapped[str] = mapped_column(Text, default="[]")
    social_accounts: Mapped[str] = mapped_column(Text, default="[]")
    enabled_platforms: Mapped[str] = mapped_column(Text, default='["telegram", "x"]')
    encrypted_credentials: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    workspace: Mapped["Workspace"] = relationship(back_populates="settings")


class TelegramIntegration(Base):
    __tablename__ = "telegram_integrations"
    __table_args__ = (UniqueConstraint("workspace_id", "telegram_user_id", name="uq_telegram_workspace_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    telegram_user_id: Mapped[int] = mapped_column(Integer, nullable=True, index=True)
    bot_username: Mapped[str] = mapped_column(String(64), nullable=True)
    review_chat_id: Mapped[int] = mapped_column(Integer, nullable=True)
    publish_chat_id: Mapped[int] = mapped_column(Integer, nullable=True)
    encrypted_bot_token: Mapped[str] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    workspace: Mapped["Workspace"] = relationship(back_populates="telegram_integrations")


class XIntegration(Base):
    __tablename__ = "x_integrations"
    __table_args__ = (UniqueConstraint("workspace_id", "x_user_id", name="uq_x_workspace_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    x_user_id: Mapped[str] = mapped_column(String(128), nullable=True, index=True)
    username: Mapped[str] = mapped_column(String(64), nullable=True)
    encrypted_credentials: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(32), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    workspace: Mapped["Workspace"] = relationship(back_populates="x_integrations")
