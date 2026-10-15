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
from playwright.sync_api import sync_playwright

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
    for key in ('username', 'password', 'project'):
        if not isinstance(config.get(key), str) or not config[key].strip():
            raise Stop(f'配置 {key} 必须是非空字符串；账号和密码请加引号。')
    for key, minimum in (('interval_seconds', 5), ('max_attempts', 1), ('login_timeout_seconds', 1)):
        value = config.get(key)
        if type(value) is not int or value < minimum:
            raise Stop(f'配置 {key} 必须是至少为 {minimum} 的整数。')
    return config


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


def login(page, context, credentials, headed, timeout, otp_file=None):
    goto(page, PORTAL)
    deadline = time.monotonic() + timeout
    submitted = False
    while time.monotonic() < deadline:
        user = first_visible(page, '#user_name')
        password = first_visible(page, '#password')
        if user and password and not submitted:
            user.fill(credentials[0]); password.fill(credentials[1])
            button = first_visible(page, '#logon_button')
            if button:
                button.click()
                submitted = True
                LOG.info('已提交统一认证登录，等待跳转。')
        if urlsplit(page.url).hostname == 'portal.pku.edu.cn' and '/login' not in page.url and '/portal2017/' in page.url:
            save_session(context)
            return
        if submitted:
            challenge = first_visible(page, '#sms_code, #otp_code, #valid_code')
            if challenge and not headed:
                if otp_file and otp_file.exists():
                    code = otp_file.read_text().strip()
                    otp_file.unlink()
                elif sys.stdin.isatty():
                    code = getpass.getpass('请输入当前手机令牌/验证码（输入不回显）：')
                else:
                    raise Stop('需要验证码或二次验证，请在终端运行 login，或使用 --headed。')
                challenge.fill(code)
                first_visible(page, '#logon_button').click()
                page.wait_for_timeout(1500)
        page.wait_for_timeout(500)
    raise Stop('登录未完成；请用 login --headed 查看提示并完成可能的二次验证。')


def enter_dentist(page):
    goto(page, DENTIST)
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        host = urlsplit(page.url).hostname or ''
        if host == 'iaaa.pku.edu.cn' or '/login.jsp' in page.url:
            raise Stop('登录已过期，请先运行 login。')
        if page.locator('#li1').count():
            if page.evaluate("Boolean(localStorage.getItem('token') && localStorage.getItem('userid'))"):
                return
        page.wait_for_timeout(500)
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


def wait_until(target):
    LOG.info('等待北京时间 %s；对应预约日期 %s。', target.isoformat(), (target + timedelta(days=7)).date())
    while (remaining := (target - now()).total_seconds()) > 0:
        time.sleep(min(remaining, 30))


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--headed', action='store_true', help='显示浏览器，供手动二次验证')
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


class Api:
    def __init__(self, page):
        self.page = page

    def call(self, endpoint, **params):
        if endpoint not in {'GetNeeds', 'GetDoctor', 'GetYyrq', 'GetApptList', 'Upload'}:
            raise Stop('未允许的接口。')
        result = self.page.evaluate("""async ({endpoint, params}) => {
          const token = localStorage.getItem('token'), uid = localStorage.getItem('userid');
          if (!token || !uid) return {clientError: '登录会话不存在，请重新登录'};
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
            if (!r.ok) return {clientError: '接口 HTTP ' + r.status};
            return await r.json();
          } finally { clearTimeout(timer); }
        }""", {'endpoint': endpoint, 'params': params})
        if not isinstance(result, dict):
            raise TransportError('接口返回格式异常。')
        if result.get('clientError'):
            raise TransportError(result['clientError'])
        if 'Head' not in result or 'ErrorNo' not in result['Head']:
            raise TransportError('接口返回登录提示或未知格式，请重新登录。')
        if str(result['Head']['ErrorNo']) != '0':
            raise ApiError(str(result['Head'].get('ErrorStr') or '预约系统拒绝请求。'))
        return result.get('Data') or {}

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
    return (int(match[1]), int(match[2]), slot['doctor']) if match else (24, 0, slot['doctor'])


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
    state_path = PRIVATE / ('booking-' + str(target_date) + '.json')
    if args.submit and state_path.exists():
        raise Stop('目标日期已有本地提交记录，请核对 .private/booking-*.json 与网站预约记录。')
    check_existing(api.records(), target_date)
    need = project_id(api.rows('GetNeeds', 'Needs'), project)
    LOG.info('目标：%s，%s 全天，按最早时间选择。', project, target_date)
    for attempt in range(config['max_attempts']):
        slots = collect_slots(api, need, target_date)
        save_json(PRIVATE / 'slots.json', {'target_date': str(target_date), 'slots': slots})
        if slots:
            slot = {**slots[0], 'project': project}
            LOG.info('找到号源：%s，%s。', slot['text'], slot['doctor'])
            if args.submit:
                return submit_once(api, slot, target_date, state_path)
            LOG.info('预览完成，未提交。自动预约请添加 --submit。')
            return
        LOG.info('第 %s/%s 次查询：%s 暂无号源。', attempt + 1, config['max_attempts'], target_date)
        if attempt + 1 < config['max_attempts']:
            time.sleep(config['interval_seconds'])
    raise Stop('已达到查询次数上限，未预约。')


def run_command(argv=None):
    args = parser().parse_args(argv)
    logging.Formatter.converter = staticmethod(lambda seconds: datetime.fromtimestamp(seconds, BEIJING).timetuple())
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [北京时间] %(message)s')
    os.umask(0o077)
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
        browser = p.chromium.launch(headless=not args.headed)
        session = PRIVATE / 'session.json'
        context = browser.new_context(storage_state=str(session) if session.exists() else None, locale='zh-CN', timezone_id='Asia/Shanghai')
        page = context.new_page()
        page.on('dialog', lambda dialog: dialog.dismiss())
        try:
            if args.command == 'login' or not session.exists():
                login(page, context, (config['username'], config['password']), args.headed, config['login_timeout_seconds'])
            enter_dentist(page)
            # 门户跳转后 token 存于预约系统 localStorage，必须一并保存。
            save_session(context)
            if args.command == 'login':
                Api(page).rows('GetNeeds', 'Needs')
                LOG.info('门户与 i看牙登录成功，会话已保存。')
            elif args.command == 'inspect':
                inspect_page(page)
                for row in Api(page).rows('GetNeeds', 'Needs'):
                    if row.get('text') == config['project']:
                        LOG.info('%s：%s', row['text'], row['Online'])
            else:
                api = Api(page)
                # 放号前验证登录、项目、联系方式与已有记录。
                api.rows('GetNeeds', 'Needs')
                if not page.evaluate("Boolean(localStorage.getItem('phone'))"):
                    raise Stop('请先在 i看牙“我的”中添加联系方式。')
                check_existing(api.records(), (release + timedelta(days=7)).date())
                if not args.now:
                    wait_until(release)
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
