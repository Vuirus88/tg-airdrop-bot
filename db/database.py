"""
Async engine + session factory. Call init_db() once on startup.
"""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from config import settings
from db.models import Base, Project, ProjectStatus, User, WebSettings, Workspace

engine = create_async_engine(settings.DATABASE_URL, echo=False)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # The helpers below repair historical SQLite databases. PostgreSQL (Neon)
        # starts from the SQLAlchemy schema, where SQLite PRAGMA statements and
        # table rebuilds are neither needed nor valid.
        if settings.DATABASE_URL.startswith("sqlite"):
            await _ensure_optional_columns(conn)
            await _ensure_project_workspace_constraint(conn)
            await _ensure_project_foreign_keys(conn)
            await _ensure_published_foreign_keys(conn)
            await _ensure_publication_index(conn)
    await _ensure_default_workspace()
    await recover_stale_publications()
    from services.job_queue import recover_stale_jobs

    await recover_stale_jobs()


async def _ensure_publication_index(conn) -> None:
    """Add the idempotency index to existing SQLite installations."""
    await conn.execute(
        text(
            "DELETE FROM published_posts "
            "WHERE id NOT IN ("
            "SELECT MAX(id) FROM published_posts GROUP BY draft_id, platform"
            ")"
        )
    )
    await conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_published_post_draft_platform "
            "ON published_posts (draft_id, platform)"
        )
    )


async def _ensure_project_workspace_constraint(conn) -> None:
    """Replace the former global dedup constraint with a tenant-scoped one."""
    result = await conn.execute(text("PRAGMA index_list(projects)"))
    indexes = result.fetchall()
    for row in indexes:
        index_name = row[1]
        if not row[2]:
            continue
        columns = await conn.execute(text(f"PRAGMA index_info({index_name})"))
        names = [item[2] for item in columns.fetchall()]
        if names == ["workspace_id", "dedup_hash"]:
            return

    old_indexes = [row[1] for row in indexes if row[1].startswith("ix_projects_")]
    await conn.execute(text("PRAGMA foreign_keys=OFF"))
    await conn.execute(text("ALTER TABLE projects RENAME TO projects_legacy"))
    for index_name in old_indexes:
        await conn.execute(text(f"DROP INDEX IF EXISTS {index_name}"))
    await conn.execute(
        text(
            "CREATE TABLE projects ("
            "id INTEGER PRIMARY KEY, workspace_id INTEGER NOT NULL DEFAULT 1, "
            "dedup_hash VARCHAR(64) NOT NULL, name VARCHAR(255) NOT NULL, "
            "chain VARCHAR(64), category VARCHAR(32) NOT NULL DEFAULT 'airdrop', "
            "source VARCHAR(64) NOT NULL, source_url TEXT, project_url TEXT, raw_data TEXT, "
            "status VARCHAR(32) NOT NULL, legitimacy_score FLOAT, score_reasoning TEXT, "
            "filter_version INTEGER NOT NULL DEFAULT 1, review_chat_id INTEGER, "
            "review_message_id INTEGER, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, "
            "CONSTRAINT uq_project_workspace_dedup_hash UNIQUE (workspace_id, dedup_hash), "
            "FOREIGN KEY(workspace_id) REFERENCES workspaces(id)"
            ")"
        )
    )
    await conn.execute(
        text(
            "INSERT INTO projects SELECT id, workspace_id, dedup_hash, name, chain, category, "
            "source, source_url, project_url, raw_data, status, legitimacy_score, score_reasoning, "
            "filter_version, review_chat_id, review_message_id, created_at, updated_at "
            "FROM projects_legacy"
        )
    )
    await conn.execute(text("DROP TABLE projects_legacy"))
    await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_projects_status ON projects(status)"))
    await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_projects_dedup_hash ON projects(dedup_hash)"))
    await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_projects_workspace_id ON projects(workspace_id)"))
    await conn.execute(text("PRAGMA foreign_keys=ON"))


async def _ensure_project_foreign_keys(conn) -> None:
    """Repair SQLite child-table FKs after a projects table rebuild."""
    needs_repair = False
    for table in ("drafts", "published_posts"):
        result = await conn.execute(text(f"PRAGMA foreign_key_list({table})"))
        if any(row[2] != "projects" for row in result.fetchall() if row[3] == "project_id"):
            needs_repair = True
    if not needs_repair:
        return

    await conn.execute(text("PRAGMA foreign_keys=OFF"))
    await conn.execute(text("ALTER TABLE published_posts RENAME TO published_posts_legacy"))
    await conn.execute(
        text(
            "CREATE TABLE published_posts ("
            "id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, draft_id INTEGER NOT NULL, "
            "platform VARCHAR(32) NOT NULL, platform_post_id VARCHAR(255), url TEXT, "
            "success BOOLEAN NOT NULL, error TEXT, published_at DATETIME NOT NULL, "
            "FOREIGN KEY(project_id) REFERENCES projects(id), FOREIGN KEY(draft_id) REFERENCES drafts(id)"
            ")"
        )
    )
    await conn.execute(
        text(
            "INSERT INTO published_posts SELECT id, project_id, draft_id, platform, platform_post_id, "
            "url, success, error, published_at FROM published_posts_legacy"
        )
    )
    await conn.execute(text("DROP TABLE published_posts_legacy"))

    await conn.execute(text("ALTER TABLE drafts RENAME TO drafts_legacy"))
    await conn.execute(
        text(
            "CREATE TABLE drafts ("
            "id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, version INTEGER NOT NULL, "
            "title VARCHAR(255) NOT NULL, summary TEXT NOT NULL, instructions TEXT NOT NULL, "
            "potential_reward TEXT, risk_note TEXT, twitter_text TEXT, image_path VARCHAR(512), "
            "image_prompt TEXT, image_source VARCHAR(64), source_url TEXT, project_url TEXT, "
            "rework_feedback TEXT, created_at DATETIME NOT NULL, "
            "FOREIGN KEY(project_id) REFERENCES projects(id)"
            ")"
        )
    )
    await conn.execute(
        text(
            "INSERT INTO drafts SELECT id, project_id, version, title, summary, instructions, "
            "potential_reward, risk_note, twitter_text, image_path, image_prompt, image_source, "
            "source_url, project_url, rework_feedback, created_at FROM drafts_legacy"
        )
    )
    await conn.execute(text("DROP TABLE drafts_legacy"))
    await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_drafts_project_id ON drafts(project_id)"))
    await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_published_posts_project_id ON published_posts(project_id)"))
    await conn.execute(text("PRAGMA foreign_keys=ON"))


