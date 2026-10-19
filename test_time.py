import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import dentist as d


class StartTimeTests(unittest.TestCase):
    def test_config_defaults_and_validation(self):
        base = dict(username='dummy', password='dummy', project='洁治(洗牙)',
                    interval_seconds=5, max_attempts=1, login_timeout_seconds=10)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'config.yaml'
            path.write_text(d.yaml.safe_dump(base))
            self.assertEqual(d.load_config(path)['start_time'], '09:00')
            for value in ['10:30', '00:00', '23:59']:
                path.write_text(d.yaml.safe_dump({**base, 'start_time': value}))
                self.assertEqual(d.load_config(path)['start_time'], value)
            for value in ['9:00', '24:00', '09:60', 900, None]:
                path.write_text(d.yaml.safe_dump({**base, 'start_time': value}))
                with self.subTest(value=value), self.assertRaises(d.Stop):
                    d.load_config(path)

    def run_booking(self, start_time=None, only_early=False):
        api = Mock()
        api.records.return_value = []
        def rows(endpoint, field, **params):
            return {
                'GetNeeds': [{'text': '洁治(洗牙)', 'value': '1'}],
                'GetDoctor': [{'text': 'doctor', 'value': '2'}],
                'GetYyrq': [
                    {'text': '10-26 ' + time, 'value': time}
                    for time in (['08:00'] if only_early else ['10:30', '08:00', '09:30', '09:00'])],
            }[endpoint]
        api.rows.side_effect = rows
        config = dict(project='洁治(洗牙)', max_attempts=1, interval_seconds=5)
        if start_time is not None:
            config['start_time'] = start_time
        with tempfile.TemporaryDirectory() as temp, patch.object(d, 'PRIVATE', Path(temp)), \
                patch.object(d, 'submit_once') as submit:
            if only_early:
                with self.assertRaisesRegex(d.Stop, '查询次数上限'):
                    d.book(api, config, SimpleNamespace(submit=True),
                           datetime(2026, 10, 19, 9, tzinfo=d.BEIJING))
                submit.assert_not_called()
                return
            d.book(api, config, SimpleNamespace(submit=True),
                   datetime(2026, 10, 19, 9, tzinfo=d.BEIJING))
            return submit.call_args.args[1]['value']

    def test_default_skips_eight_and_includes_nine(self):
        self.assertEqual(self.run_booking(), '09:00')

    def test_custom_time_selects_next_available_slot(self):
        self.assertEqual(self.run_booking('09:15'), '09:30')
        self.assertEqual(self.run_booking('10:30'), '10:30')

    def test_no_fallback_to_earlier_slot(self):
        self.run_booking('09:00', only_early=True)

    def test_unknown_slot_time_is_rejected(self):
        with self.assertRaises(d.Stop):
            d.slot_sort_key({'text': '10-26 上午', 'doctor': 'doctor'})


if __name__ == '__main__':
    unittest.main()
