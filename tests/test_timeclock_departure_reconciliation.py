import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from portal.routes import (
    _attendance_departure_type_label,
    _attendance_event_code,
    _attendance_event_label,
    _attach_departure_sources_to_attendance_events,
    _departure_display_lines,
    _departure_records_match,
    _parse_timeclock_line,
    _reconciled_departure_records,
    _sort_attendance_events_for_display,
)


class TimeclockDepartureReconciliationTests(unittest.TestCase):
    def test_fixed_length_departure_codes_are_preserved(self):
        for code in ('C', 'D', 'E', 'F'):
            row = _parse_timeclock_line(f'2026-09-01000080439073700{code}1002')
            with self.subTest(code=code):
                self.assertIsNotNone(row)
                self.assertEqual(row['event_type'], code)

    def test_csv_departure_codes_are_preserved(self):
        row = _parse_timeclock_line('80439,2026-09-01,10:30:00,E,1002')
        self.assertEqual(row['event_type'], 'E')

    def test_departure_codes_have_clear_private_and_official_categories(self):
        self.assertEqual(_attendance_departure_type_label('C'), 'شخصية')
        self.assertEqual(_attendance_departure_type_label('D'), 'شخصية')
        self.assertEqual(_attendance_departure_type_label('E'), 'رسمية')
        self.assertEqual(_attendance_departure_type_label('F'), 'رسمية')
        self.assertEqual(_attendance_departure_type_label('I'), '')

    def test_attendance_labels_do_not_expose_clock_codes(self):
        self.assertEqual(_attendance_event_label('A'), 'دخول')
        self.assertEqual(_attendance_event_label('B'), 'خروج')
        self.assertEqual(_attendance_event_label('C'), 'مغادرة شخصية')
        self.assertEqual(_attendance_event_label('D'), 'عودة من مغادرة شخصية')
        self.assertEqual(_attendance_event_label('E'), 'مغادرة رسمية')
        self.assertEqual(_attendance_event_label('F'), 'عودة من مغادرة رسمية')

    def test_display_order_uses_time_then_daily_employee_number(self):
        at_eight_eighteen = datetime(2026, 9, 1, 8, 18)
        events = [
            SimpleNamespace(id=1, event_dt=datetime(2026, 9, 1, 8, 32), daily_employee_number=17),
            SimpleNamespace(id=2, event_dt=at_eight_eighteen, daily_employee_number=14),
            SimpleNamespace(id=3, event_dt=at_eight_eighteen, daily_employee_number=13),
            SimpleNamespace(id=4, event_dt=at_eight_eighteen, daily_employee_number=15),
            SimpleNamespace(id=5, event_dt=datetime(2026, 9, 1, 8, 14), daily_employee_number=12),
        ]

        ordered = _sort_attendance_events_for_display(events)

        self.assertEqual([event.daily_employee_number for event in ordered], [17, 13, 14, 15, 12])

    def test_legacy_out_row_recovers_departure_code_from_raw_fixed_record(self):
        legacy_event = SimpleNamespace(
            event_type='O',
            raw_line='2026-09-01000080439073700C1002',
        )
        self.assertEqual(_attendance_event_code(legacy_event), 'C')

    def test_matching_clock_and_system_records_do_not_double_count(self):
        clock_record = {
            'kind': 'PRIVATE',
            'from_dt': datetime(2026, 9, 1, 10, 0),
            'to_dt': datetime(2026, 9, 1, 11, 0),
        }
        matching_system_record = {
            'kind': 'PRIVATE',
            'from_dt': datetime(2026, 9, 1, 10, 20),
            'to_dt': datetime(2026, 9, 1, 11, 20),
        }
        different_kind = dict(matching_system_record, kind='OFFICIAL')

        self.assertTrue(_departure_records_match(clock_record, matching_system_record))
        self.assertFalse(_departure_records_match(clock_record, different_kind))

    def test_reconciliation_uses_clock_as_authoritative_source_when_both_match(self):
        clock = {
            'user_id': 7,
            'day': '2026-09-01',
            'kind': 'PRIVATE',
            'from_dt': datetime(2026, 9, 1, 12, 10),
            'to_dt': datetime(2026, 9, 1, 13, 30),
            'minutes': 80,
            'complete': True,
            'countable': True,
            'source': 'CLOCK',
        }
        matching_system = {
            'user_id': 7,
            'day': '2026-09-01',
            'kind': 'PRIVATE',
            'from_dt': datetime(2026, 9, 1, 12, 0),
            'to_dt': datetime(2026, 9, 1, 13, 0),
            'minutes': 60,
            'complete': True,
            'countable': True,
            'source': 'SYSTEM',
            'permission_id': 3,
            'permission_type_id': 2,
        }
        system_only = dict(
            matching_system,
            day='2026-09-02',
            from_dt=datetime(2026, 9, 2, 12, 0),
            to_dt=datetime(2026, 9, 2, 12, 30),
            minutes=30,
        )

        with patch('portal.routes._clock_departure_records', return_value=[clock]), patch(
            'portal.routes._system_departure_records', return_value=[matching_system, system_only]
        ):
            records = _reconciled_departure_records([7], '2026-09-01', '2026-09-02')

        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]['source'], 'CLOCK_SYSTEM')
        self.assertEqual(records[0]['from_dt'], datetime(2026, 9, 1, 12, 10))
        self.assertEqual(records[0]['to_dt'], datetime(2026, 9, 1, 13, 30))
        self.assertEqual(records[0]['counted_minutes'], 80)
        self.assertEqual(records[0]['system_from_dt'], datetime(2026, 9, 1, 12, 0))
        self.assertEqual(records[0]['system_to_dt'], datetime(2026, 9, 1, 13, 0))
        self.assertEqual(records[1]['source'], 'SYSTEM')
        self.assertEqual(records[1]['counted_minutes'], 30)

    def test_report_cell_shows_clock_and_masar_times_separately(self):
        record = {
            'kind': 'PRIVATE',
            'label': 'مغادرة شخصية',
            'source': 'CLOCK_SYSTEM',
            'clock_from_dt': datetime(2026, 9, 1, 10, 0),
            'clock_to_dt': datetime(2026, 9, 1, 11, 0),
            'system_from_dt': datetime(2026, 9, 1, 10, 10),
            'system_to_dt': datetime(2026, 9, 1, 11, 10),
        }

        lines = _departure_display_lines([record])

        self.assertEqual(len(lines), 2)
        self.assertEqual([line['source_label'] for line in lines], ['ساعة الدوام', 'نظام مسار'])
        self.assertIn('10:00', lines[0]['time_range'])
        self.assertIn('10:10', lines[1]['time_range'])

    def test_system_only_departure_creates_a_report_row(self):
        record = {
            'user_id': 7,
            'day': '2026-09-01',
            'kind': 'PRIVATE',
            'label': 'مغادرة شخصية',
            'source': 'SYSTEM',
            'system_from_dt': datetime(2026, 9, 1, 12, 0),
            'system_to_dt': datetime(2026, 9, 1, 12, 30),
        }

        rows = _attach_departure_sources_to_attendance_events([], [record])

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].display_event_label, 'مغادرة من نظام مسار')
        self.assertEqual(rows[0].departure_display_lines[0]['source_label'], 'نظام مسار')


if __name__ == '__main__':
    unittest.main()
