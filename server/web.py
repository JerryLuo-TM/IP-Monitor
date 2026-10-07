"""Dependency-free LAN dashboard. Start with WEB_PASSWORD set."""
import base64
from datetime import datetime, timedelta, timezone
import hmac
import ipaddress
import json
import os
from pathlib import Path
import queue
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import monitor

ROOT = Path(__file__).parent
DATA = Path(os.getenv('DATA_DIR', '/data'))
DEFAULTS = dict(MONITOR_NAME='电信宽带', CHECK_INTERVAL=60, CONFIRM_COUNT=2,
                REMIND_INTERVAL=21600, FAIL_THRESHOLD=3, SMTP_HOST='', SMTP_PORT=465,
                SMTP_SECURITY='ssl', SMTP_USER='', SMTP_FROM='', SMTP_TO='',
                SMTP_PASSWORD='', MAIL_ENABLED=False,
                UNICOM_NAME='联通宽带', DUAL_ROUTING_READY=False,
                UNICOM_ALERT_OUTSIDE=False)
CONFIG_PATH = DATA / 'web-config.json'
STATE_PATH = DATA / 'web-state.json'
LOCK = threading.RLock()
JOBS = queue.Queue(maxsize=4)
CSRF = secrets.token_urlsafe(32)
CONFIG = dict(DEFAULTS)
STATUS = dict(ip=None, location='', risk=None, last_check=None, last_success=None, next_check=None,
              busy=False, error='', mail_status='尚未发送', history=[])
UNICOM_NETWORK = ipaddress.ip_network('119.4.0.0/16')
MAX_PENDING_IP_CHANGES = 200


def unicom_status():
    return dict(ip=None, location='', risk=None, last_check=None, last_success=None,
                error='等待配置双线源端口分流', ready=False)


STATUS['unicom'] = unicom_status()


def atomic_write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    temporary.chmod(0o600)
    temporary.replace(path)


def validate(values, old):
    result = dict(old)
    for key in DEFAULTS:
        if key in values:
            # Empty password preserves saved credential; never return it to UI.
            if key == 'SMTP_PASSWORD' and values[key] == '':
                continue
            result[key] = values[key]
    bounds = {'CHECK_INTERVAL': (15, 86400), 'CONFIRM_COUNT': (1, 20),
              'REMIND_INTERVAL': (0, 2592000), 'FAIL_THRESHOLD': (1, 100),
              'SMTP_PORT': (1, 65535)}
    for key, (low, high) in bounds.items():
        value = result[key]
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ValueError(f'{key} 必须为整数')
        result[key] = int(value)
        if not low <= result[key] <= high:
            raise ValueError(f'{key} 范围应为 {low}–{high}')
    if result['SMTP_SECURITY'] not in ('ssl', 'starttls'):
        raise ValueError('请选择 SSL 或 STARTTLS')
    booleans = {'MAIL_ENABLED', 'DUAL_ROUTING_READY', 'UNICOM_ALERT_OUTSIDE'}
    for key in booleans:
        if not isinstance(result[key], bool):
            raise ValueError('开关无效')
    for key in DEFAULTS.keys() - bounds.keys() - booleans:
        if not isinstance(result[key], str) or len(result[key]) > 1024:
            raise ValueError(f'{key} 格式无效')
        if '\r' in result[key] or '\n' in result[key]:
            raise ValueError(f'{key} 不得包含换行')
    if result['MAIL_ENABLED']:
        if not all(result[key].strip() for key in ('SMTP_HOST', 'SMTP_USER',
                   'SMTP_FROM', 'SMTP_TO', 'SMTP_PASSWORD')):
            raise ValueError('开启邮件前请填写完整的 SMTP 配置')
    return result


def event(kind, message):
    with LOCK:
        STATUS['history'].insert(0, dict(time=time.time(), kind=kind, message=message))
        del STATUS['history'][200:]


def setup():
    global CONFIG
    if CONFIG_PATH.exists():
        CONFIG = validate(json.loads(CONFIG_PATH.read_text(encoding='utf-8')), DEFAULTS)
    if STATE_PATH.exists():
        saved = json.loads(STATE_PATH.read_text(encoding='utf-8'))
        STATUS.update({k: v for k, v in saved.get('status', {}).items() if k in STATUS})
        state = saved.get('state', {})
    else:
        state = {}
    STATUS.update(busy=False, next_check=None, error='等待本次启动后的首次检测')
    state['count'] = 0
    state['failures'] = 0
    unicom = state.setdefault('unicom', {})
    unicom['count'] = 0
    unicom['failures'] = 0
    STATUS['unicom'].update(ready=CONFIG['DUAL_ROUTING_READY'],
        error='等待本次启动后的首次检测' if CONFIG['DUAL_ROUTING_READY'] else '等待配置双线源端口分流')
    return state


