"""Background source scanning with detailed outcome statistics."""
import asyncio
import json
import logging
from datetime import datetime

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.events import EVENT_JOB_ERROR

from config import settings
from ingestion.sources import collect_all_signals_detailed
from services.pipeline import process_raw_signal
from services.job_queue import claim_next_job, complete_job, enqueue_job, fail_job
from services.maintenance import cleanup_old_data
from db.database import get_session

logger = logging.getLogger(__name__)


class SourceScanScheduler:
    def __init__(self) -> None:
        self.scheduler = AsyncIOScheduler()
        self.bot: Bot | None = None

    def configure(self, bot: Bot) -> None:
        self.bot = bot

    def start(self) -> None:
        if not self.bot:
            raise RuntimeError("SourceScanScheduler.configure(bot) must be called before start().")
        self.scheduler.add_listener(self._on_job_error, EVENT_JOB_ERROR)
        self.scheduler.add_job(
            self.scan_once,
            trigger=IntervalTrigger(minutes=settings.SOURCE_SCAN_INTERVAL_MINUTES),
            id="scan_sources",
            name="Scan free crypto opportunity sources",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.add_job(
            self.process_pending_jobs,
            trigger=IntervalTrigger(seconds=settings.JOB_WORKER_INTERVAL_SECONDS),
            id="process_background_jobs",
            name="Process queued opportunity jobs",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.add_job(
            cleanup_old_data,
            trigger=IntervalTrigger(hours=settings.CLEANUP_INTERVAL_HOURS),
            id="cleanup_technical_data",
            name="Clean technical history",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        if settings.RUN_SCAN_ON_START:
            self.scheduler.add_job(
                self.scan_once,
                trigger=DateTrigger(run_date=datetime.now()),
                id="startup_scan",
                name="Initial source scan",
                replace_existing=True,
            )
        self.scheduler.start()
        logger.info("Source scanner started: every %s minutes", settings.SOURCE_SCAN_INTERVAL_MINUTES)

    @staticmethod
    def _on_job_error(event) -> None:
        logger.error(
            "Scheduled job failed: id=%s exception=%s",
            event.job_id,
            event.exception,
            exc_info=event.exception,
        )

    async def stop(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    async def scan_once(self) -> dict[str, int]:
        summary = {
            "collected": 0,
            "sent_for_review": 0,
            "filtered": 0,
            "duplicates": 0,
            "groq": 0,
            "fallback": 0,
            "errors": 0,
            "queued": 0,
            "source_reports": [],
        }
        if not self.bot:
            return summary

        try:
            collection = await asyncio.wait_for(
                collect_all_signals_detailed(),
                timeout=settings.SOURCE_COLLECTION_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            summary["errors"] += 1
            logger.error(
                "Source collection timed out after %s seconds",
                settings.SOURCE_COLLECTION_TIMEOUT_SECONDS,
            )
            return summary
        except Exception:
            summary["errors"] += 1
            logger.exception("Source collection failed")
            return summary
        signals = collection.signals
        summary["source_reports"] = [
            {
                "source": report.source,
                "fetched": report.fetched,
                "entries": report.entries,
                "candidates": report.candidates,
                "rejected": report.rejected,
                "reason_counts": report.reason_counts or {},
                "error": report.error,
            }
            for report in collection.reports
        ]
        summary["collected"] = len(signals)
        async with get_session() as session:
            for signal in signals:
                try:
                    await enqueue_job(
                        session,
                        "process_signal",
                        {
                            "name": signal.name,
                            "raw_text": signal.raw_text,
                            "source": signal.source,
                            "source_url": signal.source_url,
                        },
                        max_attempts=settings.JOB_MAX_ATTEMPTS,
                    )
                    summary["queued"] += 1
                except Exception:
                    summary["errors"] += 1
                    logger.exception("Could not queue source signal (%s)", signal.name)
            await session.commit()

        logger.info("Source scan summary: %s", summary)
        return summary

    async def process_pending_jobs(self) -> bool:
        if not self.bot:
            return False
        async with get_session() as session:
            job = await claim_next_job(session)
        if not job:
            return False
        try:
            payload = json.loads(job.payload_json)
            if job.job_type != "process_signal":
                raise RuntimeError(f"Unknown background job type: {job.job_type}")
            result = await asyncio.wait_for(
                process_raw_signal(bot=self.bot, **payload),
                timeout=settings.SIGNAL_PROCESS_TIMEOUT_SECONDS,
            )
            async with get_session() as session:
                await complete_job(session, job.id)
            logger.info(
                "Background job completed: id=%s name=%s outcome=%s provider=%s",
                job.id,
                payload.get("name"),
                result.outcome,
                result.provider,
            )
        except Exception as exc:
            async with get_session() as session:
                requeued = await fail_job(session, job.id, str(exc))
            logger.exception(
                "Background job failed: id=%s name=%s requeued=%s",
                job.id,
                payload.get("name", "unknown") if "payload" in locals() else "unknown",
                requeued,
            )
        return True


source_scanner = SourceScanScheduler()
