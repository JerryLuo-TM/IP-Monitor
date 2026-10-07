import unittest
import errno
import socket
from unittest.mock import patch, MagicMock
from urllib.error import URLError
import monitor


class MonitorTests(unittest.TestCase):
    def test_location_from_same_response(self):
        result = monitor.parse_ip_details('当前 IP：171.221.1.2  来自于：示例地区  电信'.encode())
        self.assertEqual(result, {'ip':'171.221.1.2', 'location':'示例地区 电信'})
        self.assertEqual(monitor.parse_ip_details(b'119.4.1.2')['location'], '')

    def test_bound_handler_passes_ipv4_source_port(self):
        handler = monitor.BoundHTTPSHandler(42003)
        with patch.object(handler, 'do_open') as opened:
            handler.https_open(object())
            self.assertEqual(opened.call_args.kwargs['source_address'], ('0.0.0.0', 42003))
            self.assertTrue(opened.call_args.kwargs['context'].check_hostname)
            self.assertIs(opened.call_args.args[0], monitor.BoundIPv4HTTPSConnection)

    def test_bound_connection_resolves_only_ipv4_and_preserves_port(self):
        candidates = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('203.0.113.8', 443))]
        with patch.object(monitor.socket, 'getaddrinfo', return_value=candidates) as resolve, \
             patch.object(monitor.socket, 'create_connection') as connect:
            monitor.create_ipv4_connection(('myip.ipip.net', 443), 15, ('0.0.0.0', 41003))
        resolve.assert_called_once_with('myip.ipip.net', 443, socket.AF_INET, socket.SOCK_STREAM)
        connect.assert_called_once_with(('203.0.113.8', 443), 15, ('0.0.0.0', 41003))

    def test_bound_connection_tries_next_ipv4_address(self):
        candidates = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('203.0.113.8', 443)),
                      (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('203.0.113.9', 443))]
        with patch.object(monitor.socket, 'getaddrinfo', return_value=candidates), \
             patch.object(monitor.socket, 'create_connection',
                          side_effect=[OSError(errno.ENETUNREACH, 'unreachable'), object()]) as connect:
            monitor.create_ipv4_connection(('myip.ipip.net', 443), 15, ('0.0.0.0', 41003))
        self.assertEqual(connect.call_count, 2)

    def test_bound_connection_preserves_port_collision_error(self):
        candidates = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('203.0.113.8', 443))]
        with patch.object(monitor.socket, 'getaddrinfo', return_value=candidates), \
             patch.object(monitor.socket, 'create_connection',
                          side_effect=OSError(errno.EADDRINUSE, 'busy')):
            with self.assertRaises(OSError) as caught:
                monitor.create_ipv4_connection(('myip.ipip.net', 443), 15, ('0.0.0.0', 41000))
        self.assertEqual(caught.exception.errno, errno.EADDRINUSE)

    def test_distinct_pools_retry_only_local_collisions(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'119.4.5.6'
        opener = MagicMock()
        opener.open.side_effect = [URLError(OSError(errno.EADDRINUSE, 'busy')), response]
        with patch.object(monitor, '_port_cursor', {'telecom': 0, 'unicom': 0}), \
             patch.object(monitor, 'build_opener', return_value=opener) as builder:
            self.assertEqual(monitor.fetch_line_ip('unicom'), '119.4.5.6')
            self.assertEqual([c.args[1].source_port for c in builder.call_args_list], [42000, 42001])
            opener.open.side_effect = URLError(ConnectionRefusedError(errno.ECONNREFUSED, 'refused'))
            with self.assertRaises(URLError):
                monitor.fetch_line_ip('telecom')
            self.assertEqual(builder.call_args.args[1].source_port, 41000)
            self.assertEqual(builder.call_count, 3)

    def test_provider_response_formats(self):
        self.assertEqual(monitor.parse_ip_response(
            '当前 IP：171.221.1.2  来自于：示例地区  电信'.encode()),
            '171.221.1.2')
        self.assertEqual(monitor.parse_ip_response(b'171.223.255.255\n'), '171.223.255.255')
        self.assertFalse(monitor.is_risk('171.221.1.2'))
        for body in [b'<html>Error 171.222.1.1</html>', b'999.222.1.1', b'::1', b'']:
            with self.assertRaises(ValueError):
                monitor.parse_ip_response(body)

    def test_network_boundaries(self):
        for ip in ['171.222.0.0', '171.222.255.255', '171.223.0.0', '171.223.255.255']:
            self.assertTrue(monitor.is_risk(ip))
        for ip in ['171.221.255.255', '171.224.0.0', '17.222.1.1']:
            self.assertFalse(monitor.is_risk(ip))

    def test_confirmation_recovery_and_mail_retry(self):
        state = {}
        self.assertIsNone(monitor.step(state, '171.222.1.1', 100))
        self.assertIsNotNone(monitor.step(state, '171.223.1.1', 200))
        # Failed delivery leaves notification pending.
        self.assertIsNotNone(monitor.step(state, '171.223.1.1', 300))
        state.update(notified_risk=True, last_risk_mail=300)
        self.assertIsNone(monitor.step(state, '171.222.1.2', 400))
        self.assertIsNone(monitor.step(state, '8.8.8.8', 500))
        self.assertEqual(monitor.step(state, '8.8.8.8', 600)[0], '已离开风险网段')

    def test_repeat_reminder(self):
        state = dict(notified_risk=True, last_risk_mail=100, candidate=True, count=2)
        self.assertIsNotNone(monitor.step(state, '171.222.1.1', 100 + monitor.REMIND))
        with patch.object(monitor, 'REMIND', 0):
            self.assertIsNone(monitor.step(state, '171.222.1.1', 999999))


if __name__ == '__main__':
    unittest.main()
