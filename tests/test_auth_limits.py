import http.client
import json
import sqlite3
from pathlib import Path
import tempfile
import threading
import unittest

from eksmu.auth_limits import AuthLimiter
from eksmu.modern_accounts import ModernAccounts, email_address
from eksmu.server import make_server
from eksmu.store import Problem, Store


PASSWORD = 'A sufficiently long private passphrase'


class LimiterTests(unittest.TestCase):
    def test_persistent_counters_share_private_key_and_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'accounts.sqlite'
            now = [1000.0]
            first = AuthLimiter(database=path, clock=lambda: now[0])
            for _ in range(8):
                first.check('password-login', '198.51.100.24', 'Private@example.org')
            second = AuthLimiter(database=path, clock=lambda: now[0])
            with self.assertRaises(Problem) as caught:
                second.check('password-login', '198.51.100.99', 'private@example.org')
            self.assertEqual(caught.exception.status, 429)
            with sqlite3.connect(path) as db:
                dump = '\n'.join(db.iterdump())
            self.assertNotIn('Private@example.org', dump)
            self.assertNotIn('198.51.100.24', dump)
            now[0] += 901
            second.check('password-login', '198.51.100.99', 'private@example.org')

    def test_identity_cap_applies_to_unknown_accounts_and_survives_source_changes(self):
        now = [100.0]
        limiter = AuthLimiter(clock=lambda: now[0])
        for n in range(8):
            limiter.check('password-login', f'client-{n}', 'Unknown@Example.org')
        with self.assertRaises(Problem) as caught:
            limiter.check('password-login', 'new-client', 'unknown@example.org')
        self.assertEqual(caught.exception.status, 429)
        self.assertNotIn('Unknown@Example.org', repr(limiter.counters))
        now[0] += 901
        limiter.check('password-login', 'new-client', 'unknown@example.org')

    def test_source_cap_stops_changing_identifiers(self):
        limiter = AuthLimiter()
        for n in range(40):
            limiter.check('email-register', 'same-client', f'unique-{n}@example.org')
        with self.assertRaises(Problem) as caught:
            limiter.check('email-register', 'same-client', 'new@example.org')
        self.assertEqual(caught.exception.status, 429)

    def test_address_policy_rejects_malformed_labels_without_rewriting_local_part(self):
        self.assertEqual(email_address('Case@EXAMPLE.org'), 'Case@example.org')
        for invalid in ('.a@example.org', 'a.@example.org', 'a@-bad.example.org',
                        'a@bad-.example.org', 'a@bad..example.org'):
            with self.subTest(invalid=invalid), self.assertRaises(Problem):
                email_address(invalid)

    def test_smtp_error_on_known_reset_does_not_reveal_account(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'accounts.sqlite')
            try:
                sent = []
                accounts = ModernAccounts(store, lambda *args: sent.append(args))
                _, token = accounts.register('A', 'known@example.org', PASSWORD, 'desk')
                accounts.verify_email(token, sent[-1][2])

                def unavailable(*_):
                    raise OSError('synthetic SMTP outage')

                accounts = ModernAccounts(store, unavailable)
                expected = {'request_received': True}
                self.assertEqual(accounts.request_code('reset', email='known@example.org'), expected)
                self.assertEqual(accounts.request_code('reset', email='absent@example.org'), expected)
            finally:
                store.close()


class AccountHTTPThrottlingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.sent = []
        self.server = make_server(Path(directory.name) / 'community.sqlite', 0,
                                  mail_sender=lambda address, purpose, code: self.sent.append((address, purpose, code)))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def api(self, path, data, cookie=None):
        headers = {'X-Eksmu-Client': 'local-prototype', 'Content-Type': 'application/json'}
        if cookie:
            headers['Cookie'] = cookie
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        try:
            conn.request('POST', path, json.dumps(data), headers)
            response = conn.getresponse()
            return response.status, json.loads(response.read()), dict(response.getheaders())
        finally:
            conn.close()

    def test_unknown_and_known_reset_requests_have_same_response_on_cooldown(self):
        code, _, headers = self.api('/api/auth/email-register',
            {'alias': 'A', 'email': 'known@example.org', 'password': PASSWORD, 'label': 'desk'})
        self.assertEqual(code, 200)
        cookie = headers['Set-Cookie'].split(';')[0]
        verification = self.sent[-1][2]
        self.assertEqual(self.api('/api/auth/verify-email', {'code': verification}, cookie)[0], 200)
        expected = {'request_received': True}
        self.assertEqual(self.api('/api/auth/request-reset', {'email': 'known@example.org'})[:2], (200, expected))
        self.assertEqual(self.api('/api/auth/request-reset', {'email': 'known@example.org'})[:2], (200, expected))
        self.assertEqual(self.api('/api/auth/request-reset', {'email': 'absent@example.org'})[:2], (200, expected))
        self.assertEqual(sum(purpose == 'reset' for _, purpose, _ in self.sent), 1)

    def test_login_unknown_account_is_throttled_before_unbounded_slow_hashes(self):
        wrong = {'email': 'absent@example.org', 'password': PASSWORD, 'label': 'desk'}
        for _ in range(8):
            status, body, _ = self.api('/api/auth/password-login', wrong)
            self.assertEqual(status, 401)
            self.assertEqual(body['error'], '邮箱或密码不正确')
        status, body, _ = self.api('/api/auth/password-login', wrong)
        self.assertEqual(status, 429)
        self.assertNotIn('absent@example.org', json.dumps(body))

    def test_verification_code_guesses_are_throttled(self):
        status, _, headers = self.api('/api/auth/email-register',
            {'alias': 'A', 'email': 'guess@example.org', 'password': PASSWORD, 'label': 'desk'})
        self.assertEqual(status, 200)
        cookie = headers['Set-Cookie'].split(';')[0]
        for _ in range(8):
            self.assertEqual(self.api('/api/auth/verify-email', {'code': 'invalid'}, cookie)[0], 400)
        self.assertEqual(self.api('/api/auth/verify-email', {'code': self.sent[-1][2]}, cookie)[0], 429)


if __name__ == '__main__':
    unittest.main()
