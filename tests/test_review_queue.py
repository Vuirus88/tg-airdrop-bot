import unittest

from services.review_queue import adjacent_project_id, queue_position


class QueueLogicTests(unittest.TestCase):
    def test_position_and_total_are_one_based(self):
        class Item:
            def __init__(self, item_id):
                self.id = item_id

        items = [Item(10), Item(20), Item(30)]
        self.assertEqual(queue_position(items, 20), (2, 3))

    def test_navigation_stops_at_queue_edges(self):
        class Item:
            def __init__(self, item_id):
                self.id = item_id

        items = [Item(10), Item(20), Item(30)]
        self.assertEqual(adjacent_project_id(items, 20, -1), 10)
        self.assertEqual(adjacent_project_id(items, 20, 1), 30)
        self.assertIsNone(adjacent_project_id(items, 10, -1))
        self.assertIsNone(adjacent_project_id(items, 30, 1))


if __name__ == "__main__":
    unittest.main()
