"""Safety and real parser/ingress checks for the candidate-only public preparer."""
from __future__ import annotations

import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import public_alpha as candidate


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base / 'home'
        self.home.mkdir()
        self.source = self.base / 'source'
        (self.source / 'eksmu').mkdir(parents=True)
        (self.source / 'eksmu/example.py').write_bytes(b'print("fixture")\n')
        self.manifest = self.source / 'manifest.json'
        self.manifest.write_text(json.dumps({'format': 'eksmu.deploy-source.v1', 'files': {
            'eksmu/example.py': hashlib.sha256((self.source / 'eksmu/example.py').read_bytes()).hexdigest()}}))
        self.root = self.home / 'public-alpha'
        self.env_file = self.home / '.config/eksmu/mail.env'

    def prepare(self, **kwargs):
        # A live public backend may own the real PORT; probe a free port instead.
        if 'probe_port' not in kwargs:
            with socket.socket() as free:
                free.bind(('127.0.0.1', 0))
                kwargs['probe_port'] = free.getsockname()[1]
        return candidate.prepare(self.source, self.manifest, self.root, self.home, self.env_file, **kwargs)

    def test_default_preflight_has_no_writes(self):
        before = set(self.base.rglob('*'))
        result = self.prepare()
        self.assertEqual(set(self.base.rglob('*')), before)
        self.assertFalse(result['staged'])
        self.assertFalse(result['public_site'])

    def test_stage_preserves_original_data_and_copies_only_snapshot(self):
        private = self.home / 'private-alpha'
        private.mkdir()
        sentinel = private / 'community.sqlite'
        sentinel.write_bytes(b'original private data')
        (self.source / 'private.key').write_bytes(b'unlisted private fixture')
        self.env_file.parent.mkdir(parents=True)
        self.env_file.write_bytes(b'PRIVATE_MAIL_FIXTURE=not-to-be-copied\n')
        self.env_file.chmod(0o600)
        result = self.prepare(stage=True)
        self.assertTrue(result['staged'])
        self.assertFalse(result['public_site'])
        self.assertEqual(sentinel.read_bytes(), b'original private data')
        self.assertEqual(list((self.root / 'data').iterdir()), [])
        self.assertFalse((self.root / 'app/private.key').exists())
        self.assertFalse((self.home / '.config/systemd/user').exists())
        self.assertEqual((self.root / 'app/eksmu/example.py').read_bytes(), (self.source / 'eksmu/example.py').read_bytes())
        for path in self.root.rglob('*'):
            self.assertEqual(path.stat().st_mode & 0o077, 0)
            if path.is_file():
                self.assertNotIn(b'not-to-be-copied', path.read_bytes())
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with self.assertRaises(ValueError):
            self.prepare(stage=True)
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_modified_source_fails_before_any_candidate_directory(self):
        (self.source / 'eksmu/example.py').write_bytes(b'changed')
        with self.assertRaises(ValueError):
            self.prepare(stage=True)
        self.assertFalse(self.root.exists())

    def test_candidate_symlinks_escape_and_config_injection_are_rejected(self):
        (self.home / 'redirect').symlink_to(self.base)
        for root in (self.home, self.base / 'outside', self.home / 'redirect/new',
                     self.home / 'bad;include', self.home / 'bad\n[Service]',
                     self.home / 'bad%h', self.home / 'bad$variable', self.home / 'bad"path'):
            with self.subTest(root=root), self.assertRaises(ValueError):
                candidate.new_root(root, self.home)
        for value in ('/tmp/cert;include', '/tmp/cert\nkey', '/tmp/$cert', '/tmp/../cert', '/tmp/cert with spaces'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                candidate.nginx_text(Path(value), Path('/safe/key.pem'), Path('/safe/acme'))

    def test_external_mail_file_is_private_and_never_a_symlink(self):
        self.env_file.parent.mkdir(parents=True)
        self.env_file.write_text('private fixture')
        self.env_file.chmod(0o644)
        with self.assertRaises(ValueError):
            self.prepare(stage=True)
        self.assertFalse(self.root.exists())
        self.env_file.chmod(0o600)
        self.assertTrue(self.prepare()['mail_env_present'])
        self.env_file.unlink()
        self.env_file.symlink_to(self.manifest)
        with self.assertRaises(ValueError):
            self.prepare()
        with self.assertRaises(ValueError):
            candidate.mail_file(self.root / 'mail.env', self.home, self.root, self.source)

    def test_tls_pair_and_missing_files_fail_before_writes(self):
        with self.assertRaises(ValueError):
            self.prepare(certificate=self.manifest, stage=True)
        with self.assertRaises(ValueError):
            self.prepare(certificate=self.manifest, key=self.base / 'missing.pem', stage=True)
        self.assertFalse(self.root.exists())
        record = self.prepare(stage=True)
        self.assertFalse(record['trusted_tls_verified'])
        self.assertIn('REPLACE_WITH_TRUSTED_EKSMU_CERT', (self.root / 'nginx-https.conf').read_text())

    def test_existing_service_and_occupied_backend_refuse_staging(self):
        unit = self.home / '.config/systemd/user' / candidate.SERVICE
        unit.parent.mkdir(parents=True)
        unit.write_text('existing unit')
        with self.assertRaises(ValueError):
            self.prepare(stage=True)
        self.assertEqual(unit.read_text(), 'existing unit')
        unit.unlink()
        with socket.socket() as bound:
            bound.bind(('127.0.0.1', 0))
            with self.assertRaises(OSError):
                self.prepare(stage=True, probe_port=bound.getsockname()[1])
        self.assertFalse(self.root.exists())

    @unittest.skipUnless(shutil.which('systemd-analyze'), 'systemd-analyze not installed')
    def test_generated_service_parses_without_installing_it(self):
        self.prepare(stage=True)
        interpreter = self.root / 'venv/bin/python'
        interpreter.parent.mkdir(parents=True)
        interpreter.symlink_to(sys.executable)
        result = subprocess.run(['systemd-analyze', '--user', 'verify', str(self.root / candidate.SERVICE)],
                                capture_output=True, text=True, timeout=15)
        if result.returncode and 'Failed to initialize manager' in result.stderr:
            result = subprocess.run(['systemd-analyze', 'verify', str(self.root / candidate.SERVICE)],
                                    capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        unit = (self.root / candidate.SERVICE).read_text()
        self.assertIn('--port 8780 --public-origin https://eksmu.com', unit)
        self.assertIn('EnvironmentFile=-' + str(self.env_file), unit)
        self.assertIn('CPUQuota=50%', unit)
        self.assertIn('MemoryMax=1G', unit)


def unused_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


@unittest.skipUnless(shutil.which('nginx') and shutil.which('openssl'), 'nginx/openssl not installed')
class NginxIngressTests(unittest.TestCase):
    def test_isolated_tls_ingress_overwrites_headers_and_enforces_limits(self):
        # Only temporary self-signed TLS and high loopback sockets. This does not
        # load /etc/nginx or change the production nginx/8765/user service.
        seen = []
        holding = threading.Condition()
        held_requests = []
        release = threading.Event()
        class Echo(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == '/hold':
                    with holding:
                        held_requests.append(self.client_address)
                        holding.notify_all()
                    release.wait(10)
                seen.append(self.headers)
                body = json.dumps({'headers': list(self.headers.items())}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        backend = ThreadingHTTPServer(('127.0.0.1', 0), Echo)
        worker = threading.Thread(target=backend.serve_forever, daemon=True)
        worker.start()
        try:
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                cert, key = root / 'cert.pem', root / 'key.pem'
                subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                                '-subj', '/CN=eksmu.com', '-keyout', str(key), '-out', str(cert)],
                               check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
                http_port, https_port = unused_port(), unused_port()
                acme = root / 'acme'
                challenge = acme / '.well-known/acme-challenge/token'
                challenge.parent.mkdir(parents=True)
                challenge.write_text('ACME fixture')
                (root / 'client_body').mkdir()
                (root / 'proxy_temp').mkdir()
                site = candidate.nginx_text(cert, key, acme, backend_port=backend.server_port,
                                            http_port=http_port, https_port=https_port, bind_loopback=True,
                                            static_cache=root / 'static_cache',
                                            # Production uses 16/512; the same mechanism is checked at a scale
                                            # this fixture's worker_connections can hold.
                                            peer_connections=8, site_connections=32)
                config = root / 'nginx.conf'
                config.write_text('worker_processes 1;\npid ' + str(root / 'nginx.pid') + ';\n'
                                  'error_log stderr crit;\nevents { worker_connections 128; }\nhttp {\n'
                                  'set_real_ip_from 0.0.0.0/0; real_ip_header X-Forwarded-For; real_ip_recursive on;\n'
                                  'client_body_temp_path ' + str(root / 'client_body') + ';\n'
                                  'proxy_temp_path ' + str(root / 'proxy_temp') + ';\n' + site + '\n}\n')
                verify = subprocess.run(['nginx', '-t', '-c', str(config), '-p', str(root)],
                                        capture_output=True, text=True, timeout=15)
                self.assertEqual(verify.returncode, 0, verify.stdout + verify.stderr)
                process = subprocess.Popen(['nginx', '-c', str(config), '-p', str(root), '-g', 'daemon off;'],
                                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                try:
                    deadline = time.monotonic() + 5
                    while True:
                        try:
                            with socket.create_connection(('127.0.0.1', https_port), timeout=.2):
                                break
                        except OSError:
                            if time.monotonic() > deadline:
                                self.fail('isolated nginx did not start')
                            time.sleep(.05)
                    # Validation of the temporary self-signed certificate is
                    # deliberately disabled only for this local ingress test.
                    context = ssl._create_unverified_context()
                    def request(path='/', headers=None, *, method='GET', source=None):
                        connection = http.client.HTTPSConnection('127.0.0.1', https_port, context=context,
                                                                 timeout=5, source_address=source)
                        try:
                            connection.request(method, path, headers={'Host': 'eksmu.com', **(headers or {})})
                            response = connection.getresponse()
                            return response.status, response.read(), response.getheader('Location')
                        finally:
                            connection.close()

                    status, body, _ = request(headers={'X-Eksmu-Client-IP': '198.51.100.9',
                                                       'Forwarded': 'for=198.51.100.9',
                                                       'X-Forwarded-For': '198.51.100.9',
                                                       'X-Forwarded-Host': 'attacker.example',
                                                       'X-Forwarded-Proto': 'http',
                                                       'X-Eksmu-Direct-Peer-Only': '198.51.100.8'})
                    self.assertEqual(status, 200)
                    headers = dict(json.loads(body)['headers'])
                    self.assertEqual(headers['Host'], 'eksmu.com')
                    self.assertEqual(headers['X-Eksmu-Client-IP'], '127.0.0.1')
                    self.assertEqual(sum(name == 'X-Eksmu-Client-IP' for name, _ in json.loads(body)['headers']), 1)
                    for name in ('Forwarded', 'X-Forwarded-For', 'X-Forwarded-Host', 'X-Forwarded-Proto'):
                        self.assertNotIn(name, headers)
                    self.assertEqual(request(headers={'Host': 'attacker.example'})[0], 421)
                    # Account reads after each step must not consume the credential budget.
                    self.assertEqual({request('/api/auth/status')[0] for _ in range(10)}, {200})
                    statuses = [request('/api/auth/password-login', method='POST')[0] for _ in range(10)]
                    self.assertNotIn(429, statuses[:5])
                    self.assertIn(429, statuses)
                    before = len(seen)
                    self.assertEqual(request(headers={'Content-Length': str(9 * 1024 * 1024)}, method='POST')[0], 413)
                    self.assertEqual(len(seen), before)
                    # Four distinct loopback peers each hold eight active
                    # requests. A fifth peer must hit the site-wide 32 limit,
                    # rather than the per-peer limit or the auth rate limit.
                    completed, failures, clients = [], [], []
                    def hold(peer):
                        try:
                            completed.append(request('/hold', source=(peer, 0))[0])
                        except Exception as exc:
                            failures.append(str(exc))
                    try:
                        for peer in range(2, 6):
                            for _ in range(8):
                                thread = threading.Thread(target=hold, args=(f'127.0.0.{peer}',), daemon=True)
                                clients.append(thread)
                                thread.start()
                        with holding:
                            self.assertTrue(holding.wait_for(lambda: len(held_requests) == 32, timeout=4),
                                            f'Only {len(held_requests)} held requests reached the backend')
                        self.assertEqual(request('/hold', source=('127.0.0.6', 0))[0], 429)
                        self.assertEqual(request('/hold', source=('127.0.0.2', 0))[0], 429)
                    finally:
                        release.set()
                        for thread in clients:
                            thread.join(timeout=5)
                    self.assertEqual(failures, [])
                    self.assertEqual(completed, [200] * 32)
                    plain = http.client.HTTPConnection('127.0.0.1', http_port, timeout=5)
                    try:
                        plain.request('GET', '/.well-known/acme-challenge/token', headers={'Host': 'eksmu.com'})
                        response = plain.getresponse()
                        self.assertEqual((response.status, response.read()), (200, b'ACME fixture'))
                        plain.request('GET', '/paper?version=1', headers={'Host': 'eksmu.com'})
                        response = plain.getresponse()
                        self.assertEqual(response.status, 301)
                        self.assertEqual(response.getheader('Location'), 'https://eksmu.com/paper?version=1')
                        response.read()
                    finally:
                        plain.close()
                finally:
                    release.set()
                    process.terminate()
                    try:
                        process.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate(timeout=5)
        finally:
            backend.shutdown()
            backend.server_close()
            worker.join(timeout=5)


if __name__ == '__main__':
    unittest.main(verbosity=2)
