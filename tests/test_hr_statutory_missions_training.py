from datetime import date
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from html.parser import HTMLParser
from pathlib import Path

from portal.routes import _period_within_month_limit
from portal.routes import _training_can_manage, _manager_ids_for_employee


class WorkflowReviewRegressionTests(unittest.TestCase):
    def test_read_permissions_do_not_grant_training_management(self):
        for permission in ('HR_REPORTS_VIEW', 'HR_REQUESTS_VIEW_ALL'):
            with self.subTest(permission=permission), patch('portal.routes.current_user', SimpleNamespace(has_perm=lambda key: key == permission)):
                self.assertFalse(_training_can_manage())
        with patch('portal.routes.current_user', SimpleNamespace(has_perm=lambda key: key == 'HR_EMPLOYEE_MANAGE')):
            self.assertTrue(_training_can_manage())

    def test_manager_resolution_excludes_requester(self):
        with patch('portal.routes.resolve_responsible_managers', return_value=[SimpleNamespace(id=7), SimpleNamespace(id=8)]):
            self.assertEqual(_manager_ids_for_employee(7), {8})

    def test_rejection_buttons_bypass_approval_only_browser_requirements(self):
        class Buttons(HTMLParser):
            def __init__(self):
                super().__init__()
                self.actions = {}

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == 'button' and attrs.get('name') == 'action':
                    self.actions[attrs.get('value')] = attrs

        for filename, actions in (
            ('training/requests.html', ('MANAGER_REJECT', 'HR_REJECT')),
            ('mission_requests_queue.html', ('HR_REJECT',)),
        ):
            parser = Buttons()
            parser.feed((Path(__file__).resolve().parents[1] / 'templates/portal/hr' / filename).read_text(encoding='utf-8'))
            for action in actions:
                self.assertIn('formnovalidate', parser.actions[action])


class StatutoryMissionTrainingDateTests(unittest.TestCase):
    def test_official_mission_period_is_limited_to_one_inclusive_calendar_month(self):
        self.assertTrue(_period_within_month_limit(date(2026, 1, 1), date(2026, 1, 31), 1))
        self.assertFalse(_period_within_month_limit(date(2026, 1, 1), date(2026, 2, 1), 1))

    def test_training_period_is_limited_to_eight_months_and_handles_leap_year(self):
        self.assertTrue(_period_within_month_limit(date(2024, 2, 1), date(2024, 9, 30), 8))
        self.assertFalse(_period_within_month_limit(date(2024, 2, 1), date(2024, 10, 1), 8))

    def test_month_end_clamps_to_shorter_month(self):
        self.assertTrue(_period_within_month_limit(date(2024, 1, 31), date(2024, 2, 28), 1))
        self.assertFalse(_period_within_month_limit(date(2024, 1, 31), date(2024, 2, 29), 1))


if __name__ == "__main__":
    unittest.main()
