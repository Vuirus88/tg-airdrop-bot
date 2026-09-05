import unittest

from publishing.state import archive_project_for_review, claim_project_for_publication


class PublicationStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_claim_allows_only_review_states(self):
        class FakeResult:
            rowcount = 1

        class FakeSession:
            async def execute(self, statement):
                self.statement = statement
                return FakeResult()

        session = FakeSession()
        self.assertTrue(await claim_project_for_publication(session, 10))
        self.assertIn("projects.status IN", str(session.statement))

    async def test_archive_allows_only_unpublished_review_states(self):
        class FakeResult:
            rowcount = 1

        class FakeSession:
            async def execute(self, statement):
                self.statement = statement
                return FakeResult()

        session = FakeSession()
        self.assertTrue(await archive_project_for_review(session, 10))
        self.assertIn("projects.status IN", str(session.statement))


if __name__ == "__main__":
    unittest.main()
