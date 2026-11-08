import http.client
import json
from pathlib import Path
import secrets
import tempfile
import threading
import unittest
import uuid
import os
from unittest.mock import patch

from eksmu.accounts import Accounts
from eksmu.account_recovery import reconcile_accounts
from eksmu.backup import create_backup, restore_backup
from eksmu.modern_accounts import ModernAccounts, smtp_sender_from_environment
from eksmu.server import make_server
from eksmu.store import Problem, Store


PASSWORD = 'A long private passphrase 2026'
NEW_PASSWORD = 'Another private passphrase 2026'


class ModernAccountTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / 'data').mkdir()
        self.path = self.root / 'data/community.sqlite'
        self.store = Store(self.path); self.addCleanup(self.store.close)
        self.sent = []
        self.modern = ModernAccounts(self.store, lambda address, purpose, code: self.sent.append((address, purpose, code)))

    def test_email_registration_private_contact_and_editable_public_profile(self):
        signed, token = self.modern.register('林作者', 'Author@example.org', PASSWORD, '笔记本')
        actor = signed['actor']['id']
        self.assertEqual(self.store.authenticate(token), actor)
        self.assertEqual(self.modern.profile(token)['email'], 'Author@example.org')
        self.assertNotIn('Author@example.org', json.dumps(self.store.export()))
        self.assertNotIn('Author@example.org', json.dumps(self.modern.public_profiles()))
        self.assertNotIn(PASSWORD, '\n'.join(self.store.db.iterdump()))
        work = self.store.publish(token, '自由发表')
        changed = self.modern.update_profile(token, '林某', '独立研究者', 1)
        self.assertEqual(changed['version'], 2)
        self.assertEqual(self.modern.public_profiles()[actor]['affiliation'], '独立研究者')
        self.assertEqual(work['exploration']['actor_id'], actor)
        with self.assertRaises(Problem):
            self.modern.update_profile(token, '旧页误写', '', 1)
        self.modern.update_profile(token, '林某', '', 2)
        self.assertEqual(self.modern.public_profiles()[actor]['affiliation'], '')

    def test_display_name_rejects_email_addresses_and_accepts_normal_names(self):
        msg = '公开显示名称会永久公开，不能使用邮箱地址。请填写真实姓名或笔名。'
        for bad_alias in (
            'author@example.org',
            '  AUTHOR@Example.ORG  ',
            'other.user@mail.domain.co.uk',
            '张三 <zhang@example.com>',
        ):
            with self.subTest(bad_alias=bad_alias), self.assertRaises(Problem) as ctx:
                self.modern.register(bad_alias, 'author@example.org', PASSWORD, '笔记本')
            self.assertEqual(str(ctx.exception), msg)

        for idx, good_alias in enumerate(('张三', 'J. Smith', '王五 (Wang Wu)', 'Alice_2026')):
            with self.subTest(good_alias=good_alias):
                signed, token = self.modern.register(good_alias, f'user{idx}@example.org', PASSWORD, '笔记本')
                self.assertEqual(signed['actor']['payload']['alias'], good_alias)
                for bad_profile in (f'user{idx}@example.org', f'  USER{idx}@EXAMPLE.ORG ', 'someone@other.org'):
                    with self.assertRaises(Problem) as ctx:
                        self.modern.update_profile(token, bad_profile, '', 1)
                    self.assertEqual(str(ctx.exception), msg)
                updated = self.modern.update_profile(token, 'J. R. R. Tolkien (笔名)', 'Oxford', 1)
                self.assertEqual(updated['display_name'], 'J. R. R. Tolkien (笔名)')

        # Verified-email registration (confirm_registration) also rejects email-like display names
        verified_modern = ModernAccounts(
            self.store,
            lambda address, purpose, code: self.sent.append((address, purpose, code)),
            require_verified_email=True,
        )
        verified_modern.request_registration('verified.user@example.org')
        reg_code = self.sent[-1][2]
        for bad_alias in ('verified.user@example.org', '  VERIFIED.USER@EXAMPLE.ORG ', 'another@site.io'):
            with self.subTest(confirm_bad_alias=bad_alias), self.assertRaises(Problem) as ctx:
                verified_modern.confirm_registration('verified.user@example.org', reg_code, bad_alias, PASSWORD, '桌面')
            self.assertEqual(str(ctx.exception), msg)
        confirmed, _ = verified_modern.confirm_registration(
            'verified.user@example.org', reg_code, '李四 (Li Si)', PASSWORD, '桌面'
        )
        self.assertEqual(confirmed['actor']['payload']['alias'], '李四 (Li Si)')

    def test_verify_and_reset_revokes_sessions_and_does_not_reuse_code(self):
        signed, token = self.modern.register('A', 'a@example.org', PASSWORD, '电脑')
        self.assertEqual(self.modern.request_code('reset', email='a@example.org'), {'request_received': True})
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[-1][1], 'verify')
        self.modern.verify_email(token, self.sent[-1][2])
        with self.assertRaises(Problem):
            self.modern.verify_email(token, self.sent[-1][2])
        second, second_token = self.modern.login('a@example.org', PASSWORD, '手机')
        self.assertEqual(second['actor']['id'], signed['actor']['id'])
        self.modern.request_code('reset', email='a@example.org')
        code = self.sent[-1][2]
        self.modern.reset_password('a@example.org', code, NEW_PASSWORD)
        self.assertEqual(self.sent[-1], ('a@example.org', 'notice', ''))
        for old in (token, second_token):
            with self.assertRaises(Problem):
                self.store.authenticate(old)
        with self.assertRaises(Problem):
            self.modern.reset_password('a@example.org', code, NEW_PASSWORD)
        with self.assertRaises(Problem):
            self.modern.login('a@example.org', PASSWORD, '旧密码')
        fresh, fresh_token = self.modern.login('a@example.org', NEW_PASSWORD, '新设备')
        self.assertEqual(fresh['actor']['id'], signed['actor']['id'])
        self.assertEqual(self.store.authenticate(fresh_token), signed['actor']['id'])

    def test_legacy_author_attaches_email_without_losing_work(self):
        legacy = Accounts(self.store)
        recovery = 'oir1_' + secrets.token_urlsafe(32)
        payload = {'operation_id': str(uuid.uuid4()), 'session_token': 'ois1_' + secrets.token_urlsafe(32),
                   'label': '旧设备', 'alias': '原作者', 'recovery': recovery}
        old = legacy.issue('register', payload)
        actor = old['actor']['id']
        work = self.store.publish(payload['session_token'], '原作品')
        self.modern.attach(payload['session_token'], recovery, 'old@example.org', PASSWORD)
        self.assertEqual(self.sent[-1][1], 'verify')
        self.modern.verify_email(payload['session_token'], self.sent[-1][2])
        with self.assertRaises(Problem):
            legacy.issue('login', {'operation_id': str(uuid.uuid4()),
                'session_token': 'ois1_' + secrets.token_urlsafe(32),
                'label': '旧文件尝试', 'credential': recovery})
        newer, password_token = self.modern.login('old@example.org', PASSWORD, '新设备')
        self.assertEqual(newer['actor']['id'], actor)
        rev = self.store.revise(password_token, work['exploration']['id'], work['revision']['id'], '新版', '仍是本人')
        self.assertEqual(rev['actor_id'], actor)
        with self.assertRaises(Problem):
            self.modern.attach(payload['session_token'], recovery, 'other@example.org', PASSWORD)

    def test_change_password_keeps_current_session_and_revokes_other_devices(self):
        signed, current = self.modern.register('A', 'a@example.org', PASSWORD, '电脑')
        _, other = self.modern.login('a@example.org', PASSWORD, '手机')
        with self.assertRaises(Problem):
            self.modern.change_password(current, 'incorrect current password', NEW_PASSWORD)
        self.modern.change_password(current, PASSWORD, NEW_PASSWORD)
        self.assertEqual(self.store.authenticate(current), signed['actor']['id'])
        with self.assertRaises(Problem):
            self.store.authenticate(other)
        with self.assertRaises(Problem):
            self.modern.login('a@example.org', PASSWORD, '旧密码')
        self.assertEqual(self.modern.login('a@example.org', NEW_PASSWORD, '新设备')[0]['actor']['id'], signed['actor']['id'])

    def test_restore_uses_newer_password_and_profile_state(self):
        signed, token = self.modern.register('A', 'a@example.org', PASSWORD, '设备')
        self.modern.verify_email(token, self.sent[-1][2])
        create_backup(self.path, self.root / 'backup', offline=True)
        self.modern.update_profile(token, '新名', '公开单位', 1)
        self.modern.request_code('reset', email='a@example.org')
        self.modern.reset_password('a@example.org', self.sent[-1][2], NEW_PASSWORD)
        restored = restore_backup(self.root / 'backup', self.root / 'restored')
        result = reconcile_accounts(restored, self.path, offline=True)
        self.assertTrue(result['all_sessions_revoked'])
        other = Store(restored)
        try:
            modern = ModernAccounts(other)
            with self.assertRaises(Problem):
                modern.login('a@example.org', PASSWORD, '旧密码')
            signed_again, new_token = modern.login('a@example.org', NEW_PASSWORD, '恢复后')
            self.assertEqual(signed_again['actor']['id'], signed['actor']['id'])
            self.assertEqual(modern.profile(new_token)['affiliation'], '公开单位')
            self.assertEqual(other.authenticate(new_token), signed['actor']['id'])
        finally:
            other.close()


class ModernHTTPTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'community.sqlite'
        self.sent = []
        self.server = make_server(self.path, 0, mail_sender=lambda address, purpose, code: self.sent.append((address, purpose, code)))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()

    def api(self, path, data=None, cookie=None):
        headers = {'X-Eksmu-Client': 'local-prototype', 'Content-Type': 'application/json'}
        if cookie: headers['Cookie'] = cookie
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        try:
            conn.request('GET' if data is None else 'POST', path, json.dumps(data) if data is not None else None, headers)
            response = conn.getresponse()
            return response.status, json.loads(response.read()), dict(response.getheaders())
        finally:
            conn.close()

    def test_http_registration_profile_and_private_email_boundary(self):
        code, signed, headers = self.api('/api/auth/email-register',
            {'alias': '实名作者', 'email': 'name@example.org', 'password': PASSWORD, 'label': '浏览器'})
        self.assertEqual(code, 200)
        cookie = headers['Set-Cookie'].split(';')[0]
        actor = signed['actor']['id']
        self.assertEqual(self.api('/api/me', cookie=cookie)[1]['id'], actor)
        code, changed, _ = self.api('/api/auth/profile',
            {'display_name': '真实姓名', 'affiliation': '示例研究所', 'expected_version': 1}, cookie)
        self.assertEqual(code, 200)
        self.assertEqual(changed['version'], 2)
        public = self.api('/api/profiles')[1]
        self.assertEqual(public['profiles'][actor]['affiliation'], '示例研究所')
        self.assertNotIn('name@example.org', json.dumps(public))
        self.assertEqual(self.api('/api/auth/profile', cookie=cookie)[1]['email'], 'name@example.org')
        self.assertEqual(self.api('/api/explorations', {'body': '作品'}, cookie)[0], 201)


