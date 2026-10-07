import base64
import json
import os
import secrets
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from http.server import ThreadingHTTPServer

import web

TEST_WEB_PASSWORD = secrets.token_urlsafe(24)
TEST_DISPLAY_TOKEN = secrets.token_urlsafe(24)
TEST_SMTP_PASSWORD = secrets.token_urlsafe(24)


class WebTests(unittest.TestCase):
    def test_display_payload_and_device_token(self):
        now = 1000
        web.CONFIG = dict(web.DEFAULTS, DUAL_ROUTING_READY=True)
        web.STATUS.update(ip='171.222.4.5', risk=True, error='', last_success=990)
        web.STATUS['unicom'].update(ip='119.4.4.5', risk=False, error='', last_success=900)
        self.assertEqual(web.display_payload(now),
                         'WAN1|171.222.4.5|RISK|10\nWAN2|119.4.4.5|OK|100\nTIME|1000\n')
        web.STATUS['unicom']['error'] = 'timeout'
        self.assertIn('WAN2|119.4.4.5|ERROR|100', web.display_payload(now))
        web.CONFIG['DUAL_ROUTING_READY'] = False
        self.assertIn('WAN2|119.4.4.5|WAIT|100', web.display_payload(now))
        with patch.dict(os.environ, WEB_PASSWORD=TEST_WEB_PASSWORD, DISPLAY_TOKEN=TEST_DISPLAY_TOKEN), \
             patch.object(web, 'CONFIG', dict(web.DEFAULTS)):
            server = ThreadingHTTPServer(('127.0.0.1', 0), web.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = f'http://127.0.0.1:{server.server_port}/api/display'
            try:
                with self.assertRaises(HTTPError) as error:
                    urlopen(url, timeout=3)
                self.assertEqual(error.exception.code, 403)
                req = Request(url, headers={'X-Device-Token':TEST_DISPLAY_TOKEN})
                with urlopen(req, timeout=3) as response:
                    self.assertEqual(response.status, 200)
                    self.assertIn('WAN1|', response.read().decode('ascii'))
            finally:
                server.shutdown()
                server.server_close()

    def test_manual_check_reports_unchanged_results_and_failure(self):
        cfg = dict(web.DEFAULTS, DUAL_ROUTING_READY=True)
        state = {}
        with patch.object(web.monitor, 'fetch_line_ip', side_effect=['171.221.1.3', '119.4.1.4'] * 2):
            web.manual_check(state, cfg)
            web.manual_check(state, cfg)
        completed = [r for r in web.STATUS['history'] if r['kind'] == '手动检测完成']
        self.assertEqual(len(completed), 2)
        self.assertIn('171.221.1.3', completed[0]['message'])
        self.assertIn('119.4.1.4', completed[0]['message'])
        with patch.object(web.monitor, 'fetch_line_ip', side_effect=[TimeoutError('timed out'), '119.4.1.4']):
            web.manual_check(state, cfg)
        self.assertEqual(web.STATUS['history'][0]['kind'], '手动检测失败')
        self.assertIn('TimeoutError: timed out', web.STATUS['history'][0]['message'])

    def test_query_error_is_visible_and_recovery_recorded(self):
        from urllib.error import URLError
        cfg = dict(web.DEFAULTS, DUAL_ROUTING_READY=True)
        state = {}
        for line, check in [('电信', web.check), ('联通', web.check_unicom)]:
            with patch.object(web.monitor, 'fetch_line_ip', side_effect=URLError(TimeoutError('timed out'))):
                check(state, cfg)
            row = web.STATUS['history'][0]
            self.assertEqual(row['kind'], line + ' · 检测失败')
            self.assertIn('TimeoutError: timed out', row['message'])
            with patch.object(web.monitor, 'fetch_line_ip', return_value='119.4.8.9'):
                check(state, cfg)
            self.assertTrue(any(r['kind'] == line + ' · 检测恢复' for r in web.STATUS['history']))

    def test_locations_stay_with_their_line_and_clear_on_missing_data(self):
        cfg = dict(web.DEFAULTS, DUAL_ROUTING_READY=True)
        samples = [{'ip':'171.221.1.2','location':'示例地区 电信'},
                   {'ip':'119.4.8.9','location':'联通归属地样本'}]
        with patch.object(web.monitor, 'fetch_line_ip', side_effect=samples):
            web.check_all({}, cfg)
        self.assertEqual(web.STATUS['location'], samples[0]['location'])
        self.assertEqual(web.STATUS['unicom']['location'], samples[1]['location'])
        with patch.object(web.monitor, 'fetch_line_ip', side_effect=TimeoutError):
            web.check({}, cfg)
        self.assertEqual(web.STATUS['location'], samples[0]['location'])
        with patch.object(web.monitor, 'fetch_line_ip', return_value={'ip':'171.221.1.2','location':''}):
            web.check({}, cfg)
        self.assertEqual(web.STATUS['location'], '')

    def setUp(self):
        web.CONFIG = dict(web.DEFAULTS)
        web.STATUS.update(ip=None, risk=None, error='', history=[], mail_status='尚未发送')
        web.STATUS['unicom'] = web.unicom_status()

    def test_legacy_mode_does_not_query_unicom(self):
        with patch.object(web.monitor, 'fetch_ip', return_value='171.221.1.2'), \
             patch.object(web.monitor, 'fetch_line_ip') as bound:
            web.check_all({}, dict(web.DEFAULTS))
        bound.assert_not_called()
        self.assertFalse(web.STATUS['unicom']['ready'])
        self.assertIsNone(web.STATUS['unicom']['ip'])

    def test_dual_line_failure_isolation(self):
        cfg = dict(web.DEFAULTS, DUAL_ROUTING_READY=True)
        state = {}
        with patch.object(web.monitor, 'fetch_line_ip', side_effect=[TimeoutError(), '119.4.8.9']) as query:
            web.check_all(state, cfg)
        self.assertEqual([c.args[0] for c in query.call_args_list], ['telecom', 'unicom'])
        self.assertTrue(web.STATUS['error'])
        self.assertFalse(web.STATUS['unicom']['risk'])
        self.assertEqual(web.STATUS['unicom']['ip'], '119.4.8.9')
        self.assertEqual(state['failures'], 1)
        self.assertEqual(state['unicom']['failures'], 0)

    def test_unicom_expected_range_and_independent_alerts(self):
        cfg = dict(web.DEFAULTS, DUAL_ROUTING_READY=True, MAIL_ENABLED=True, UNICOM_ALERT_OUTSIDE=True)
        state = {}
        with patch.object(web.monitor, 'fetch_line_ip', side_effect=['171.222.1.1', '119.5.1.1'] * 2), \
             patch.object(web.monitor, 'send_mail') as send:
            web.check_all(state, cfg)
            send.assert_not_called()
            web.check_all(state, cfg)
            self.assertEqual(send.call_count, 2)
        self.assertTrue(state['notified_risk'])
        self.assertTrue(state['unicom']['notified_risk'])
        with patch.object(web.monitor, 'fetch_line_ip', return_value='119.4.255.255'), \
             patch.object(web.monitor, 'send_mail') as send:
            web.check_unicom(state['unicom'], cfg)
            web.check_unicom(state['unicom'], cfg)
            send.assert_called_once()
            self.assertIn('联通', send.call_args.args[0])
        self.assertFalse(state['unicom']['notified_risk'])
        self.assertTrue(state['notified_risk'])

    def test_both_lines_email_on_each_ip_change(self):
        cfg = dict(web.DEFAULTS, DUAL_ROUTING_READY=True, MAIL_ENABLED=True)
        state = {}
        samples = ['171.221.1.1', '119.4.1.1',
                   '171.221.1.2', '119.4.1.2',
                   '171.221.1.2', '119.4.1.2']
        with patch.object(web.monitor, 'fetch_line_ip', side_effect=samples), \
             patch.object(web.monitor, 'send_mail') as send:
            web.check_all(state, cfg)
            send.assert_not_called()  # The first successful sample is the baseline.
            web.check_all(state, cfg)
            self.assertEqual(send.call_count, 2)
            self.assertIn('171.221.1.1', send.call_args_list[0].args[1])
            self.assertIn('171.221.1.2', send.call_args_list[0].args[1])
            self.assertIn('119.4.1.1', send.call_args_list[1].args[1])
            self.assertIn('119.4.1.2', send.call_args_list[1].args[1])
            web.check_all(state, cfg)
            self.assertEqual(send.call_count, 2)
        self.assertEqual(state['pending_ip_changes'], [])
        self.assertEqual(state['unicom']['pending_ip_changes'], [])

    def test_failed_change_email_retries_without_losing_transitions(self):
        cfg = dict(web.DEFAULTS, MAIL_ENABLED=True)
        state = {}
        with patch.object(web.monitor, 'fetch_ip', side_effect=['171.221.1.1', '171.221.1.2',
                                                                '171.221.1.3', '171.221.1.3']), \
             patch.object(web.monitor, 'send_mail', side_effect=[OSError(), OSError(), None, None]) as send:
            web.check_all(state, cfg)
            web.check_all(state, cfg)
            web.check_all(state, cfg)
            self.assertEqual(len(state['pending_ip_changes']), 2)
            web.check_all(state, cfg)
            self.assertEqual(send.call_count, 4)
        self.assertEqual(state['pending_ip_changes'], [])
        self.assertTrue(any(row['kind'] == '电信 · IP 变化' and
                            row['message'] == '171.221.1.1 → 171.221.1.2'
                            for row in web.STATUS['history']))

    def test_unicom_outside_alert_is_optional(self):
        cfg = dict(web.DEFAULTS, DUAL_ROUTING_READY=True, MAIL_ENABLED=True)
        state = {}
        with patch.object(web.monitor, 'fetch_line_ip', return_value='119.5.0.1'), \
             patch.object(web.monitor, 'send_mail') as send:
            web.check_unicom(state, cfg)
            web.check_unicom(state, cfg)
            send.assert_not_called()
        self.assertTrue(web.STATUS['unicom']['risk'])

    def test_old_config_and_state_migrate_without_losing_alerts(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(web, 'CONFIG_PATH', Path(folder) / 'config.json'), \
             patch.object(web, 'STATE_PATH', Path(folder) / 'state.json'):
            web.CONFIG_PATH.write_text(json.dumps({'SMTP_PASSWORD':TEST_SMTP_PASSWORD, 'MONITOR_NAME':'old-name'}))
            web.STATE_PATH.write_text(json.dumps({'state': {'ip':'171.221.1.1',
                'pending_ip_changes':[{'previous':'171.221.1.0', 'current':'171.221.1.1', 'time':123}],
                'notified_risk':True, 'last_risk_mail':123},
                'status':{'ip':'171.222.1.1', 'history':[{'kind':'IP 变化', 'message':'old', 'time':1}]}}))
            state = web.setup()
            self.assertEqual(web.CONFIG['SMTP_PASSWORD'], TEST_SMTP_PASSWORD)
            self.assertTrue(state['notified_risk'])
            self.assertEqual(state['last_risk_mail'], 123)
            self.assertEqual(state['ip'], '171.221.1.1')
            self.assertEqual(len(state['pending_ip_changes']), 1)
            self.assertEqual(web.STATUS['history'][0]['message'], 'old')
            self.assertFalse(web.CONFIG['DUAL_ROUTING_READY'])
            self.assertEqual(state['unicom']['count'], 0)

    def test_config_and_password_preservation(self):
        old = dict(web.DEFAULTS, SMTP_PASSWORD=TEST_SMTP_PASSWORD)
        result = web.validate({'SMTP_PASSWORD': '', 'CHECK_INTERVAL': 20}, old)
        self.assertEqual(result['SMTP_PASSWORD'], TEST_SMTP_PASSWORD)
        self.assertEqual(result['CHECK_INTERVAL'], 20)
        for payload in [{'CHECK_INTERVAL': 0}, {'CHECK_INTERVAL': 15.5},
                        {'SMTP_SECURITY': 'plain'}, {'MAIL_ENABLED': True},
                        {'SMTP_FROM': 'x\r\nBcc: other@example.com'}]:
            with self.assertRaises(ValueError):
                web.validate(payload, old)

    def test_failed_query_cannot_be_recovery(self):
        state = dict(ip='171.222.1.1', notified_risk=True, count=2)
        with patch.object(web.monitor, 'fetch_ip', side_effect=TimeoutError), \
             patch.object(web.monitor, 'send_mail') as send:
            web.check(state, dict(web.DEFAULTS))
        self.assertTrue(state['notified_risk'])
        self.assertEqual(state['count'], 0)
        self.assertTrue(web.STATUS['error'])
        send.assert_not_called()

    def test_retry_then_recovery(self):
        cfg = dict(web.DEFAULTS, MAIL_ENABLED=True)
        state = {}
        with patch.object(web.monitor, 'fetch_ip', return_value='171.222.1.1'), \
             patch.object(web.monitor, 'send_mail', side_effect=OSError):
            web.check(state, cfg)
            web.check(state, cfg)
        self.assertFalse(state.get('notified_risk', False))
        with patch.object(web.monitor, 'fetch_ip', return_value='171.222.1.1'), \
             patch.object(web.monitor, 'send_mail') as send:
            web.check(state, cfg)
            send.assert_called_once()
        self.assertTrue(state['notified_risk'])
        with patch.object(web.monitor, 'fetch_ip', return_value='8.8.8.8'), \
             patch.object(web.monitor, 'send_mail') as send:
            web.check(state, cfg)
            send.assert_not_called()
            web.check(state, cfg)
            send.assert_called_once()
        self.assertFalse(state['notified_risk'])

    def test_http_auth_csrf_secrets_and_storage(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.dict(os.environ, WEB_PASSWORD=TEST_WEB_PASSWORD), \
             patch.object(web, 'CONFIG_PATH', Path(folder) / 'config.json'):
            server = ThreadingHTTPServer(('127.0.0.1', 0), web.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            root = f'http://127.0.0.1:{server.server_port}'
            auth = 'Basic ' + base64.b64encode(('admin:' + TEST_WEB_PASSWORD).encode()).decode()
            def request(path, payload=None, csrf=None, authenticated=True):
                headers = {'Authorization': auth} if authenticated else {}
                if csrf:
                    headers['X-CSRF-Token'] = csrf
                body = None if payload is None else json.dumps(payload).encode()
                return urlopen(Request(root + path, data=body, headers=headers), timeout=3)
            try:
                with self.assertRaises(HTTPError) as exc:
                    request('/api/status', authenticated=False)
                self.assertEqual(exc.exception.code, 401)
                with self.assertRaises(HTTPError) as exc:
                    request('/api/config', {'CHECK_INTERVAL': 45})
                self.assertEqual(exc.exception.code, 403)
                with request('/api/config', {'SMTP_PASSWORD': TEST_SMTP_PASSWORD, 'CHECK_INTERVAL': 45}, web.CSRF) as response:
                    self.assertEqual(response.status, 200)
                with request('/api/status') as response:
                    body = response.read().decode()
                    self.assertNotIn(TEST_SMTP_PASSWORD, body)
                    self.assertNotIn('SMTP_PASSWORD', json.loads(body)['config'])
                self.assertEqual(json.loads(web.CONFIG_PATH.read_text(encoding='utf-8'))['CHECK_INTERVAL'], 45)
                with request('/api/config', {'CHECK_INTERVAL': 0}, web.CSRF) as response:
                    self.fail('Expected invalid config rejection')
            except HTTPError as exc:
                self.assertEqual(exc.code, 400)
                self.assertEqual(web.CONFIG['CHECK_INTERVAL'], 45)
            finally:
                server.shutdown()
                server.server_close()


if __name__ == '__main__':
    unittest.main()
