import json
from argparse import Namespace
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from dentist import BEIJING, Stop, load_config, release_time, save_json, project_id, parse_slot_date, collect_slots, check_existing, submit_once, book


class SchedulingTests(unittest.TestCase):
    def test_release_before_and_after_nine(self):
        morning = datetime(2026, 10, 15, 8, 59, tzinfo=BEIJING)
        self.assertEqual(release_time(morning).isoformat(), '2026-10-15T09:00:00+08:00')
        self.assertEqual(release_time(morning.replace(hour=9)).date().isoformat(), '2026-10-16')

    def test_explicit_release_date(self):
        self.assertEqual(release_time(datetime.now(BEIJING), '2026-10-15').isoformat(), '2026-10-15T09:00:00+08:00')

    def test_yaml_credentials_preserve_strings_and_spaces(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'config.yaml'
            path.write_text('username: "001234"\npassword: " pass #word: "\n'
                            'project: "洁治(洗牙)"\ninterval_seconds: 5\n'
                            'max_attempts: 120\nlogin_timeout_seconds: 180\n')
            config = load_config(path)
            self.assertEqual(config['username'], '001234')
            self.assertEqual(config['password'], ' pass #word: ')
            path.write_text('username: 1234\npassword: "secret"\nproject: "洁治(洗牙)"\n')
            with self.assertRaises(Stop):
                load_config(path)

    def test_state_saved_privately(self):
        with TemporaryDirectory() as folder:
            p = Path(folder) / 'state.json'
            save_json(p, {'status': 'pending'})
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)


class FakeApi:
    def __init__(self, fail=False):
        self.uploads = 0
        self.fail = fail

    def records(self):
        return []

    def rows(self, endpoint, field, **params):
        if endpoint == 'GetNeeds':
            return [{'text': '洁治(洗牙)', 'value': '19', 'text2': '别的项目', 'value2': '22'}]
        if endpoint == 'GetDoctor':
            return [{'text': '测试医生', 'value': 'D1'}]
        return [{'text': '10-22 14:00\t星期四', 'value': 'S2'},
                {'text': '2026-10-21 08:00', 'value': 'WRONG-DAY'},
                {'text': '2026-10-22 08:00', 'value': 'S1'}]

    def call(self, endpoint, **params):
        if endpoint == 'Upload':
            self.uploads += 1
            if self.fail:
                raise TimeoutError('response lost')
        return {}


class BookingTests(unittest.TestCase):
    def test_project_exact_match_in_both_columns(self):
        self.assertEqual(project_id([{'text2': '洁治(洗牙)', 'value2': '19'}], '洁治(洗牙)'), '19')
        with self.assertRaises(Stop):
            project_id([{'text': '拔牙', 'value': '26'}], '洁治(洗牙)')

    def test_dates_and_unknown_format(self):
        for text in ['2026-10-22 08:00', '2026年10月22日', '20261022']:
            self.assertEqual(str(parse_slot_date(text)), '2026-10-22')
        with self.assertRaises(Stop):
            parse_slot_date('周四上午')

    def test_real_slot_format_and_new_year(self):
        self.assertEqual(str(parse_slot_date('10-22 15:30\t星期四', parse_slot_date('2026-10-22'))), '2026-10-22')
        self.assertEqual(str(parse_slot_date('01-01 08:00', parse_slot_date('2026-12-31'))), '2027-01-01')

    def test_only_target_day_and_earliest_time(self):
        slots = collect_slots(FakeApi(), '19', parse_slot_date('2026-10-22'))
        self.assertEqual([s['value'] for s in slots], ['S1', 'S2'])

    def test_weekly_limit_excludes_cancelled(self):
        target = parse_slot_date('2026-10-22')
        check_existing([{'YYRQ': '2026-10-22', 'Status': '-1'}], target)
        with self.assertRaises(Stop):
            check_existing([{'YYRQ': '2026-10-20', 'Status': '0'}], target)

    def test_unknown_result_never_retried(self):
        api = FakeApi(fail=True)
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'booking.json'
            slot = {'value': 'S1', 'text': '2026-10-22 08:00', 'doctor': '测试医生'}
            for _ in range(2):
                with self.assertRaises(Stop):
                    submit_once(api, slot, parse_slot_date('2026-10-22'), path)
            self.assertEqual(api.uploads, 1)
            self.assertEqual(json.loads(path.read_text())['status'], 'unknown')

    def test_preview_never_submits(self):
        api = FakeApi()
        with TemporaryDirectory() as folder, patch('dentist.PRIVATE', Path(folder)):
            book(api, {'project': '洁治(洗牙)', 'max_attempts': 1}, Namespace(submit=False),
                 datetime(2026, 10, 15, 9, tzinfo=BEIJING))
        self.assertEqual(api.uploads, 0)

    def test_accepted_result_blocks_later_submission(self):
        api = FakeApi()
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'booking.json'
            slot = {'value': 'S1', 'text': '2026-10-22 08:00', 'doctor': '测试医生'}
            result = submit_once(api, slot, parse_slot_date('2026-10-22'), path)
            self.assertEqual(result['status'], 'accepted')
            with self.assertRaises(Stop):
                submit_once(api, slot, parse_slot_date('2026-10-22'), path)
        self.assertEqual(api.uploads, 1)


if __name__ == '__main__':
    unittest.main()
