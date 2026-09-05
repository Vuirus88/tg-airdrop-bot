"""One-time, guarded migration of the local SQLite database to Neon PostgreSQL."""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from aiogram import Bot
from aiogram.types import FSInputFile
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.ext.asyncio import create_async_engine

from config import settings
from db.models import Base, Draft

BATCH_SIZE = 500


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sqlite-path",
        default="airdrop_bot.db",
        help="Path to the backed-up SQLite database (default: airdrop_bot.db).",
    )
    parser.add_argument(
        "--upload-local-images",
        action="store_true",
        help="Upload local draft cards to Telegram and save durable Telegram file IDs.",
    )
    return parser.parse_args()


def sqlite_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.resolve().as_posix()}"


async def ensure_empty_target(target) -> None:
    result = await target.execute(
        text(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
        )
    )
    if result.scalar_one():
        raise RuntimeError(
            "Target Neon database is not empty. Refusing to overwrite it; create a new Neon "
            "project or clear it manually only after checking its contents."
        )


async def copy_tables(source, target) -> dict[str, int]:
    copied: dict[str, int] = {}
    async with target.begin() as target_conn:
        await target_conn.run_sync(Base.metadata.create_all)
        for table in Base.metadata.sorted_tables:
            result = await source.execute(select(table))
            rows = [dict(row) for row in result.mappings()]
            for offset in range(0, len(rows), BATCH_SIZE):
                await target_conn.execute(insert(table), rows[offset : offset + BATCH_SIZE])
            copied[table.name] = len(rows)
    return copied


async def reset_postgres_sequences(target) -> None:
    async with target.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if "id" not in table.c:
                continue
            await conn.execute(
                text(
                    f"SELECT setval(pg_get_serial_sequence('{table.name}', 'id'), "
                    f"COALESCE((SELECT MAX(id) FROM {table.name}), 1), "
                    f"(SELECT COUNT(*) > 0 FROM {table.name}))"
                )
            )


async def upload_local_images(target) -> int:
    if not settings.BOT_TOKEN or settings.ADMIN_USER_ID <= 0:
        raise RuntimeError("BOT_TOKEN and ADMIN_USER_ID are required with --upload-local-images.")
    bot = Bot(settings.BOT_TOKEN)
    uploaded = 0
    try:
        result = await target.execute(select(Draft.id, Draft.image_path))
        for draft_id, image_path in result:
            if not image_path or image_path.startswith(("http://", "https://")):
                continue
            image = Path(image_path)
            if not image.is_file():
                print(f"Skipping draft {draft_id}: local image not found: {image_path}")
                continue
            sent = await bot.send_photo(
                chat_id=settings.ADMIN_USER_ID,
                photo=FSInputFile(image),
                caption=f"Migrated bot card (draft {draft_id})",
            )
            await target.execute(
                update(Draft)
                .where(Draft.id == draft_id)
                .values(image_path=sent.photo[-1].file_id, image_source="telegram_file_id")
            )
            await target.commit()
            uploaded += 1
    finally:
        await bot.session.close()
    return uploaded


async def main() -> None:
    args = parse_args()
    source_path = Path(args.sqlite_path)
    if not source_path.is_file():
        raise FileNotFoundError(f"SQLite source was not found: {source_path.resolve()}")
    if not settings.DATABASE_URL.startswith("postgresql+asyncpg://"):
        raise RuntimeError("Set DATABASE_URL to the Neon PostgreSQL connection string before running.")

    source_engine = create_async_engine(sqlite_url(source_path))
    target_engine = create_async_engine(settings.DATABASE_URL)
    try:
        async with source_engine.connect() as source, target_engine.connect() as target:
            await ensure_empty_target(target)
            copied = await copy_tables(source, target_engine)
        await reset_postgres_sequences(target_engine)
        print("Migration completed:")
        for table, count in copied.items():
            print(f"  {table}: {count}")
        if args.upload_local_images:
            uploaded = await upload_local_images(target_engine)
            print(f"Uploaded local images to Telegram: {uploaded}")
    finally:
        await source_engine.dispose()
        await target_engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