async def _ensure_published_foreign_keys(conn) -> None:
    """Finalize publication FKs after the drafts table is rebuilt."""
    result = await conn.execute(text("PRAGMA foreign_key_list(published_posts)"))
    foreign_tables = {row[2] for row in result.fetchall()}
    if {"projects", "drafts"}.issubset(foreign_tables):
        return

    await conn.execute(text("PRAGMA foreign_keys=OFF"))
    await conn.execute(text("ALTER TABLE published_posts RENAME TO published_posts_legacy"))
    await conn.execute(
        text(
            "CREATE TABLE published_posts ("
            "id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, draft_id INTEGER NOT NULL, "
            "platform VARCHAR(32) NOT NULL, platform_post_id VARCHAR(255), url TEXT, "
            "success BOOLEAN NOT NULL, error TEXT, published_at DATETIME NOT NULL, "
            "FOREIGN KEY(project_id) REFERENCES projects(id), FOREIGN KEY(draft_id) REFERENCES drafts(id)"
            ")"
        )
    )
    await conn.execute(
        text(
            "INSERT INTO published_posts SELECT id, project_id, draft_id, platform, platform_post_id, "
            "url, success, error, published_at FROM published_posts_legacy"
        )
    )
    await conn.execute(text("DROP TABLE published_posts_legacy"))
    await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_published_posts_project_id ON published_posts(project_id)"))
    await conn.execute(text("PRAGMA foreign_keys=ON"))


async def recover_stale_publications(max_age_minutes: int = 15) -> int:
    """Return projects left in APPROVED to review after a process crash."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
    recovered = 0
    async with get_session() as session:
        result = await session.execute(
            select(Project).where(Project.status == ProjectStatus.APPROVED)
        )
        for project in result.scalars():
            updated_at = project.updated_at
            if updated_at and updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            if not updated_at or updated_at < cutoff:
                project.status = ProjectStatus.PENDING_REVIEW
                recovered += 1
        if recovered:
            await session.commit()
    return recovered


async def _ensure_optional_columns(conn) -> None:
    """Small forward-compatible migrations for local SQLite databases."""
    await _ensure_columns(
        conn,
        "drafts",
        {
            "twitter_text": "TEXT",
            "image_prompt": "TEXT",
            "image_source": "VARCHAR(64)",
            "source_url": "TEXT",
            "project_url": "TEXT",
        },
    )
    await _ensure_columns(
        conn,
        "projects",
        {
            "filter_version": "INTEGER DEFAULT 1",
            "project_url": "TEXT",
            "workspace_id": "INTEGER DEFAULT 1",
        },
    )
    await _ensure_columns(conn, "users", {"password_hash": "VARCHAR(255)"})
    await _ensure_columns(conn, "review_requests", {"message_ids_json": "TEXT DEFAULT '[]'"})
    await _ensure_columns(conn, "web_settings", {"workspace_id": "INTEGER DEFAULT 1"})


async def _ensure_columns(conn, table: str, columns: dict[str, str]) -> None:
    result = await conn.execute(text(f"PRAGMA table_info({table})"))
    existing = {row[1] for row in result.fetchall()}
    for name, sql_type in columns.items():
        if name not in existing:
            await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"))


async def _ensure_default_workspace() -> None:
    """Give the existing owner-operated installation an explicit tenant boundary."""
    async with get_session() as session:
        user = await session.get(User, 1)
        if not user:
            user = User(id=1, telegram_user_id=settings.ADMIN_USER_ID or None)
            session.add(user)
            await session.flush()

        workspace = await session.get(Workspace, 1)
        if not workspace:
            workspace = Workspace(
                id=1,
                owner_id=user.id,
                name="Personal workspace",
                slug="personal",
                plan="free",
            )
            session.add(workspace)
            await session.flush()

        await session.execute(text("UPDATE projects SET workspace_id = 1 WHERE workspace_id IS NULL"))
        await session.execute(text("UPDATE web_settings SET workspace_id = 1 WHERE workspace_id IS NULL"))
        settings_row = await session.get(WebSettings, 1)
        if settings_row and settings_row.workspace_id != workspace.id:
            settings_row.workspace_id = workspace.id
        await session.commit()


@asynccontextmanager
async def get_session():
    async with SessionLocal() as session:
        yield session