class MailAdapterTests(unittest.TestCase):
    def test_private_credential_file_and_tls_mail_message(self):
        with tempfile.TemporaryDirectory() as directory:
            secret_file = Path(directory) / 'smtp-password'
            secret_file.write_text('synthetic-app-secret')
            secret_file.chmod(0o600)
            config = {'EKSMU_SMTP_HOST': 'mail.example.org', 'EKSMU_SMTP_FROM': 'noreply@example.org',
                      'EKSMU_SMTP_USER': 'test-user', 'EKSMU_SMTP_PASSWORD_FILE': str(secret_file)}
            with patch.dict(os.environ, config, clear=True), patch('eksmu.modern_accounts.smtplib.SMTP_SSL') as smtp:
                sender = smtp_sender_from_environment()
                sender('reader@example.org', 'verify', 'eksmu_synthetic-code')
                connection = smtp.return_value.__enter__.return_value
                connection.login.assert_called_once_with('test-user', 'synthetic-app-secret')
                message = connection.send_message.call_args.args[0]
                self.assertEqual(message['To'], 'reader@example.org')
                self.assertEqual(message['From'], 'Eksmu <noreply@example.org>')
                self.assertEqual(message['Subject'], 'Eksmu 邮箱验证')
                self.assertTrue(message['Message-ID'].endswith('@example.org>'))
                self.assertTrue(message['Date'])
                self.assertIn('eksmu_synthetic-code', message.get_content())
                self.assertNotIn('synthetic-app-secret', message.get_content())
            secret_file.chmod(0o644)
            with patch.dict(os.environ, config, clear=True), self.assertRaises(OSError):
                smtp_sender_from_environment()('reader@example.org', 'verify', 'code')


if __name__ == '__main__':
    unittest.main()
