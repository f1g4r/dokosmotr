"""Security and collector regression tests. No real container mutations."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from werkzeug.security import generate_password_hash

import app as dashboard

CID = 'a' * 64
CONTAINER = {'id': CID, 'protected': False, 'name': 'test', 'running': True}
PASSWORD = 'a-test-password-for-tests-only'


class SecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password_hash = generate_password_hash(PASSWORD)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        Path(self.temp.name, 'users.json').write_text(json.dumps({
            'admin': {'password_hash': self.password_hash, 'role': 'admin'},
            'viewer': {'password_hash': self.password_hash, 'role': 'viewer'},
        }))
        self.app = dashboard.create_app({'TESTING': True, 'DATA_DIR': self.temp.name,
                                         'DISK_PATH': self.temp.name, 'PUBLIC_ORIGIN': 'http://localhost',
                                         'READ_ONLY': False})
        self.client = self.app.test_client()
        self.state = self.app.extensions['dashboard_state']
        self.state['snapshot']['containers'] = [dict(CONTAINER)]
        for handler in dashboard.logging.getLogger(f'dokosmotr.audit.{id(self.app)}').handlers:
            self.addCleanup(handler.close)

    def csrf(self, client=None):
        return (client or self.client).get('/api/session').json['csrf']

    def login(self, username='admin'):
        response = self.client.post('/api/login', json={'username': username, 'password': PASSWORD},
                                    headers={'X-CSRF-Token': self.csrf()})
        self.assertEqual(response.status_code, 200)
        return response

    def post(self, path, **kwargs):
        return self.client.post(path, headers={'X-CSRF-Token': self.csrf()}, **kwargs)

    def test_api_requires_authentication(self):
        for endpoint in ['/api/containers', '/api/stats', '/api/snapshot', f'/api/container/{CID}/logs']:
            self.assertEqual(self.client.get(endpoint).status_code, 401)
        self.assertEqual(self.client.get('/api/debug').status_code, 404)
        self.assertEqual(self.client.get('/').status_code, 302)

    def test_csrf_required_on_login_and_actions(self):
        self.assertEqual(self.client.post('/api/login', json={}).status_code, 403)
        self.login()
        self.assertEqual(self.client.post(f'/api/container/{CID}/stop').status_code, 403)

    def test_cross_origin_is_rejected(self):
        token = self.csrf()
        r = self.client.post('/api/login', json={}, headers={'X-CSRF-Token': token, 'Origin': 'https://evil.example'})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.client.get('/api/session', headers={'Host': 'evil.example'}).status_code, 400)

    def test_cookie_flags_and_no_token_in_json(self):
        r = self.login()
        cookie = next(c for c in r.headers.getlist('Set-Cookie') if c.startswith('dokosmotr_auth='))
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Strict', cookie)
        self.assertNotIn('token', r.json)
        for key in self.state['sessions']:
            self.assertRegex(key, r'^[0-9a-f]{64}$')

    def test_secure_cookie_for_https(self):
        self.app.config['SESSION_COOKIE_SECURE'] = True
        response = self.login()
        self.assertIn('Secure;', response.headers['Set-Cookie'])
        self.assertIn('Strict-Transport-Security', response.headers)

    def test_malformed_payload(self):
        for body in [[], {'username': ['admin'], 'password': PASSWORD}, {'username': 'admin', 'password': {}}]:
            self.assertEqual(self.post('/api/login', json=body).status_code, 400)

    def test_login_rate_limit(self):
        for _ in range(5):
            self.assertEqual(self.post('/api/login', json={'username': 'admin', 'password': 'wrong'}).status_code, 401)
        self.assertEqual(self.post('/api/login', json={'username': 'admin', 'password': PASSWORD}).status_code, 429)

    def test_viewer_cannot_control(self):
        self.login('viewer')
        with patch('app.docker_command') as command:
            self.assertEqual(self.post(f'/api/container/{CID}/stop').status_code, 403)
            command.assert_not_called()

    def test_readonly_cannot_control(self):
        self.login(); self.app.config['READ_ONLY'] = True
        with patch('app.docker_command') as command:
            self.assertEqual(self.post(f'/api/container/{CID}/restart').status_code, 403)
            command.assert_not_called()

    def test_action_validation_and_protected_container(self):
        self.login()
        with patch('app.docker_command') as command:
            self.assertEqual(self.post('/api/container/--help/stop').status_code, 400)
            self.assertEqual(self.post(f'/api/container/{CID}/exec').status_code, 400)
            self.assertEqual(self.post(f'/api/container/{"b" * 64}/stop').status_code, 404)
            self.state['snapshot']['containers'][0]['protected'] = True
            self.assertEqual(self.post(f'/api/container/{CID}/stop').status_code, 403)
            command.assert_not_called()

    def test_action_uses_exact_arguments_and_is_audited(self):
        self.login()
        with patch('app.docker_command', return_value=('', '')) as command:
            self.assertEqual(self.post(f'/api/container/{CID}/restart').status_code, 200)
            command.assert_called_once_with('restart', CID, timeout=45)
        audit = Path(self.temp.name, 'audit.jsonl').read_text()
        self.assertIn('container_action', audit)
        self.assertNotIn(PASSWORD, audit)

    def test_docker_failure_returns_sanitized_error(self):
        self.login()
        with patch('app.docker_command', side_effect=dashboard.DockerError('Docker unavailable')):
            self.assertEqual(self.post(f'/api/container/{CID}/start').status_code, 502)

    def test_logs_combine_streams_validate_bounds(self):
        self.login()
        with patch('app.docker_command', return_value=('2026-01-01T00:00:01Z out\n', '2026-01-01T00:00:02Z err\n')):
            r = self.client.get(f'/api/container/{CID}/logs')
            self.assertIn('out', r.json['logs']); self.assertIn('err', r.json['logs'])
            for tail in ['0', '-1', '2001', 'abc']:
                self.assertEqual(self.client.get(f'/api/container/{CID}/logs?tail={tail}').status_code, 400)
            self.assertEqual(self.client.get('/api/container/--help/logs').status_code, 400)

    def test_logout_revokes_http_and_socket(self):
        self.login()
        sio = self.app.extensions['dashboard_socket']
        socket = sio.test_client(self.app, flask_test_client=self.client)
        self.assertTrue(socket.is_connected())
        self.assertEqual(self.post('/api/logout').status_code, 200)
        self.assertFalse(socket.is_connected())
        self.assertEqual(self.client.get('/api/snapshot').status_code, 401)

    def test_expired_socket_disconnected_before_next_broadcast(self):
        self.login()
        sio = self.app.extensions['dashboard_socket']
        socket = sio.test_client(self.app, flask_test_client=self.client)
        for entry in self.state['sessions'].values():
            entry['expires'] = time.time() - 1
        with patch('app.read_containers', return_value=([], None)):
            self.app.extensions['dashboard_sample']()
        self.assertFalse(socket.is_connected())
        self.assertEqual(self.client.get('/api/snapshot').status_code, 401)

    def test_anonymous_socket_refused(self):
        sio = self.app.extensions['dashboard_socket']
        self.assertFalse(sio.test_client(self.app, flask_test_client=self.client).is_connected())

    def test_outage_keeps_last_snapshot(self):
        with patch('app.read_containers', side_effect=dashboard.DockerError('no daemon')):
            self.app.extensions['dashboard_sample']()
        self.assertFalse(self.state['snapshot']['docker_available'])
        self.assertEqual(self.state['snapshot']['containers'][0]['id'], CID)

    def test_security_headers(self):
        response = self.client.get('/login')
        self.addCleanup(response.close)
        headers = response.headers
        self.assertIn("script-src 'self'", headers['Content-Security-Policy'])
        self.assertNotIn('unsafe-inline', headers['Content-Security-Policy'])
        self.assertEqual(headers['X-Frame-Options'], 'DENY')
        self.assertEqual(headers['Cache-Control'], 'no-store')

    def test_auto_origin_lan_login_rejects_cross_origin(self):
        self.app.config['PUBLIC_ORIGIN'] = ''
        self.app.config['TRUSTED_HOSTS'] = None
        for origin in ['http://192.168.1.50:5080', 'http://server.lan:9000', 'http://[fd00::50]:5080']:
            with self.subTest(origin=origin):
                client = self.app.test_client()
                csrf = client.get('/api/session', base_url=origin).json['csrf']
                headers = {'X-CSRF-Token': csrf, 'Origin': 'http://evil.example'}
                self.assertEqual(client.post('/api/login', base_url=origin, headers=headers, json={}).status_code, 403)
                headers['Origin'] = origin
                response = client.post('/api/login', base_url=origin, headers=headers,
                                       json={'username': 'admin', 'password': PASSWORD})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(client.get('/api/snapshot', base_url=origin).status_code, 200)

    def test_lan_http_origin_can_be_pinned(self):
        app = dashboard.create_app({'TESTING': True, 'PUBLIC_ORIGIN': 'http://192.168.1.50:5080',
                                    'DATA_DIR': self.temp.name})
        for handler in dashboard.logging.getLogger(f'dokosmotr.audit.{id(app)}').handlers:
            self.addCleanup(handler.close)
        client = app.test_client()
        response = client.get('/login', base_url='http://192.168.1.50:5080')
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(client.get('/login', base_url='http://evil.example').status_code, 400)


class CollectorTests(unittest.TestCase):
    def test_batch_stats_and_only_selected_metadata(self):
        rows = [{'ID': str(i) * 64, 'Names': f'c-{i}', 'Image': 'nginx:alpine', 'State': 'running'} for i in [1, 2, 3]]
        metrics = [{'ID': r['ID'], 'CPUPerc': '1.5%', 'MemPerc': '2%', 'MemUsage': '12MiB / 1GiB'} for r in rows]
        with patch('app.docker_command', side_effect=[('\n'.join(map(json.dumps, rows)), ''), ('', ''), ('\n'.join(map(json.dumps, metrics)), '')]) as run:
            containers, error = dashboard.read_containers()
            self.assertEqual(len(containers), 3)
            self.assertIsNone(error)
            self.assertEqual(sum(call.args[0] == 'stats' for call in run.call_args_list), 1)
            self.assertEqual(containers[0]['memory_bytes'], 12 * 1024**2)
            self.assertNotIn('env', containers[0])

    def test_command_timeout_and_output_limit(self):
        original = subprocess.Popen
        def fake(*args, **kwargs):
            return original([sys.executable, '-c', 'import time; time.sleep(5)'], **kwargs)
        with patch('app.subprocess.Popen', side_effect=fake):
            with self.assertRaises(dashboard.DockerError): dashboard.docker_command('ps', timeout=.1)
        def flood(*args, **kwargs):
            return original([sys.executable, '-c', 'print("x" * 100000)'], **kwargs)
        with patch('app.subprocess.Popen', side_effect=flood):
            with self.assertRaises(dashboard.DockerError): dashboard.docker_command('logs', CID, limit=100)

    def test_memory_units(self):
        self.assertEqual(dashboard.memory_bytes('2GiB / 8GiB'), 2 * 1024**3)
        self.assertEqual(dashboard.memory_bytes('1.5MiB'), 1.5 * 1024**2)
        self.assertEqual(dashboard.memory_bytes(None), 0)


if __name__ == '__main__':
    unittest.main()