def configure_worker(cfg):
    # Only the single worker accesses monitor functions/environment.
    for key, value in cfg.items():
        os.environ[key] = str(value)
    os.environ['IP_URL'] = monitor.DEFAULT_IP_URL
    monitor.CONFIRM = cfg['CONFIRM_COUNT']
    monitor.REMIND = cfg['REMIND_INTERVAL']


def deliver(cfg, subject, body, test=False):
    if not cfg['MAIL_ENABLED'] and not test:
        return False
    try:
        monitor.send_mail(subject, body)
    except Exception:
        # Avoid exposing provider response text or credentials in the API/log.
        with LOCK:
            STATUS['mail_status'] = '发送失败：请检查 SMTP、授权码和联通分流'
        event('邮件失败', subject)
        return False
    with LOCK:
        STATUS['mail_status'] = 'SMTP 已接收：' + subject
    event('邮件', subject)
    return True


def query_error(exc):
    reason = getattr(exc, 'reason', exc)
    detail = ' '.join(str(reason).split())[:240]
    return f'{type(reason).__name__}: {detail}' if detail else type(reason).__name__


def record_ip_change(state, cfg, line, previous, current, now):
    if previous == current:
        return
    event(f'{line} · IP 变化', f'{previous or "首次检测"} → {current}')
    if previous and cfg['MAIL_ENABLED']:
        pending = state.setdefault('pending_ip_changes', [])
        pending.append(dict(previous=previous, current=current, time=now))
        if len(pending) > MAX_PENDING_IP_CHANGES:
            del pending[:-MAX_PENDING_IP_CHANGES]
            event(f'{line} · 邮件积压', '待发 IP 变化邮件超过 200 条，已保留最近 200 条')


def send_ip_change_mail(state, cfg, line, name):
    if not cfg['MAIL_ENABLED']:
        return
    pending = state.get('pending_ip_changes', [])
    while pending:
        change = pending[0]
        observed = datetime.fromtimestamp(change['time'], timezone(timedelta(hours=8)))
        body = (f'线路：{name}\n'
                f'上次成功检测 IP：{change["previous"]}\n'
                f'当前检测 IP：{change["current"]}\n'
                f'检测时间：{observed:%Y-%m-%d %H:%M:%S} UTC+08:00\n'
                '此邮件仅表示出口 IP 发生变化，不代表测速结果。')
        if not deliver(cfg, f'{line} · 出口 IP 变化', body):
            break
        pending.pop(0)


def check(state, cfg):
    now = time.time()
    with LOCK:
        STATUS['last_check'] = now
    try:
        sample = monitor.fetch_line_ip('telecom', details=True) if cfg['DUAL_ROUTING_READY'] else monitor.fetch_ip(details=True)
        ip = sample['ip'] if isinstance(sample, dict) else sample
        location = sample.get('location', '') if isinstance(sample, dict) else ''
    except Exception as exc:
        detail = query_error(exc)
        monitor.logging.exception('Telecom IP query failed')
        state['count'] = 0
        state['failures'] = state.get('failures', 0) + 1
        with LOCK:
            STATUS['error'] = f'查询失败（连续 {state["failures"]} 次），保留上次成功 IP；{detail}'
        event('电信 · 检测失败', detail)
        if state['failures'] >= cfg['FAIL_THRESHOLD'] and not state.get('fault_sent'):
            state['fault_sent'] = deliver(cfg, '电信出口检测故障',
                f'连续 {state["failures"]} 次无法查询出口。\n最后成功 IP：{state.get("ip", "未知")}')
        return
    if state.get('failures', 0):
        event('电信 · 检测恢复', f'查询成功，当前出口：{ip}')
    previous = state.get('ip')
    state['failures'] = 0
    pending = monitor.step(state, ip, now)
    risk = monitor.is_risk(ip)
    with LOCK:
        STATUS.update(ip=ip, location=location, risk=risk, last_success=now, error='')
    record_ip_change(state, cfg, '电信', previous, ip, now)
    if state.get('fault_sent'):
        if deliver(cfg, '电信出口检测已恢复', f'当前出口 IPv4：{ip}'):
            state['fault_sent'] = False
    if pending:
        subject, risk, previous = pending
        if deliver(cfg, '电信 · ' + subject, f'线路：{cfg["MONITOR_NAME"]}\n当前公网出口：{ip}\n'
                   f'上次采样 IP：{previous}\n风险网段：171.222.0.0/16、171.223.0.0/16\n'
                   '按网段预警，不代表实际测速结果。'):
            state['notified_risk'] = risk
            if risk:
                state['last_risk_mail'] = now


