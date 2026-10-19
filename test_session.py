"""会话恢复与提交边界回归测试；不访问真实预约系统。"""
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import dentist as d


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.page = Mock()
        self.context = Mock()
        self.config = dict(username='user', password='secret', login_timeout_seconds=180,
                           project='洁治(洗牙)', max_attempts=2, interval_seconds=5)
        self.api = d.SessionApi(self.page, self.context, self.config)

    def test_stale_local_token_requires_server_validation_and_reauthentication(self):
        self.page.evaluate.side_effect = [
            {'Head': {'ErrorNo': 1, 'ErrorStr': '登录会话已过期'}},
            {'Head': {'ErrorNo': 0}, 'Data': {'Needs': []}},
        ]
        with patch.object(self.api, 'authenticate') as auth, patch.object(d, 'save_session') as save:
            self.assertEqual(self.api.rows('GetNeeds', 'Needs'), [])
        auth.assert_called_once_with(force=True)
        save.assert_called_once_with(self.context)

    def test_force_auth_discards_old_token_preserves_sso_and_is_headless(self):
        with patch.object(d, 'enter_dentist') as enter:
            self.api.authenticate(force=True)
        self.assertIn("removeItem('token')", self.page.evaluate.call_args.args[0])
        self.context.clear_cookies.assert_not_called()
        enter.assert_called_once_with(self.page, self.context, ('user', 'secret'), False, 180)

    def test_open_validates_even_when_page_looks_logged_in(self):
        with patch.object(self.api, 'authenticate') as auth, patch.object(self.api, 'validate') as validate:
            self.api.open()
        auth.assert_called_once_with()
        validate.assert_called_once_with()

    def test_transport_error_after_successful_auth_does_not_repeat_auth(self):
        with patch.object(d.Api, 'call', side_effect=[d.SessionExpired('expired'), d.TransportError('timeout'), {}]), \
                patch.object(self.api, 'authenticate') as auth, patch.object(d.time, 'sleep'), \
                patch.object(d, 'save_session'):
            self.assertEqual(self.api.call('GetNeeds'), {})
        auth.assert_called_once_with(force=True)

    def test_unknown_response_is_not_proof_of_expiry(self):
        self.page.evaluate.return_value = {'unexpected': 'data'}
        with patch.object(self.api, 'authenticate') as auth, patch.object(d.time, 'sleep'):
            with self.assertRaises(d.TransportError):
                self.api.call('GetNeeds')
        auth.assert_not_called()
        self.assertEqual(self.page.evaluate.call_count, 3)

    def test_read_timeout_retries_without_reauth(self):
        with patch.object(d.Api, 'call', side_effect=[d.TransportError('timeout'), {'Doctor': []}]) as call, \
                patch.object(self.api, 'authenticate') as auth, patch.object(d.time, 'sleep') as sleep:
            self.assertEqual(self.api.rows('GetDoctor', 'Doctor'), [])
        self.assertEqual(call.call_count, 2)
        auth.assert_not_called()
        sleep.assert_called_once_with(5)

    def test_reauth_navigation_failure_can_recover(self):
        with patch.object(d.Api, 'call', side_effect=[d.SessionExpired('expired'), {}]), \
                patch.object(self.api, 'authenticate', side_effect=[d.TransportError('timeout'), None]) as auth, \
                patch.object(d.time, 'sleep'), patch.object(d, 'save_session'):
            self.assertEqual(self.api.call('GetNeeds'), {})
        self.assertEqual(auth.call_count, 2)

    def test_retries_are_bounded(self):
        with patch.object(d.Api, 'call', side_effect=d.TransportError('timeout')) as call, \
                patch.object(d.time, 'sleep'):
            with self.assertRaises(d.TransportError):
                self.api.call('GetDoctor')
        self.assertEqual(call.call_count, 3)

    def test_repeated_expiry_is_bounded(self):
        with patch.object(d.Api, 'call', side_effect=d.SessionExpired('expired')) as call, \
                patch.object(self.api, 'authenticate') as auth:
            with self.assertRaisesRegex(d.Stop, '仍无效'):
                self.api.call('GetNeeds')
        self.assertEqual(call.call_count, 3)
        self.assertEqual(auth.call_count, 2)

    def test_business_rejection_does_not_trigger_reauthentication(self):
        with patch.object(d.Api, 'call', side_effect=d.ApiError('无号')) as call, \
                patch.object(self.api, 'authenticate') as auth:
            with self.assertRaises(d.ApiError):
                self.api.call('GetYyrq')
        call.assert_called_once()
        auth.assert_not_called()

    def test_otp_requirement_stops_without_looping(self):
        with patch.object(d.Api, 'call', side_effect=d.SessionExpired('expired')) as call, \
                patch.object(self.api, 'authenticate', side_effect=d.Stop('需要验证码')) as auth:
            with self.assertRaisesRegex(d.Stop, '验证码'):
                self.api.call('GetNeeds')
        call.assert_called_once()
        auth.assert_called_once()

    def test_validate_checks_business_and_records_then_saves(self):
        with patch.object(self.api, 'rows') as rows, patch.object(self.api, 'records') as records, \
                patch.object(d, 'save_session') as save:
            self.api.validate()
        rows.assert_called_once_with('GetNeeds', 'Needs')
        records.assert_called_once()
        save.assert_called_once_with(self.context)

    def test_prepare_checks_repeatedly_and_at_release(self):
        target = datetime(2026, 10, 19, 9, tzinfo=d.BEIJING)
        clock = [target - timedelta(seconds=61)]
        checks = []
        def sleep(seconds):
            clock[0] += timedelta(seconds=seconds)
        with patch.object(d, 'now', side_effect=lambda: clock[0]), \
                patch.object(d.time, 'sleep', side_effect=sleep):
            d.wait_until(target, lambda: checks.append(clock[0]))
        self.assertEqual(len(checks), 4)
        self.assertEqual(checks[-1], target)
        self.assertTrue(all((b-a).total_seconds() <= 30 for a,b in zip(checks, checks[1:])))

    def test_expiry_during_slot_query_recovers_without_changing_target(self):
        release = datetime(2026, 10, 19, 9, tzinfo=d.BEIJING)
        def response(endpoint, **params):
            if endpoint == 'GetApptList': return {'List': []}
            if endpoint == 'GetNeeds': return {'Needs': [{'text': '洁治(洗牙)', 'value': '1'}]}
            if endpoint == 'GetDoctor': return {'Doctor': [{'text': 'doctor', 'value': '2'}]}
            if endpoint == 'GetYyrq':
                response.calls += 1
                if response.calls == 1: raise d.SessionExpired('expired')
                return {'YYRQ': [{'text': '2026-10-26 09:00', 'value': 'slot'}]}
            self.fail('Unexpected endpoint: ' + endpoint)
        response.calls = 0
        with tempfile.TemporaryDirectory() as temp, patch.object(d, 'PRIVATE', Path(temp)), \
                patch.object(d.Api, 'call', side_effect=response), \
                patch.object(self.api, 'authenticate') as auth, patch.object(d, 'save_session'):
            d.book(self.api, self.config, SimpleNamespace(submit=False), release)
        self.assertEqual(response.calls, 2)
        auth.assert_called_once_with(force=True)

    def test_upload_timeout_marks_unknown_and_never_retries(self):
        target = datetime(2026, 10, 26).date()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'state.json'
            with patch.object(self.api, 'records', return_value=[]), \
                    patch.object(d.Api, 'call', side_effect=d.TransportError('timeout')) as upload, \
                    patch.object(self.api, 'authenticate') as auth:
                with self.assertRaisesRegex(d.Stop, '提交结果不明确'):
                    d.submit_once(self.api, {'value': 'slot'}, target, path)
                self.assertIn('unknown', path.read_text())
                with self.assertRaises(d.Stop):
                    d.submit_once(self.api, {'value': 'slot'}, target, path)
            upload.assert_called_once_with('Upload', yyxh='slot')
            auth.assert_not_called()

    def test_upload_expired_response_is_not_retried(self):
        with patch.object(d.Api, 'call', side_effect=d.SessionExpired('expired')) as upload, \
                patch.object(self.api, 'authenticate') as auth:
            with self.assertRaises(d.SessionExpired):
                self.api.call('Upload', yyxh='slot')
        upload.assert_called_once()
        auth.assert_not_called()

    def test_api_distinguishes_expiry_from_business_error(self):
        for message, exception in [('令牌已失效', d.SessionExpired), ('Session expired', d.SessionExpired),
                                   ('未登录', d.SessionExpired), ('无号', d.ApiError)]:
            with self.subTest(message=message):
                self.page.evaluate.return_value = {'Head': {'ErrorNo': '1', 'ErrorStr': message}}
                with self.assertRaises(exception):
                    d.Api(self.page).call('GetNeeds')


