import ipaddress
import errno
import http.client
import json
import logging
import os
import re
from pathlib import Path
import smtplib
import socket
import ssl
import sys
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from urllib.request import HTTPSHandler, HTTPRedirectHandler, ProxyHandler, build_opener
from urllib.error import URLError


# Optional file configuration for iKuai's Docker UI; environment takes priority.
CONFIG = Path(os.getenv('CONFIG_FILE', '/app/config.json'))
if CONFIG.exists():
    for key, value in json.loads(CONFIG.read_text(encoding='utf-8-sig')).items():
        os.environ.setdefault(key, str(value))

NETWORKS = tuple(map(ipaddress.ip_network, ('171.222.0.0/16', '171.223.0.0/16')))
DEFAULT_IP_URL = 'https://myip.ipip.net'
LINE_PORTS = {'telecom': (41000, 41031), 'unicom': (42000, 42031)}
_port_cursor = dict.fromkeys(LINE_PORTS, 0)
STATE = Path(os.getenv('STATE_FILE', '/data/state.json'))
INTERVAL = int(os.getenv('CHECK_INTERVAL', '300'))
REMIND = int(os.getenv('REMIND_INTERVAL', '21600'))
CONFIRM = int(os.getenv('CONFIRM_COUNT', '2'))
FAIL_LIMIT = int(os.getenv('FAIL_THRESHOLD', '3'))
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')


def parse_ip_details(body):
    text = body.decode('utf-8-sig').strip()
    # Accept a plain IPv4 response or ipip.net's labelled text, not arbitrary
    # addresses embedded in an HTML error page.
    match = re.fullmatch(r'当前\s*IP\s*[:：]\s*([0-9.]+)(?:\s+来自于[:：]\s*(.*))?', text)
    address = ipaddress.ip_address(match.group(1) if match else text)
    if address.version != 4:
        raise ValueError('IP endpoint did not return IPv4')
    location = ' '.join((match.group(2) or '').split()) if match else ''
    return {'ip': str(address), 'location': location}


def parse_ip_response(body):
    return parse_ip_details(body)['ip']


def fetch_ip(details=False):
    # Bypass explicit HTTP proxies. Router/TUN policies still need a direct route.
    opener = build_opener(ProxyHandler({}))
    with opener.open(os.getenv('IP_URL', DEFAULT_IP_URL), timeout=15) as response:
        result = parse_ip_details(response.read(4096))
        return result if details else result['ip']


def create_ipv4_connection(address, timeout, source_address):
    """Resolve only A records while preserving the source port used by iKuai."""
    last_error = None
    for _, _, _, _, destination in socket.getaddrinfo(
            address[0], address[1], socket.AF_INET, socket.SOCK_STREAM):
        try:
            return socket.create_connection(destination, timeout, source_address)
        except OSError as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise OSError(f'No IPv4 address for {address[0]}')


class BoundIPv4HTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = create_ipv4_connection


class BoundHTTPSHandler(HTTPSHandler):
    def __init__(self, source_port):
        super().__init__(context=ssl.create_default_context())
        self.source_port = source_port

    def https_open(self, req):
        return self.do_open(BoundIPv4HTTPSConnection, req,
                            context=self._context,
                            source_address=('0.0.0.0', self.source_port))


class NoQueryRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise URLError('Query endpoint redirected; refusing an unverified route')


def fetch_line_ip(line, details=False):
    """Use distinct IPv4 source-port pools for iKuai policy routing.

    Resolve only IPv4 so IPv6 DNS answers cannot conflict with the IPv4
    source address or bypass the iKuai source-port rules.
    Rotate ports to avoid TIME_WAIT; only retry local port collisions.
    """
    first, last = LINE_PORTS[line]
    count = last - first + 1
    for _ in range(count):
        port = first + (_port_cursor[line] % count)
        _port_cursor[line] += 1
        opener = build_opener(ProxyHandler({}), BoundHTTPSHandler(port), NoQueryRedirect())
        try:
            with opener.open(DEFAULT_IP_URL, timeout=15) as response:
                result = parse_ip_details(response.read(4096))
                return result if details else result['ip']
        except (URLError, OSError) as exc:
            reason = getattr(exc, 'reason', exc)
            if getattr(reason, 'errno', None) not in (errno.EADDRINUSE, 10048):
                raise
            logging.warning('%s source port %s is busy; trying the next port', line, port)
    raise OSError(errno.EADDRINUSE, f'{line} source ports are busy')


def is_risk(ip):
    return any(ipaddress.ip_address(ip) in network for network in NETWORKS)


