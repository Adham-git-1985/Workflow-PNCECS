import unittest

from services.meeting_service import normalize_agenda_order


class MeetingAgendaOrderTests(unittest.TestCase):
    def test_accepts_complete_reordering(self):
        self.assertEqual(
            normalize_agenda_order(["30", "10", "20"], [10, 20, 30]),
            [30, 10, 20],
        )

    def test_rejects_stale_or_invalid_submissions(self):
        invalid_submissions = [
            ["10", "10", "20"],
            ["10", "20"],
            ["10", "20", "99"],
            ["10", "not-an-id", "30"],
        ]
        for submitted in invalid_submissions:
            with self.subTest(submitted=submitted):
                with self.assertRaises(ValueError):
                    normalize_agenda_order(submitted, [10, 20, 30])


if __name__ == "__main__":
    unittest.main()