class HeadlessBrowserTests(unittest.TestCase):
    def test_stale_session_and_mid_query_expiry_reauthenticate_without_gui(self):
        import json
        from urllib.parse import parse_qs, urlsplit
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            server = {'token': 'fresh-1', 'expired_in_query': False, 'logins': 0}
            uploads = []

            def handle(route):
                url = urlsplit(route.request.url)
                if url.path.startswith('/api/Appt/'):
                    endpoint = url.path.rsplit('/', 1)[-1]
                    if endpoint == 'Upload':
                        uploads.append(endpoint)
                    token = parse_qs(url.query).get('sessionKey', [''])[0]
                    if endpoint == 'GetYyrq' and not server['expired_in_query']:
                        server['expired_in_query'] = True
                        server['token'] = 'fresh-2'
                    if token != server['token']:
                        result = {'Head': {'ErrorNo': 1, 'ErrorStr': '登录会话已过期'}}
                    else:
                        data = {
                            'GetNeeds': {'Needs': [{'text': '洁治(洗牙)', 'value': '1'}]},
                            'GetApptList': {'List': []},
                            'GetDoctor': {'Doctor': [{'text': 'doctor', 'value': '2'}]},
                            'GetYyrq': {'YYRQ': [{'text': '2026-10-26 09:00', 'value': 'slot'}]},
                        }
                        result = {'Head': {'ErrorNo': 0}, 'Data': data.get(endpoint, {})}
                    route.fulfill(content_type='application/json', body=json.dumps(result))
                elif url.path == '/authenticate':
                    server['logins'] += 1
                    route.fulfill(content_type='text/html; charset=utf-8', body='''<script>
                        localStorage.setItem('token', %s);
                        localStorage.setItem('userid', 'dummy-user');
                        location.href='/entry';</script>''' % json.dumps(server['token']))
                else:
                    route.fulfill(content_type='text/html; charset=utf-8', body='''
                        <div id="li1"></div>
                        <form action="/authenticate" id="auth">
                          <input id="user_name"><input id="password" type="password">
                          <input id="logon_button" type="submit">
                        </form>
                        <script>if(localStorage.getItem('token'))
                          document.getElementById('auth').remove();</script>''')

            page.route('**/*', handle)
            page.goto('http://dentist.test/entry')
            page.evaluate("localStorage.setItem('token','stale'); localStorage.setItem('userid','dummy-user')")
            config = dict(username='dummy-user', password='dummy-password', login_timeout_seconds=10,
                          project='洁治(洗牙)', max_attempts=1, interval_seconds=5)
            api = d.SessionApi(page, page.context, config)
            with tempfile.TemporaryDirectory() as temp, patch.object(d, 'PRIVATE', Path(temp)), \
                    patch.object(d, 'DENTIST', 'http://dentist.test/entry'):
                api.open()
                self.assertEqual(server['logins'], 1)
                d.book(api, config, SimpleNamespace(submit=False),
                       datetime(2026, 10, 19, 9, tzinfo=d.BEIJING))
                self.assertEqual(server['logins'], 2)
                state = json.loads((Path(temp) / 'session.json').read_text())
                self.assertTrue(state['origins'])
                self.assertIn('2026-10-26', (Path(temp) / 'slots.json').read_text())
            self.assertEqual(uploads, [])
            browser.close()


    def test_portal_landing_then_second_auth_form_with_same_url(self):
        from playwright.sync_api import sync_playwright
        from urllib.parse import urlsplit
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            server = {'logins': 0}
            def handle(route):
                url = urlsplit(route.request.url)
                if url.hostname == 'portal.pku.edu.cn' and url.path.endswith('/entry'):
                    route.fulfill(content_type='text/html', body="<script>location.href='http://iaaa.pku.edu.cn/iaaa/oauth.jsp'</script>")
                elif url.path == '/authenticate':
                    server['logins'] += 1
                    target = ('http://portal.pku.edu.cn/portal2017/index.jsp'
                              if server['logins'] == 1 else 'http://dentist.test/home')
                    route.fulfill(content_type='text/html', body='<script>location.href=' + repr(target) + '</script>')
                elif url.hostname == 'iaaa.pku.edu.cn':
                    route.fulfill(content_type='text/html; charset=utf-8', body='''<form action="/authenticate">
                        <input id="user_name"><input id="password" type="password">
                        <button id="logon_button">登录</button></form>''')
                elif url.hostname == 'dentist.test':
                    route.fulfill(content_type='text/html; charset=utf-8', body='''<div id="li1"></div><script>
                        localStorage.setItem('token','dummy');localStorage.setItem('userid','dummy');
                        </script>''')
                else:
                    route.fulfill(content_type='text/html; charset=utf-8', body='<h1>Portal</h1>')
            page.route('**/*', handle)
            with patch.object(d, 'DENTIST', 'http://portal.pku.edu.cn/portal2017/entry'):
                d.login(page, page.context, ('dummy', 'dummy'), False, 15, dentist=True)
            self.assertEqual(server['logins'], 2)
            self.assertTrue(d.dentist_ready(page))
            browser.close()

    def test_bad_password_stops_with_specific_error_without_timeout(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.route('**/*', lambda route: route.fulfill(content_type='text/html; charset=utf-8', body='''
                <input id="user_name"><input id="password" type="password">
                <div id="msg"></div><button id="logon_button"
                onclick="document.getElementById('msg').innerText='用户名或密码错误'">登录</button>'''))
            with patch.object(d, 'DENTIST', 'http://dentist.test/login'):
                with self.assertRaisesRegex(d.Stop, '拒绝账号或密码'):
                    d.login(page, page.context, ('dummy', 'dummy'), False, 10, dentist=True)
            browser.close()


    def test_slow_otp_response_does_not_prompt_twice(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.route('**/*', lambda route: route.fulfill(content_type='text/html; charset=utf-8', body='''
                <input id="user_name"><input id="password" type="password"><input id="otp_code">
                <div id="msg"></div><button id="logon_button" onclick="
                if(document.getElementById('otp_code').value) {
                  document.getElementById('msg').innerText='正在登录';
                  setTimeout(() => {
                    document.body.innerHTML='&lt;div id=li1&gt;&lt;/div&gt;';
                    localStorage.setItem('token','dummy');localStorage.setItem('userid','dummy');
                  },2300);
                }">登录</button>'''))
            with patch.object(d, 'DENTIST', 'http://dentist.test/login'), \
                    patch.object(d.sys.stdin, 'isatty', return_value=True), \
                    patch.object(d.getpass, 'getpass', return_value='123456') as prompt:
                d.login(page, page.context, ('dummy', 'dummy'), False, 10, dentist=True)
            prompt.assert_called_once()
            browser.close()


if __name__ == '__main__':
    unittest.main()