def check_unicom(state, cfg):
    """Separate samples, confirmation, failure counters and alert timestamps."""
    now = time.time()
    with LOCK:
        STATUS['unicom'].update(last_check=now, ready=True)
    try:
        sample = monitor.fetch_line_ip('unicom', details=True)
        ip = sample['ip'] if isinstance(sample, dict) else sample
        location = sample.get('location', '') if isinstance(sample, dict) else ''
    except Exception as exc:
        detail = query_error(exc)
        monitor.logging.exception('Unicom IP query failed')
        state['count'] = 0
        state['failures'] = state.get('failures', 0) + 1
        with LOCK:
            STATUS['unicom']['error'] = f'查询失败（连续 {state["failures"]} 次），显示最后成功采样；{detail}'
        event('联通 · 检测失败', detail)
        if state['failures'] >= cfg['FAIL_THRESHOLD'] and not state.get('fault_sent'):
            state['fault_sent'] = deliver(cfg, '联通出口检测故障',
                f'连续 {state["failures"]} 次查询失败。\n最后成功 IP：{state.get("ip", "未知")}')
        return
    if state.get('failures', 0):
        event('联通 · 检测恢复', f'查询成功，当前出口：{ip}')
    previous = state.get('ip')
    risk = ipaddress.ip_address(ip) not in UNICOM_NETWORK
    state['failures'] = 0
    pending = monitor.step(state, ip, now, risk=risk)
    with LOCK:
        STATUS['unicom'].update(ip=ip, location=location, risk=risk, last_success=now, error='')
    record_ip_change(state, cfg, '联通', previous, ip, now)
    if state.get('fault_sent'):
        if deliver(cfg, '联通出口检测已恢复', f'当前公网 IPv4：{ip}'):
            state['fault_sent'] = False
    if pending and cfg['UNICOM_ALERT_OUTSIDE']:
        subject = ('持续偏离预期网段' if state.get('notified_risk') else '偏离预期网段') if risk else '已回到预期网段'
        if deliver(cfg, '联通 · ' + subject,
                   f'线路：{cfg["UNICOM_NAME"]}\n当前公网 IPv4：{ip}\n'
                   '预期网段：119.4.0.0/16\n网段变化不等同于线路故障或上行限速。'):
            state['notified_risk'] = risk
            if risk:
                state['last_risk_mail'] = now


def check_all(state, cfg):
    # Switching routing mode interrupts consecutive confirmation on both lines.
    if state.get('dual_ready', False) != cfg['DUAL_ROUTING_READY']:
        state['count'] = 0
        state.setdefault('unicom', {})['count'] = 0
    state['dual_ready'] = cfg['DUAL_ROUTING_READY']
    check(state, cfg)
    if cfg['DUAL_ROUTING_READY']:
        check_unicom(state.setdefault('unicom', {}), cfg)
    else:
        with LOCK:
            STATUS['unicom'].update(ready=False, error='等待配置双线源端口分流')
    send_ip_change_mail(state, cfg, '电信', cfg['MONITOR_NAME'])
    if cfg['DUAL_ROUTING_READY']:
        send_ip_change_mail(state['unicom'], cfg, '联通', cfg['UNICOM_NAME'])


def manual_check(state, cfg):
    event('手动检测开始', '正在查询电信' + ('、联通出口' if cfg['DUAL_ROUTING_READY'] else '出口'))
    monitor.logging.info('Manual IP check started')
    check_all(state, cfg)
    with LOCK:
        results = [('电信', dict(STATUS))]
        if cfg['DUAL_ROUTING_READY']:
            results.append(('联通', dict(STATUS['unicom'])))
    message = '；'.join(f'{name}：{s["error"] or s.get("ip") or "未获取到 IP"}' for name, s in results)
    failed = any(s['error'] for _, s in results)
    event('手动检测失败' if failed else '手动检测完成', message)
    monitor.logging.info('Manual IP check finished: %s', message)


def display_payload(now=None):
    """Compact, read-only LAN payload for a small OLED client."""
    now = time.time() if now is None else now
    with LOCK:
        cfg = dict(CONFIG)
        telecom = dict(STATUS)
        unicom = dict(STATUS['unicom'])
    def line(label, sample, enabled=True):
        ip = sample.get('ip') or '-'
        checked = sample.get('last_success')
        age = max(0, int(now - checked)) if isinstance(checked, (int, float)) else -1
        if not enabled or not checked:
            condition = 'WAIT'
        elif sample.get('error') or age > max(180, cfg['CHECK_INTERVAL'] * 3):
            condition = 'ERROR'
        elif sample.get('risk'):
            condition = 'RISK'
        else:
            condition = 'OK'
        return f'{label}|{ip}|{condition}|{age}'
    return '\n'.join((line('WAN1', telecom),
                      line('WAN2', unicom, cfg['DUAL_ROUTING_READY']),
                      f'TIME|{int(now)}')) + '\n'


