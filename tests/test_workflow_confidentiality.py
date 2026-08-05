import unittest
from types import SimpleNamespace
from unittest.mock import patch

from services.workflow_confidentiality import (
    can_user_pass_confidential_workflow_gate,
    is_confidential_workflow,
)


class _User(SimpleNamespace):
    def has_perm(self, permission):
        return permission in getattr(self, "permission_keys", set())


class WorkflowConfidentialityTests(unittest.TestCase):
    def setUp(self):
        self.source = SimpleNamespace(
            confidentiality="SECRET",
            created_by_id=1,
            current_assignee_id=2,
        )
        self.request = SimpleNamespace(
            confidentiality="SECRET",
            requester_id=1,
            source_corr_kind="OUT",
            source_corr_id=44,
        )

    def test_normal_workflow_does_not_activate_confidential_gate(self):
        request = SimpleNamespace(confidentiality="NORMAL")
        self.assertFalse(is_confidential_workflow(request))
        self.assertTrue(can_user_pass_confidential_workflow_gate(_User(id=99), request))

    @patch("services.workflow_confidentiality._authorized_ids_for_item", return_value={3})
    @patch("services.workflow_confidentiality._source_correspondence")
    def test_secret_workflow_allows_only_source_acl_users(
        self,
        source_correspondence,
        _authorized_ids,
    ):
        source_correspondence.return_value = self.source

        for user_id in (1, 2, 3):
            with self.subTest(user_id=user_id):
                self.assertTrue(can_user_pass_confidential_workflow_gate(
                    _User(id=user_id, permission_keys=set()),
                    self.request,
                ))

        self.assertFalse(can_user_pass_confidential_workflow_gate(
            _User(id=4, permission_keys=set()),
            self.request,
        ))

    @patch("services.workflow_confidentiality._authorized_ids_for_item", return_value=set())
    @patch("services.workflow_confidentiality._source_correspondence")
    def test_dedicated_confidential_permission_remains_an_explicit_override(
        self,
        source_correspondence,
        _authorized_ids,
    ):
        source_correspondence.return_value = self.source
        user = _User(id=9, permission_keys={"CORR_CONFIDENTIAL_READ"})
        self.assertTrue(can_user_pass_confidential_workflow_gate(user, self.request))

    @patch("services.workflow_confidentiality._source_correspondence", return_value=None)
    def test_missing_source_fails_closed_for_ordinary_participant(self, _source):
        self.assertFalse(can_user_pass_confidential_workflow_gate(
            _User(id=8, permission_keys=set()),
            self.request,
        ))


if __name__ == "__main__":
    unittest.main()