def send_mail(subject, body):
    message = EmailMessage()
    message['From'] = os.environ['SMTP_FROM']
    recipients = [s.strip() for s in os.environ['SMTP_TO'].split(',') if s.strip()]
    if not recipients:
        raise ValueError('SMTP_TO is empty')
    message['To'] = ', '.join(recipients)
    message['Subject'] = '[宽带出口监控] ' + subject
    message.set_content(body)
    mode = os.getenv('SMTP_SECURITY', 'ssl').lower()
    context = ssl.create_default_context()
    host, port = os.environ['SMTP_HOST'], int(os.getenv('SMTP_PORT', '465'))
    if mode == 'ssl':
        client = smtplib.SMTP_SSL(host, port, timeout=25, context=context)
    elif mode == 'starttls':
        client = smtplib.SMTP(host, port, timeout=25)
    else:
        raise ValueError('SMTP_SECURITY must be ssl or starttls')
    with client:
        if mode == 'starttls':
            client.starttls(context=context)
        client.login(os.environ['SMTP_USER'], os.environ['SMTP_PASSWORD'])
        client.send_message(message, to_addrs=recipients)


def save(state):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE.with_suffix('.tmp')
    temporary.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
    temporary.replace(STATE)


def step(state, ip, now, risk=None):
    """Update consecutive observations and return a pending notification."""
    if risk is None:
        risk = is_risk(ip)
    state['count'] = state.get('count', 0) + 1 if state.get('candidate') == risk else 1
    state['candidate'] = risk
    previous = state.get('ip', '未知')
    state['ip'] = ip
    if state['count'] < CONFIRM:
        return None
    notified = state.get('notified_risk', False)
    due = REMIND > 0 and now - state.get('last_risk_mail', 0) >= REMIND
    if risk and (not notified or due):
        return ('持续处于风险网段' if notified else '命中风险网段', risk, previous)
    if not risk and notified:
        return ('已离开风险网段', risk, previous)
    return None


def run():
    state = json.loads(STATE.read_text(encoding='utf-8')) if STATE.exists() else {}
    # A restart interrupts consecutive samples; keep delivered alert state.
    state['count'] = 0
    state['failures'] = 0
    while True:
        now = time.time()
        try:
            ip = fetch_ip()
        except Exception as exc:
            state['count'] = 0
            state['failures'] = state.get('failures', 0) + 1
            logging.warning('IP query failed (%s): %s', state['failures'], exc)
            if state['failures'] >= FAIL_LIMIT and not state.get('fault_sent'):
                try:
                    send_mail('出口 IP 检测故障',
                              f'连续查询失败 {state["failures"]} 次。\n'
                              f'最后成功 IP：{state.get("ip", "未知")}\n'
                              '无法判断当前网段，请检查网络、DNS 和查询接口。')
                    state['fault_sent'] = True
                except Exception:
                    logging.error('Fault email failed; will retry next cycle')
        else:
            state['failures'] = 0
            pending = step(state, ip, now)
            logging.info('IPv4=%s risk=%s consecutive=%s', ip, is_risk(ip), state['count'])
            if state.get('fault_sent'):
                try:
                    send_mail('出口 IP 检测已恢复', f'当前出口 IPv4：{ip}\n查询接口已恢复可用。')
                    state['fault_sent'] = False
                except Exception:
                    logging.error('Query recovery email failed; will retry')
            if pending:
                subject, risk, previous = pending
                try:
                    send_mail(subject, f'设备：{os.getenv("MONITOR_NAME", "NAS")}\n'
                              f'当前出口 IPv4：{ip}\n上次成功采样 IP：{previous}\n'
                              f'检测时间：{datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")}\n'
                              '风险网段：171.222.0.0/16、171.223.0.0/16\n'
                              '此提醒仅按网段判断，不代表实际测速结果。')
                    state['notified_risk'] = risk
                    if risk:
                        state['last_risk_mail'] = now
                    logging.info('Alert email sent')
                except Exception:
                    logging.error('Alert email failed; will retry next cycle')
        save(state)
        time.sleep(INTERVAL)


if __name__ == '__main__':
    if min(INTERVAL, CONFIRM, FAIL_LIMIT) <= 0 or REMIND < 0:
        raise ValueError('Invalid monitoring intervals or thresholds')
    if '--check-telecom' in sys.argv or '--check-unicom' in sys.argv:
        line = 'unicom' if '--check-unicom' in sys.argv else 'telecom'
        current = fetch_line_ip(line)
        print(f'line={line} IPv4={current} source_ports={LINE_PORTS[line]}')
    elif '--check' in sys.argv:
        current = fetch_ip()
        print(f'IPv4={current} risk={is_risk(current)}')
    elif '--test-email' in sys.argv:
        send_mail('测试邮件', 'SMTP 配置成功，NAS 出口 IP 监控可以发送邮件。')
        print('Test email accepted by SMTP server')
    else:
        run()
