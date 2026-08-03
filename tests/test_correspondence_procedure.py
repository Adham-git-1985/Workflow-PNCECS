import unittest
from datetime import date

from services.correspondence_procedure import due_state, next_status, queue_matches


class CorrespondenceProcedureTests(unittest.TestCase):
    def _matches(self, queue, **overrides):
        values = {
            "kind": "IN",
            "status": "RECEIVED",
            "mail_scope": "EXTERNAL",
            "priority": "NORMAL",
            "confidentiality": "NORMAL",
            "assigned_to_user_id": None,
            "current_user_id": 7,
            "forwarded_by_user_ids": set(),
        }
        values.update(overrides)
        return queue_matches(queue=queue, **values)

    def test_actions_move_to_expected_statuses(self):
        self.assertEqual(next_status("RECEIVED", "OPEN"), "IN_PROGRESS")
        self.assertEqual(next_status("IN_PROGRESS", "FORWARD"), "FORWARDED")
        self.assertEqual(next_status("FORWARDED", "SUBMIT_APPROVAL"), "WAITING_APPROVAL")
        self.assertEqual(next_status("WAITING_APPROVAL", "APPROVE"), "APPROVED")
        self.assertEqual(next_status("APPROVED", "FINAL_REPLY"), "COMPLETED")
        self.assertEqual(next_status("APPROVED", "ARCHIVE"), "ARCHIVED")

    def test_internal_note_keeps_current_status(self):
        self.assertEqual(next_status("FORWARDED", "INTERNAL_NOTE"), "FORWARDED")

    def test_deadline_states(self):
        today = date(2026, 8, 3)
        self.assertEqual(due_state("2026-08-02", today), "OVERDUE")
        self.assertEqual(due_state("2026-08-03", today), "DUE_TODAY")
        self.assertEqual(due_state("2026-08-05", today), "DUE_SOON")
        self.assertEqual(due_state("2026-08-20", today), "SCHEDULED")
        self.assertIsNone(due_state("", today))

    def test_guide_queues(self):
        self.assertTrue(self._matches("incoming"))
        self.assertTrue(self._matches("outgoing", kind="OUT"))
        self.assertTrue(self._matches("internal", mail_scope="INTERNAL"))
        self.assertTrue(self._matches("to_me", assigned_to_user_id=7))
        self.assertTrue(self._matches("from_me", forwarded_by_user_ids={7}))
        self.assertTrue(self._matches("waiting_action", status="WAITING_INFO"))
        self.assertTrue(self._matches("waiting_approval", status="WAITING_APPROVAL"))
        self.assertTrue(self._matches("completed", status="CLOSED"))
        self.assertTrue(self._matches("archived", status="ARCHIVED"))
        self.assertTrue(self._matches("returned", status="RETURNED"))
        self.assertTrue(self._matches("high_priority", priority="URGENT"))
        self.assertTrue(self._matches("confidential", confidentiality="SECRET"))


if __name__ == "__main__":
    unittest.main()
