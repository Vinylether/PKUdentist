#!/usr/bin/env python3
"""北京大学 i看牙：浏览器登录、检查号源、北京时间定时预约。"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import getpass
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, time as day_time, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import yaml
from playwright.sync_api import Error as PlaywrightError, sync_playwright

ROOT = Path(__file__).resolve().parent
PRIVATE = ROOT / '.private'
PORTAL = 'https://portal.pku.edu.cn/'
DENTIST = 'https://portal.pku.edu.cn/portal2017/util/portletRedir.do?portletId=dentistAppointment'
BEIJING = ZoneInfo('Asia/Shanghai')
LOG = logging.getLogger('dentist')


class Stop(RuntimeError):
    pass


def now():
    return datetime.now(BEIJING)


def release_time(current, explicit=None):
    """下一个北京时间 9:00；显式日期支持当天立即补捡。"""
    if explicit:
        return datetime.combine(datetime.strptime(explicit, '%Y-%m-%d').date(), day_time(9), BEIJING)
    target = current.astimezone(BEIJING).replace(hour=9, minute=0, second=0, microsecond=0)
    if current >= target:
        target += timedelta(days=1)
    return target


def load_config(path):
    try:
        config = yaml.safe_load(path.read_text(encoding='utf-8'))
    except (OSError, yaml.YAMLError):
        raise Stop('无法读取 YAML 配置，请检查文件路径和格式。') from None
    if not isinstance(config, dict):
        raise Stop('YAML 配置必须是键值映射。')
    config.setdefault('start_time', '09:00')
    parse_start_time(config['start_time'])
    for key in ('username', 'password', 'project'):
        if not isinstance(config.get(key), str) or not config[key].strip():
            raise Stop(f'配置 {key} 必须是非空字符串；账号和密码请加引号。')
    for key, minimum in (('interval_seconds', 5), ('max_attempts', 1), ('login_timeout_seconds', 1)):
        value = config.get(key)
        if type(value) is not int or value < minimum:
            raise Stop(f'配置 {key} 必须是至少为 {minimum} 的整数。')
    return config


def parse_start_time(value):
    if not isinstance(value, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value):
        raise Stop('配置 start_time 必须是带引号的 24 小时时间，例如 "09:00"。')
    return tuple(map(int, value.split(':')))


def save_json(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.chmod(0o600)
    temp.replace(path)


def save_session(context):
    save_json(PRIVATE / 'session.json', context.storage_state())


@contextlib.contextmanager
def lock():
    PRIVATE.mkdir(mode=0o700, exist_ok=True)
    PRIVATE.chmod(0o700)
    with (PRIVATE / 'run.lock').open('w') as fp:
        try:
            fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Stop('已有程序运行，请先结束另一个进程。')
        yield


def goto(page, url):
    response = page.goto(url, wait_until='domcontentloaded', timeout=60000)
    if response and response.status in (403, 429):
        raise Stop(f'网站返回 {response.status}，请检查网络或稍后再试。')


def first_visible(page, selector):
    for frame in page.frames:
        for el in frame.locator(selector).all():
            if el.is_visible():
                return el
    return None


def dentist_ready(page):
    return bool(page.locator('#li1').count() and page.evaluate(
        "Boolean(localStorage.getItem('token') && localStorage.getItem('userid'))"))


def login_error(page):
    """只输出已知错误类别，避免认证页面原文泄漏账号或令牌。"""
    message = first_visible(page, '#msg')
    if message is None:
        return None
    text = message.inner_text().strip()
    for pattern, reason in (
        (r'用户名或密码|密码错误|账号.*错误|incorrect.*password|invalid.*credentials', '统一认证拒绝账号或密码，请检查 config.yaml。'),
        (r'未激活|未绑定|先绑定|not activated|not bound', '统一认证要求先激活账号或绑定手机令牌。'),
        (r'密码强度|weak password', '统一认证要求修改密码，请先完成账号密码更新。'),
        (r'锁定|频繁|次数.*限制|locked|too many', '统一认证限制了登录尝试，请稍后再试。'),
        (r'系统服务异常|系统异常|service.*exception', '统一认证服务异常，请稍后重试。'),
    ):
        if re.search(pattern, text, re.IGNORECASE):
            return reason
    return None


def login(page, context, credentials, headed, timeout, otp_file=None, *, dentist=False):
    goto(page, DENTIST if dentist else PORTAL)
    deadline = time.monotonic() + timeout
    submitted = False
    portal_ready_since = None
    form_document = None
    submissions = 0
    portal_returns = 0
    challenge_attempts = 0
    while time.monotonic() < deadline:
        if dentist and dentist_ready(page):
            return
        user = first_visible(page, '#user_name')
        password = first_visible(page, '#password')
        if user and password:
            # 门户与业务端可能各有一轮认证，甚至重用相同 URL；按文档识别新表单。
            document = user.evaluate("el => { const w = el.ownerDocument.defaultView; return w.__dentistAuthDocument || (w.__dentistAuthDocument = String(Date.now()) + Math.random()); }")
            if document != form_document:
                form_document = document
                submitted = False
        error = login_error(page)
        if error:
            raise Stop(error)
        if user and password and not submitted:
            if submissions >= 3:
                raise Stop('统一认证反复返回登录表单，已停止以避免重复尝试。')
            user.fill(credentials[0]); password.fill(credentials[1])
            button = first_visible(page, '#logon_button')
            if button:
                button.click()
                submitted = True
                submissions += 1
                LOG.info('已提交统一认证登录，等待跳转。')
        if urlsplit(page.url).hostname == 'portal.pku.edu.cn' and '/login' not in page.url and '/portal2017/' in page.url and not password:
            # 门户中间页会继续跳往认证站，等待页面稳定后再保存会话。
            if portal_ready_since is None:
                portal_ready_since = time.monotonic()
            elif time.monotonic() - portal_ready_since >= 2:
                if dentist:
                    if portal_returns >= 2:
                        raise Stop('门户认证成功，但反复返回门户首页，无法进入 i看牙。')
                    LOG.info('门户认证已完成，继续进入 i看牙业务入口。')
                    portal_returns += 1
                    goto(page, DENTIST)
                    portal_ready_since = None
                    submitted = False
                    continue
                else:
                    save_session(context)
                    return
        else:
            portal_ready_since = None
        if submitted:
            challenge = first_visible(page, '#sms_code, #otp_code, #valid_code')
            if challenge and not headed:
                message = first_visible(page, '#msg')
                rejected = bool(message and re.search(
                    r'错误|过期|invalid|incorrect|expired', message.inner_text(), re.IGNORECASE))
                # 填过验证码后等待服务端处理，避免网络稍慢时重复索要动态码。
                if challenge.input_value() and not rejected:
                    page.wait_for_timeout(500)
                    continue
                if challenge_attempts >= 3:
                    raise Stop('验证码验证多次未完成，已停止，请核对手机令牌时间后重新运行 login。')
                if otp_file and otp_file.exists():
                    code = otp_file.read_text().strip()
                    otp_file.unlink()
                elif sys.stdin.isatty():
                    code = getpass.getpass('请输入当前手机令牌/验证码（输入不回显）：')
                else:
                    raise Stop('需要验证码或二次验证，请在交互终端运行 login 并输入动态码。')
                challenge.fill(code)
                challenge_attempts += 1
                first_visible(page, '#logon_button').click()
                page.wait_for_timeout(1500)
        page.wait_for_timeout(500)
    inspect_page(page)
    location = urlsplit(page.url)
    raise Stop(f'登录等待超时（停留在 {location.hostname}{location.path}）；页面诊断已保存到 .private/inspection.json 和 .private/page.png。')


def enter_dentist(page, context=None, credentials=None, headed=False, timeout=180):
    if context is not None and credentials is not None:
        login(page, context, credentials, headed, timeout, dentist=True)
        return
    goto(page, DENTIST)
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        # 统一认证站也是正常 SSO 跳转的中间页，不能一到这里就判定过期。
        if dentist_ready(page):
            return
        page.wait_for_timeout(500)
    host = urlsplit(page.url).hostname or ''
    if host == 'iaaa.pku.edu.cn' or '/login.jsp' in page.url:
        raise Stop('i看牙统一认证未完成；请在交互终端运行 login 完成认证。')
    raise Stop('i看牙登录跳转未完成，请运行 login 检查是否需要二次认证。')


def inspect_page(page):
    """诊断内容仅保存在私有目录，不将个人信息写到日志。"""
    frames = []
    for i, frame in enumerate(page.frames):
        body = frame.locator('body')
        text = body.inner_text() if body.count() else ''
        frames.append({'index': i, 'text': text, 'controls': frame.locator('input,select,button,a').evaluate_all(
            '(es)=>es.map(e=>({tag:e.tagName,id:e.id,name:e.name,type:e.type,text:e.innerText,placeholder:e.placeholder}))')})
    save_json(PRIVATE / 'inspection.json', frames)
    page.screenshot(path=str(PRIVATE / 'page.png'), full_page=True)
    LOG.info('页面诊断已保存到 .private/inspection.json 和 .private/page.png（含个人信息，请勿公开）。')


def wait_until(target, check=None):
    LOG.info('等待北京时间 %s；对应预约日期 %s。', target.isoformat(), (target + timedelta(days=7)).date())
    while (remaining := (target - now()).total_seconds()) > 0:
        if check is not None:
            check()
        remaining = (target - now()).total_seconds()
        if remaining > 0:
            time.sleep(min(remaining, 30))
    if check is not None:
        check()


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--headed', action='store_true', help='已停用；二次验证在终端完成')
    p.add_argument('--config', type=Path, default=ROOT / 'config.yaml')
    sub = p.add_subparsers(dest='command', required=True)
    login_cmd = sub.add_parser('login', help='登录门户并保存会话')
    login_cmd.add_argument('--headed', action='store_true', default=argparse.SUPPRESS)
    inspect_cmd = sub.add_parser('inspect', help='登录后查看规则并保存私有诊断')
    inspect_cmd.add_argument('--headed', action='store_true', default=argparse.SUPPRESS)
    run = sub.add_parser('run', help='预约洗牙，默认仅预览；--submit 实际提交')
    run.add_argument('--headed', action='store_true', default=argparse.SUPPRESS)
    run.add_argument('--submit', action='store_true', help='自动提交预约')
    run.add_argument('--at', help='放号日 YYYY-MM-DD，北京时间 9:00 启动')
    run.add_argument('--now', action='store_true', help='立即查询，无需等待下一次 9:00')
    return p


class ApiError(Stop):
    pass


class TransportError(Stop):
    pass


class SessionExpired(TransportError):
    pass


def expired_message(message):
    return bool(re.search(
        r"(?:会话|登录|登陆|认证|身份|令牌|token|session).{0,20}(?:过期|失效|无效|超时|不存在|expired|invalid)"
        r"|(?:重新|未|尚未)(?:登录|登陆)|(?:身份|用户)验证失败"
        r"|(?:invalid|expired)\s+(?:token|session)|not logged in|unauthorized",
        str(message), re.IGNORECASE))


class Api:
    def __init__(self, page):
        self.page = page

    def call(self, endpoint, **params):
        if endpoint not in {'GetNeeds', 'GetDoctor', 'GetYyrq', 'GetApptList', 'Upload'}:
            raise Stop('未允许的接口。')
        try:
            result = self.page.evaluate(r"""async ({endpoint, params}) => {
          if (location.hostname === 'iaaa.pku.edu.cn' || /\/login\./i.test(location.pathname))
            return {authExpired: true};
          const token = localStorage.getItem('token'), uid = localStorage.getItem('userid');
          if (!token || !uid) return {authExpired: true};
          const q = {sessionKey: token, uid, ...params};
          if (endpoint === 'Upload') {
            delete q.uid;
            q.id = uid; q.name = localStorage.getItem('username'); q.phone = localStorage.getItem('phone');
            if (!q.name || !q.phone) return {clientError: '请先在 i看牙“我的”中补全姓名和联系方式'};
          }
          const controller = new AbortController();
          const timer = setTimeout(() => controller.abort(), 20000);
          try {
            const r = await fetch('/api/Appt/' + endpoint + '?' + new URLSearchParams(q),
              {method: 'POST', signal: controller.signal});
            if (r.status === 401) return {authExpired: true};
            if (r.redirected && /iaaa\.pku\.edu\.cn|\/login(?:\.|\/|$)/i.test(r.url))
              return {authExpired: true};
            if (!r.ok) return {clientError: '接口 HTTP ' + r.status};
            const text = await r.text();
            try { return JSON.parse(text); }
            catch (_) {
              if (/id=["'](?:user_name|logon_button)["']/i.test(text)) return {authExpired: true};
              return {clientError: '接口返回非 JSON 数据'};
            }
          } finally { clearTimeout(timer); }
        }""", {'endpoint': endpoint, 'params': params})
        except PlaywrightError:
            raise TransportError('业务接口请求失败或超时。') from None
        if not isinstance(result, dict):
            raise TransportError('接口返回格式异常。')
        if result.get('authExpired'):
            raise SessionExpired('业务会话已过期。')
        if result.get('clientError'):
            raise TransportError(result['clientError'])
        if not isinstance(result.get('Head'), dict) or 'ErrorNo' not in result['Head']:
            raise TransportError('接口返回登录提示或未知格式，请重新登录。')
        if str(result['Head']['ErrorNo']) != '0':
            message = str(result['Head'].get('ErrorStr') or '预约系统拒绝请求。')
            if expired_message(message):
                raise SessionExpired('业务接口明确提示登录过期。')
            raise ApiError(message)
        data = result.get('Data') or {}
        if not isinstance(data, dict):
            raise TransportError('业务接口 Data 格式异常。')
        return data

    def rows(self, endpoint, field, **params):
        result = self.call(endpoint, **params)
        if field not in result:
            raise ApiError('接口缺少预期字段：' + field)
        rows = result[field] or []
        if not isinstance(rows, list):
            raise ApiError('号源列表格式异常。')
        return rows

    def records(self):
        return self.rows('GetApptList', 'List')


class SessionApi(Api):
    """只读请求有限重试；Upload 始终单次调用，由 submit_once 保护。"""
    def __init__(self, page, context, config):
        super().__init__(page)
        self.context = context
        self.config = config

    def authenticate(self, force=False):
        try:
            if force:
                LOG.info('业务会话已过期，开始无界面重新认证。')
                # 丢弃过期业务令牌；保留门户 cookie，让仍有效的 SSO 可自动续认证。
                self.page.evaluate("() => { localStorage.removeItem('token'); localStorage.removeItem('userid'); }")
            enter_dentist(self.page, self.context,
                          (self.config['username'], self.config['password']),
                          False, self.config['login_timeout_seconds'])
        except PlaywrightError:
            raise TransportError('无界面认证请求失败或超时。') from None

    def open(self):
        for attempt in range(3):
            try:
                self.authenticate()
                self.validate()
                return
            except TransportError:
                if attempt == 2:
                    raise
                LOG.warning('登录准备暂时失败，5 秒后重试。')
                time.sleep(5)

    def call(self, endpoint, **params):
        if endpoint == 'Upload':
            return super().call(endpoint, **params)
        reauthenticate = False
        refreshed = False
        for attempt in range(3):
            try:
                if reauthenticate:
                    self.authenticate(force=True)
                    reauthenticate = False
                    refreshed = True
                result = super().call(endpoint, **params)
                if refreshed:
                    save_session(self.context)
                    LOG.info('无界面重新认证成功，业务查询已恢复。')
                return result
            except SessionExpired:
                if attempt == 2:
                    raise Stop('重新认证后业务会话仍无效，已停止。') from None
                reauthenticate = True
            except TransportError:
                if attempt == 2:
                    raise
                LOG.warning('业务查询暂时失败，5 秒后重试（%s/2）。', attempt + 1)
                time.sleep(5)

    def validate(self):
        # 必须用业务接口验证服务端会话，不能仅判断页面元素或本地 token。
        self.rows('GetNeeds', 'Needs')
        self.records()
        save_session(self.context)


def project_id(rows, project):
    matches = []
    for row in rows:
        for suffix in ('', '2'):
            if row.get('text' + suffix) == project:
                matches.append(str(row['value' + suffix]))
    if len(matches) != 1:
        raise Stop('未找到唯一的预约项目：' + project)
    return matches[0]


def parse_slot_date(text, reference=None):
    match = re.search(r'(20\d{2})[-/年.](\d{1,2})[-/月.](\d{1,2})', text)
    if not match:
        match = re.search(r'(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)', text)
    if match:
        return datetime(*map(int, match.groups())).date()
    # 实际 GetYyrq 返回如“10-22 15:30\t星期四”，不包含年份。
    match = re.search(r'(?<!\d)(\d{1,2})[-/月.](\d{1,2})(?!\d)', text)
    if match and reference:
        candidates = []
        for year in (reference.year - 1, reference.year, reference.year + 1):
            try:
                candidates.append(datetime(year, int(match[1]), int(match[2])).date())
            except ValueError:
                continue
        if candidates:
            return min(candidates, key=lambda date: abs((date - reference).days))
    raise Stop('号源日期格式未知，停止以避免选错日期。')


def slot_sort_key(slot):
    match = re.search(r'(\d{1,2}):(\d{2})', slot['text'])
    if not match or int(match[1]) > 23 or int(match[2]) > 59:
        raise Stop('号源时间格式未知，停止以避免选错就诊时间。')
    return (int(match[1]), int(match[2]), slot['doctor'])


def collect_slots(api, need, target_date):
    slots = []
    for doctor in api.rows('GetDoctor', 'Doctor', need=need):
        for slot in api.rows('GetYyrq', 'YYRQ', need=need, doctor=str(doctor['value'])):
            if parse_slot_date(str(slot['text']), target_date) == target_date:
                slots.append({'value': str(slot['value']), 'text': str(slot['text']),
                              'doctor': str(doctor['text']), 'doctor_id': str(doctor['value'])})
    return sorted(slots, key=slot_sort_key)


def check_existing(records, target_date):
    target_week = target_date.isocalendar()[:2]
    for row in records:
        if str(row.get('Status')) in {'-1', '2'}:
            continue
        date = parse_slot_date(str(row.get('YYRQ', '')), target_date)
        if date.isocalendar()[:2] == target_week:
            raise Stop('目标周已有预约或就诊记录；每人每周只可预约一个初诊号。请在 i看牙核对。')


def submit_once(api, slot, target_date, state_path):
    """提交前落盘；任何超时、崩溃或不明确的结果均不自动重试。"""
    if state_path.exists():
        raise Stop('已有提交记录，请到 i看牙核对结果。程序不会重复提交。')
    check_existing(api.records(), target_date)
    state = {'status': 'pending', 'created_at': now().isoformat(), 'target_date': str(target_date), 'slot': slot}
    save_json(state_path, state)
    try:
        result = api.call('Upload', yyxh=slot['value'])
    except ApiError as exc:
        state['status'] = 'rejected'
        save_json(state_path, state)
        raise Stop('预约接口未接受请求：' + str(exc)) from None
    except Exception:
        state['status'] = 'unknown'
        save_json(state_path, state)
        raise Stop('提交结果不明确，已停止。请到 i看牙预约记录核对，勿直接重试。') from None
    state['status'] = 'accepted'
    save_json(state_path, state)
    try:
        records = api.records()
        matching = [r for r in records if str(r.get('Status')) not in {'-1', '2'}
                    and parse_slot_date(str(r.get('YYRQ', '')), target_date) == target_date
                    and r.get('Doctor') == slot['doctor']
                    and (not slot.get('project') or r.get('Need') == slot['project'])]
        if matching:
            state['status'] = 'confirmed'
            save_json(state_path, state)
    except Exception:
        LOG.warning('提交接口已返回成功，预约记录复核未完成，请在 i看牙确认。')
    LOG.info('预约接口返回成功：%s，%s；记录状态：%s。', slot['text'], slot['doctor'], state['status'])
    return state


def book(api, config, args, release):
    target_date = (release + timedelta(days=7)).date()
    project = config['project']
    start_time = config.get('start_time', '09:00')
    earliest = parse_start_time(start_time)
    state_path = PRIVATE / ('booking-' + str(target_date) + '.json')
    if args.submit and state_path.exists():
        raise Stop('目标日期已有本地提交记录，请核对 .private/booking-*.json 与网站预约记录。')
    check_existing(api.records(), target_date)
    need = project_id(api.rows('GetNeeds', 'Needs'), project)
    LOG.info('目标：%s，%s %s 起，选择第一个可用时段。', project, target_date, start_time)
    for attempt in range(config['max_attempts']):
        slots = [slot for slot in collect_slots(api, need, target_date)
                 if slot_sort_key(slot)[:2] >= earliest]
        save_json(PRIVATE / 'slots.json', {'target_date': str(target_date), 'slots': slots})
        if slots:
            slot = {**slots[0], 'project': project}
            LOG.info('找到号源：%s，%s。', slot['text'], slot['doctor'])
            if args.submit:
                return submit_once(api, slot, target_date, state_path)
            LOG.info('预览完成，未提交。自动预约请添加 --submit。')
            return
        LOG.info('第 %s/%s 次查询：%s %s 起暂无号源。', attempt + 1, config['max_attempts'], target_date, start_time)
        if attempt + 1 < config['max_attempts']:
            time.sleep(config['interval_seconds'])
    raise Stop('已达到查询次数上限，未预约。')


def run_command(argv=None):
    args = parser().parse_args(argv)
    logging.Formatter.converter = staticmethod(lambda seconds: datetime.fromtimestamp(seconds, BEIJING).timetuple())
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [北京时间] %(message)s')
    os.umask(0o077)
    if args.headed:
        raise Stop('本程序使用无界面浏览器；请移除 --headed，二次验证在终端完成。')
    config = load_config(args.config)
    release = None
    if args.command == 'run':
        if args.now and args.at:
            raise Stop('--now 和 --at 不能同时使用。')
        release = now().replace(hour=9, minute=0, second=0, microsecond=0) if args.now else release_time(now(), args.at)
        # 只在放号前五分钟打开浏览器，避免长时间闲置让会话过期。
        if not args.now:
            prepare = release - timedelta(minutes=5)
            while now() < prepare:
                LOG.info('预约任务待命，放号时间：%s', release.isoformat())
                time.sleep(min(30, (prepare - now()).total_seconds()))
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        session = PRIVATE / 'session.json'
        context = browser.new_context(storage_state=str(session) if session.exists() else None, locale='zh-CN', timezone_id='Asia/Shanghai')
        page = context.new_page()
        page.on('dialog', lambda dialog: dialog.dismiss())
        try:
            api = SessionApi(page, context, config)
            api.open()
            if args.command == 'login':
                LOG.info('门户与 i看牙登录成功，会话已保存。')
            elif args.command == 'inspect':
                inspect_page(page)
                for row in api.rows('GetNeeds', 'Needs'):
                    if row.get('text') == config['project']:
                        LOG.info('%s：%s', row['text'], row['Online'])
            else:
                # 放号前验证登录、项目、联系方式与已有记录。
                api.rows('GetNeeds', 'Needs')
                if not page.evaluate("Boolean(localStorage.getItem('phone'))"):
                    raise Stop('请先在 i看牙“我的”中添加联系方式。')
                check_existing(api.records(), (release + timedelta(days=7)).date())
                if not args.now:
                    wait_until(release, check=api.validate)
                book(api, config, args, release)
        finally:
            context.close()
            browser.close()
    return 0


def main(argv=None):
    with lock():
        return run_command(argv)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('已停止。', file=sys.stderr)
        sys.exit(130)
    except Stop as exc:
        LOG.error('%s', exc)
        sys.exit(2)
    except Exception as exc:
        # 浏览器异常可能包含带令牌的 URL，避免将原始异常写入终端。
        LOG.error('运行失败（%s）；请检查网络或运行 inspect。', type(exc).__name__)
        sys.exit(1)
