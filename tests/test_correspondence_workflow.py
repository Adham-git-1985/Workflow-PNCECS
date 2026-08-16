import unittest
from types import SimpleNamespace
from unittest.mock import patch

from services.correspondence_workflow import (
    _mapped_status,
    sync_correspondence_from_workflow,
)


class CorrespondenceWorkflowTests(unittest.TestCase):
    def test_workflow_statuses_map_to_procedural_correspondence_statuses(self):
        self.assertEqual(_mapped_status("IN_PROGRESS", "RECEIVED"), "IN_PROGRESS")
        self.assertEqual(_mapped_status("IN_PROGRESS", "WAITING_APPROVAL"), "WAITING_APPROVAL")
        self.assertEqual(_mapped_status("APPROVED", "IN_PROGRESS"), "APPROVED")
        self.assertEqual(_mapped_status("REJECTED", "WAITING_APPROVAL"), "RETURNED")
        self.assertEqual(_mapped_status("DRAFT", "REGISTERED"), "REGISTERED")

    @patch("services.correspondence_workflow._append_movement")
    @patch("services.correspondence_workflow._workflow_target")
    @patch("services.correspondence_workflow.source_correspondence")
    def test_active_workflow_updates_status_and_current_target(
        self,
        source_correspondence,
        workflow_target,
        append_movement,
    ):
        item = SimpleNamespace(
            id=6,
            status="RECEIVED",
            current_target_kind=None,
            current_target_id=None,
            current_target_label=None,
            current_assignee_id=None,
        )
        request = SimpleNamespace(id=11, status="IN_PROGRESS")
        source_correspondence.return_value = ("IN", item)
        workflow_target.return_value = {
            "kind": "USER",
            "id": 24,
            "label": "مدير الإدارة",
            "user_id": 24,
        }

        sync_correspondence_from_workflow(request, actor_user_id=7)

        self.assertEqual(item.status, "IN_PROGRESS")
        self.assertEqual(item.current_target_kind, "USER")
        self.assertEqual(item.current_assignee_id, 24)
        append_movement.assert_called_once()
        self.assertEqual(append_movement.call_args.kwargs["action"], "WORKFLOW_SYNC")

    @patch("services.correspondence_workflow.db.session.get")
    @patch("services.correspondence_workflow._append_movement")
    @patch("services.correspondence_workflow._workflow_target")
    @patch("services.correspondence_workflow.source_correspondence")
    def test_approved_official_reply_completes_source_inbound(
        self,
        source_correspondence,
        workflow_target,
        append_movement,
        session_get,
    ):
        outbound = SimpleNamespace(
            id=31,
            ref_no="OUT-31",
            status="IN_PROGRESS",
            source_inbound_id=15,
            current_target_kind="USER",
            current_target_id=9,
            current_target_label="المراجع",
            current_assignee_id=9,
        )
        inbound = SimpleNamespace(id=15, status="WAITING_APPROVAL")
        request = SimpleNamespace(id=12, status="APPROVED")
        source_correspondence.return_value = ("OUT", outbound)
        workflow_target.return_value = {
            "kind": None,
            "id": None,
            "label": None,
            "user_id": None,
        }
        session_get.return_value = inbound

        sync_correspondence_from_workflow(request, actor_user_id=7)

        self.assertEqual(outbound.status, "APPROVED")
        self.assertEqual(inbound.status, "COMPLETED")
        self.assertEqual(append_movement.call_count, 2)
        self.assertEqual(append_movement.call_args_list[0].kwargs["action"], "WORKFLOW_APPROVED")
        self.assertEqual(append_movement.call_args_list[1].kwargs["action"], "FINAL_REPLY")
        self.assertEqual(
            append_movement.call_args_list[1].kwargs["target"]["label"],
            "صادر رقم OUT-31",
        )


if __name__ == "__main__":
    unittest.main()