def worker(state):
    deadline = time.monotonic()
    while True:
        remaining = max(0, deadline - time.monotonic())
        try:
            job = JOBS.get(timeout=remaining) if remaining else 'check'
        except queue.Empty:
            job = 'check'
        with LOCK:
            cfg = dict(CONFIG)
            STATUS['busy'] = True
        try:
            configure_worker(cfg)
            if job == 'test':
                deliver(cfg, '测试邮件', 'iKuai 出口监控的 SMTP 测试邮件。', test=True)
            else:
                if job == 'manual-check':
                    manual_check(state, cfg)
                else:
                    check_all(state, cfg)
                deadline = time.monotonic() + cfg['CHECK_INTERVAL']
        except Exception as exc:
            monitor.logging.exception('Monitor worker task failed: %s', job)
            event('程序异常', query_error(exc))
            deadline = time.monotonic() + cfg['CHECK_INTERVAL']
        finally:
            with LOCK:
                STATUS['busy'] = False
                STATUS['next_check'] = time.time() + max(0, deadline - time.monotonic())
                try:
                    atomic_write(STATE_PATH, {'state': state, 'status': STATUS})
                except OSError:
                    STATUS['error'] = '无法保存状态，请检查 /data 挂载权限'


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self, code, value, content_type='application/json; charset=utf-8'):
        data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def authenticated(self):
        expected = 'Basic ' + base64.b64encode(
            ('admin:' + os.environ['WEB_PASSWORD']).encode()).decode()
        if hmac.compare_digest(self.headers.get('Authorization', ''), expected):
            return True
        self.send_response(401)
        self.send_header('WWW-Authenticate', 'Basic realm="WAN Monitor", charset="UTF-8"')
        self.send_header('Content-Length', '0')
        self.end_headers()
        return False

    def do_GET(self):
        if self.path == '/api/display':
            device_token = os.getenv('DISPLAY_TOKEN', '')
            if len(device_token) < 16:
                self.respond(404, {'error': '未启用设备接口'})
            elif not hmac.compare_digest(self.headers.get('X-Device-Token', ''), device_token):
                self.respond(403, {'error': '设备令牌无效'})
            else:
                self.respond(200, display_payload().encode('ascii'), 'text/plain; charset=ascii')
            return
        if not self.authenticated():
            return
        if self.path in ('/', '/app.js', '/style.css'):
            name, mime = {'/': ('index.html', 'text/html; charset=utf-8'),
                          '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                          '/style.css': ('style.css', 'text/css; charset=utf-8')}[self.path]
            self.respond(200, (ROOT / 'ui' / name).read_bytes(), mime)
        elif self.path == '/api/status':
            with LOCK:
                safe = {k: v for k, v in CONFIG.items() if k != 'SMTP_PASSWORD'}
                safe['PASSWORD_SET'] = bool(CONFIG['SMTP_PASSWORD'])
                self.respond(200, dict(status=STATUS, config=safe, csrf=CSRF))
        else:
            self.respond(404, {'error': '不存在'})

    def do_POST(self):
        global CONFIG
        if not self.authenticated():
            return
        if not hmac.compare_digest(self.headers.get('X-CSRF-Token', ''), CSRF):
            self.respond(403, {'error': '请刷新页面后重试'})
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 16384:
                raise ValueError('请求大小无效')
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError('请求格式无效')
            if self.path == '/api/config':
                with LOCK:
                    updated = validate(payload, CONFIG)
                    atomic_write(CONFIG_PATH, updated)
                    CONFIG = updated
                event('配置', '监控设置已保存')
                # Apply new settings promptly; do not require container restart.
                try:
                    JOBS.put_nowait('check')
                except queue.Full:
                    pass
            elif self.path in ('/api/check', '/api/test'):
                JOBS.put_nowait('test' if self.path.endswith('test') else 'manual-check')
            else:
                self.respond(404, {'error': '不存在'})
                return
        except queue.Full:
            self.respond(429, {'error': '任务队列已满，请稍后再试'})
            return
        except (ValueError, TypeError):
            self.respond(400, {'error': '设置无效：检查必填项、整数范围及 SMTP 加密方式'})
            return
        except OSError:
            self.respond(500, {'error': '保存失败，请检查 /data 目录权限'})
            return
        self.respond(200, {'ok': True})


if __name__ == '__main__':
    if len(os.getenv('WEB_PASSWORD', '')) < 12:
        raise SystemExit('Set WEB_PASSWORD to at least 12 characters; login username: admin')
    state = setup()
    threading.Thread(target=worker, args=(state,), daemon=True).start()
    print('WAN dashboard listening on :8080; username admin', flush=True)
    ThreadingHTTPServer(('0.0.0.0', 8080), Handler).serve_forever()
