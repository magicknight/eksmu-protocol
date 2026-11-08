"""Production-mode account boundaries, using only isolated synthetic data/mail."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from eksmu.account_recovery import reconcile_accounts
from eksmu.auth_limits import AuthLimiter
from eksmu.backup import create_backup, restore_backup, verify_backup
from eksmu.modern_accounts import ModernAccounts, MailDispatcher, MAX_PENDING_MAIL
from eksmu.server import make_server
from eksmu.store import Store
from eksmu.store import Problem


PASSWORD = 'Synthetic public account passphrase 2026'
NEW_PASSWORD = 'New synthetic public account passphrase 2026'
ORIGIN = 'https://alpha.example.test'


class PublicAccountHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'community.sqlite'
        self.sent = []
        self.start()
        self.addCleanup(self.stop)

    def start(self):
        self.server = make_server(self.path, 0, public_origin=ORIGIN,
            mail_sender=lambda *args: self.sent.append(args))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def api(self, path, payload=None, cookie=None, **extra):
        headers = {'Host': 'alpha.example.test', 'Origin': ORIGIN,
            'X-Eksmu-Client': 'local-prototype', 'X-Eksmu-Client-IP': '198.51.100.20',
            'Content-Type': 'application/json'}
        if cookie:
            headers['Cookie'] = cookie
        for key, value in extra.items():
            if value is None:
                headers.pop(key, None)
            else:
                headers[key] = value
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        try:
            connection.request('POST' if payload is not None else 'GET', path,
                json.dumps(payload) if payload is not None else None, headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read()), dict(response.getheaders())
        finally:
            connection.close()

    def register(self, email='author@example.org'):
        response = self.api('/api/auth/email-register', {'email': email, 'label': '测试浏览器'})
        self.server._mail_dispatcher.queue.join()
        return response

    def confirmation(self, email, code, password=PASSWORD):
        return {'email': email, 'code': code, 'alias': '自由作者',
                'password': password, 'label': '测试浏览器'}

    def confirm(self, email='author@example.org'):
        self.register(email)
        response = self.api('/api/auth/confirm-registration', self.confirmation(email, self.sent[-1][2]))
        self.assertEqual(response[0], 200)
        return response[1], response[2]['Set-Cookie'].split(';')[0]

    def test_verification_creates_account_and_secure_session_then_allows_publication(self):
        status, pending, headers = self.register()
        self.assertEqual((status, pending), (200, {'request_received': True, 'verification_required': True}))
        self.assertNotIn('Set-Cookie', headers)
        self.assertEqual(self.api('/api/state')[1]['records'], [])
        self.assertEqual(self.api('/api/auth/password-login', {
            'email': 'author@example.org', 'password': PASSWORD, 'label': 'before'})[0], 401)
        code = self.sent[-1][2]
        self.assertEqual(self.sent[-1][1], 'register')
        self.assertEqual(self.api('/api/auth/confirm-registration',
            self.confirmation('author@example.org', 'invalid'))[0], 400)
        status, account, headers = self.api('/api/auth/confirm-registration',
            self.confirmation('author@example.org', code))
        self.assertEqual(status, 200)
        self.assertTrue(account['email_verified'])
        self.assertIn('; Secure', headers['Set-Cookie'])
        self.assertIn('; HttpOnly', headers['Set-Cookie'])
        self.assertIn('SameSite=Strict', headers['Set-Cookie'])
        cookie = headers['Set-Cookie'].split(';')[0]
        self.assertEqual(self.api('/api/explorations', {'body': '首篇合成研究'}, cookie)[0], 201)
        self.assertEqual(self.api('/api/auth/confirm-registration',
            self.confirmation('author@example.org', code))[0], 400)
        public = json.dumps(self.api('/api/export')[1], ensure_ascii=False)
        self.assertNotIn('author@example.org', public)
        self.assertNotIn(code, public)
        status, logged, _ = self.api('/api/auth/password-login', {
            'email': 'author@example.org', 'password': PASSWORD, 'label': 'second'})
        self.assertEqual(status, 200)
        self.assertEqual(logged['actor']['id'], account['actor']['id'])

    def test_registered_unknown_and_cooldown_registrations_have_same_response(self):
        _, cookie = self.confirm('known@example.org')
        previous = len(self.sent)
        existing = self.register('known@example.org')
        unknown = self.register('new@example.org')
        cooldown = self.register('new@example.org')
        self.assertEqual(existing[:2], unknown[:2])
        self.assertEqual(existing[:2], cooldown[:2])
        for response in (existing, unknown, cooldown):
            self.assertNotIn('Set-Cookie', response[2])
        self.assertEqual(len(self.sent), previous + 1)
        self.assertEqual(self.api('/api/auth/profile', cookie=cookie)[1]['email'], 'known@example.org')

    def test_expired_confirmation_never_creates_account(self):
        self.register()
        with patch('eksmu.modern_accounts.time.time', return_value=4_000_000_000):
            status = self.api('/api/auth/confirm-registration',
                self.confirmation('author@example.org', self.sent[-1][2]))[0]
        self.assertEqual(status, 400)
        self.assertEqual(self.api('/api/state')[1]['records'], [])

    def test_unverified_prerequest_cannot_choose_email_owners_password_or_profile(self):
        attacker_password = 'An attacker chosen password 2026'
        accepted = self.api('/api/auth/email-register', {'email': 'owner@example.org',
            'alias': '攻击者预置姓名', 'password': attacker_password, 'label': 'attacker'})
        self.assertEqual(accepted[0], 200)
        self.server._mail_dispatcher.queue.join()
        original_code = self.sent[-1][2]
        self.assertEqual(self.register('owner@example.org')[:2], accepted[:2])
        self.assertEqual(len(self.sent), 1)
        store = Store(self.path)
        try:
            dump = '\n'.join(store.db.iterdump())
            self.assertNotIn('攻击者预置姓名', dump)
            self.assertNotIn('attacker', dump)
            self.assertNotIn(attacker_password, dump)
            self.assertEqual([row[1] for row in store.db.execute('PRAGMA table_info(auth_pending_registrations)')],
                             ['email', 'token_hash', 'expires'])
        finally:
            store.close()
        status, owner, _ = self.api('/api/auth/confirm-registration',
            self.confirmation('owner@example.org', original_code, NEW_PASSWORD))
        self.assertEqual(status, 200)
        self.assertEqual(owner['actor']['payload']['alias'], '自由作者')
        for password, expected in ((attacker_password, 401), (NEW_PASSWORD, 200)):
            with self.subTest(password_kind='attacker' if expected == 401 else 'owner'):
                self.assertEqual(self.api('/api/auth/password-login', {
                    'email': 'owner@example.org', 'password': password, 'label': 'login'})[0], expected)

    def test_mail_and_errors_follow_the_requesting_page_language(self):
        from eksmu import i18n
        seen = []
        original = self.server._mail_dispatcher.sender
        self.server._mail_dispatcher.sender = lambda *args: (seen.append(i18n.current()), original(*args))
        self.api('/api/auth/email-register', {'email': 'en@example.org', 'label': 'browser'}, **{'X-Eksmu-Lang': 'en'})
        self.api('/api/auth/email-register', {'email': 'zh@example.org', 'label': 'browser'})
        self.server._mail_dispatcher.queue.join()
        self.assertEqual(seen, ['en', 'zh'])
        login = {'email': 'nobody@example.org', 'password': 'not the password 1', 'label': 'x'}
        for lang, message in (('en', 'Incorrect email or password'), (None, '邮箱或密码不正确'), ('fr', '邮箱或密码不正确')):
            with self.subTest(lang=lang):
                status, body, _ = self.api('/api/auth/password-login', login, **{'X-Eksmu-Lang': lang})
                self.assertEqual((status, body['error']), (401, message))

    def test_old_registration_routes_cannot_bypass_verification(self):
        for route in ('/api/actors', '/api/auth/register', '/api/auth/login', '/api/auth/rotate'):
            with self.subTest(route=route):
                self.assertEqual(self.api(route, {})[0], 403)
        for action in ('register', 'login', 'rotate', 'email-register', 'confirm-registration'):
            with self.subTest(nested_action=action):
                self.assertEqual(self.api('/api/auth/nested/' + action, {})[0], 404)
        self.assertEqual(self.api('/api/state')[1]['records'], [])

    def test_revoked_cookie_status_and_logout_clear_session_and_writes_stay_denied(self):
        owner, cookie = self.confirm()
        self.api('/api/auth/request-reset', {'email': 'author@example.org'})
        self.server._mail_dispatcher.queue.join()
        reset_code = self.sent[-1][2]
        self.assertEqual(self.api('/api/auth/reset-password', {'email': 'author@example.org',
            'code': reset_code, 'new_password': NEW_PASSWORD})[0], 200)
        self.assertEqual(self.api('/api/explorations', {'body': '撤销后不能发表'}, cookie)[0], 401)
        status, account, headers = self.api('/api/auth/status', cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIsNone(account['actor'])
        self.assertTrue(account['public_registration'])
        self.assertIn('Max-Age=0', headers['Set-Cookie'])
        status, result, headers = self.api('/api/auth/logout', {}, cookie,
            **{'X-Eksmu-Actor': owner['actor']['id']})
        self.assertEqual((status, result), (200, {'logged_out': True}))
        self.assertIn('Max-Age=0', headers['Set-Cookie'])
        self.assertEqual(self.api('/api/auth/logout', {}, cookie)[0], 200)

    def test_password_change_invalidates_previous_reset_code(self):
        _, cookie = self.confirm()
        self.api('/api/auth/request-reset', {'email': 'author@example.org'})
        self.server._mail_dispatcher.queue.join()
        reset_code = self.sent[-1][2]
        self.assertEqual(self.api('/api/auth/change-password', {
            'current_password': PASSWORD, 'new_password': NEW_PASSWORD}, cookie)[0], 200)
        self.assertEqual(self.api('/api/auth/reset-password', {'email': 'author@example.org',
            'code': reset_code, 'new_password': PASSWORD})[0], 400)
        self.assertEqual(self.api('/api/auth/password-login', {
            'email': 'author@example.org', 'password': NEW_PASSWORD, 'label': 'after'})[0], 200)

    def test_private_unverified_session_and_password_cannot_contribute_in_public_mode(self):
        store = Store(self.path)
        try:
            _, token = ModernAccounts(store).register('私有旧账户', 'old@example.org', PASSWORD, '旧设备')
        finally:
            store.close()
        status, result, _ = self.api('/api/auth/password-login', {
            'email': 'old@example.org', 'password': PASSWORD, 'label': 'attempt'})
        self.assertEqual((status, result['error']), (401, '邮箱或密码不正确'))
        self.assertEqual(self.api('/api/explorations', {'body': '应被拒绝'},
            **{'Authorization': 'Bearer ' + token})[0], 401)

    def test_public_auth_rejects_missing_invalid_and_ordinary_forwarded_source(self):
        payload = {'email': 'absent@example.org', 'password': PASSWORD, 'label': 'test'}
        for source in (None, 'not-an-ip', '198.51.100.20, 198.51.100.21'):
            with self.subTest(source=source):
                self.assertEqual(self.api('/api/auth/password-login', payload,
                    **{'X-Eksmu-Client-IP': source, 'X-Forwarded-For': '198.51.100.20'})[0], 403)
        self.assertEqual(self.api('/api/auth/password-login', payload,
            **{'X-Eksmu-Client-IP': '2001:db8::20'})[0], 401)

    def test_public_auth_rejects_duplicate_source_headers(self):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        payload = json.dumps({'email': 'absent@example.org', 'password': PASSWORD, 'label': 'test'})
        try:
            connection.putrequest('POST', '/api/auth/password-login', skip_host=True)
            for key, value in [('Host', 'alpha.example.test'), ('Content-Type', 'application/json'),
                               ('X-Eksmu-Client', 'local-prototype'), ('Content-Length', str(len(payload))),
                               ('X-Eksmu-Client-IP', '198.51.100.20'), ('X-Eksmu-Client-IP', '198.51.100.21')]:
                connection.putheader(key, value)
            connection.endheaders(payload.encode())
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 403)
        finally:
            connection.close()

    def test_public_login_limits_survive_server_restart_and_source_change(self):
        payload = {'email': 'absent@example.org', 'password': PASSWORD, 'label': 'test'}
        for _ in range(8):
            self.assertEqual(self.api('/api/auth/password-login', payload)[0], 401)
        self.stop()
        self.start()
        self.assertEqual(self.api('/api/auth/password-login', payload,
            **{'X-Eksmu-Client-IP': '198.51.100.99'})[0], 429)

    def test_public_status_and_health_report_registration_mode(self):
        self.assertTrue(self.api('/api/auth/status')[1]['public_registration'])
        self.assertTrue(self.api('/api/auth/status')[1]['require_verified_email'])
        health = self.api('/api/health')[1]
        self.assertEqual(health['mode'], 'public-alpha')
        self.assertTrue(health['email_recovery_available'])


class PublicAccountBackupTests(unittest.TestCase):
    def test_full_backup_keeps_limits_but_restored_registration_codes_are_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'data').mkdir()
            path = root / 'data/community.sqlite'
            sent = []
            store = Store(path)
            try:
                accounts = ModernAccounts(store, lambda *args: sent.append(args), require_verified_email=True)
                accounts.register('A', 'pending@example.org', PASSWORD, 'desk')
                self.assertEqual(store.db.execute('SELECT COUNT(*) FROM auth_pending_registrations').fetchone()[0], 1)
                limiter = AuthLimiter(database=path)
                for _ in range(8):
                    limiter.check('password-login', 'synthetic-client', 'absent@example.org')
                create_backup(path, root / 'backup', offline=True)
                self.assertTrue(verify_backup(root / 'backup')['data_verified'])
                restored = restore_backup(root / 'backup', root / 'restored')
                reconcile_accounts(restored, path, offline=True)
                recovered = Store(restored)
                try:
                    self.assertEqual(recovered.db.execute('SELECT COUNT(*) FROM auth_pending_registrations').fetchone()[0], 0)
                finally:
                    recovered.close()
                with self.assertRaises(Problem) as caught:
                    AuthLimiter(database=restored).check('password-login', 'changed-source', 'absent@example.org')
                self.assertEqual(caught.exception.status, 429)
            finally:
                store.close()

    def test_public_registration_without_mail_cannot_create_account(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {}, clear=True):
            path = Path(directory) / 'community.sqlite'
            server = make_server(path, 0, public_origin=ORIGIN)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
            try:
                payload = {'alias': 'A', 'email': 'unverified@example.org', 'password': PASSWORD, 'label': 'test'}
                connection.request('POST', '/api/auth/email-register', json.dumps(payload), {
                    'Host': 'alpha.example.test', 'Origin': ORIGIN, 'X-Eksmu-Client': 'local-prototype',
                    'X-Eksmu-Client-IP': '198.51.100.20', 'Content-Type': 'application/json'})
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 503)
                self.assertIsNone(response.getheader('Set-Cookie'))
            finally:
                connection.close()
                server.shutdown()
                server.server_close()
                thread.join()
            store = Store(path)
            try:
                self.assertEqual(store.rows(), [])
            finally:
                store.close()


class MailDispatchTests(unittest.TestCase):
    def test_slow_mail_is_bounded_and_never_blocks_enqueue(self):
        started, release = threading.Event(), threading.Event()
        def slow_sender(*_):
            started.set()
            release.wait(5)
        dispatcher = MailDispatcher(slow_sender)
        try:
            dispatcher('synthetic@example.org', 'register', 'synthetic-code')
            self.assertTrue(started.wait(1))
            for _ in range(MAX_PENDING_MAIL):
                dispatcher('synthetic@example.org', 'register', 'synthetic-code')
            with self.assertRaises(OSError):
                dispatcher('synthetic@example.org', 'register', 'synthetic-code')
        finally:
            release.set()
            dispatcher.close()


if __name__ == '__main__':
    unittest.main()
