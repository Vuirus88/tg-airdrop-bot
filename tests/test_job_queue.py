import unittest
from datetime import datetime, timezone

from db.models import BackgroundJob
from services.job_queue import complete_job, fail_job


class JobQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_job_is_requeued_until_attempt_limit(self):
        job = BackgroundJob(id=7, attempts=1, max_attempts=3, status="running")

        class FakeSession:
            async def get(self, model, job_id):
                return job

            async def commit(self):
                return None

        requeued = await fail_job(FakeSession(), job.id, "temporary provider error")
        self.assertTrue(requeued)
        self.assertEqual(job.status, "queued")
        self.assertEqual(job.last_error, "temporary provider error")
        self.assertIsNotNone(job.available_at)

    async def test_failed_job_is_marked_failed_at_attempt_limit(self):
        job = BackgroundJob(id=8, attempts=3, max_attempts=3, status="running")

        class FakeSession:
            async def get(self, model, job_id):
                return job

            async def commit(self):
                return None

        requeued = await fail_job(FakeSession(), job.id, "permanent error")
        self.assertFalse(requeued)
        self.assertEqual(job.status, "failed")

    async def test_complete_job_marks_completion(self):
        job = BackgroundJob(id=9, status="running")

        class FakeSession:
            async def get(self, model, job_id):
                return job

            async def commit(self):
                return None

        await complete_job(FakeSession(), job.id)
        self.assertEqual(job.status, "completed")
        self.assertIsInstance(job.completed_at, datetime)
        self.assertEqual(job.completed_at.tzinfo, timezone.utc)


if __name__ == "__main__":
    unittest.main()
