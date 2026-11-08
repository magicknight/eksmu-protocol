import concurrent.futures
from copy import deepcopy
import hashlib
import http.client
import json
from pathlib import Path
import secrets
import sqlite3
import shutil
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from eksmu.accounts import Accounts, SESSION_SECONDS
from eksmu.account_recovery import reconcile_accounts
from eksmu.backup import create_backup, restore_backup
from eksmu.maintenance import RequestLock
from eksmu.server import make_server
from eksmu.store import Problem, Store


def key(prefix='oir1_'):
    return prefix + secrets.token_urlsafe(32)


def request(**extra):
    return {'operation_id': str(uuid.uuid4()), 'session_token': key('ois1_'), 'label': '测试设备', **extra}


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); (self.root / 'data').mkdir()
        self.path = self.root / 'data/community.sqlite'
        self.store = Store(self.path); self.addCleanup(self.store.close)
        self.account = Accounts(self.store)
        self.registration = request(alias='账户测试 A', recovery=key())
        self.a = self.account.issue('register', self.registration)
        self.token = self.registration['session_token']; self.recovery = self.registration['recovery']
        self.aid = self.a['actor']['id']

    def login(self, recovery=None):
        payload = request(credential=recovery or self.recovery)
        return self.account.issue('login', payload), payload

    def rotate(self):
        payload = request(credential=self.recovery, new_recovery=key(), actor_id=self.aid)
        return self.account.issue('rotate', payload), payload

    def test_registration_is_atomic_idempotent_and_archival_hash_is_not_recovery(self):
        self.assertEqual(self.account.issue('register', self.registration), self.a)
        self.assertEqual(len(self.store.rows()), 1)
        self.assertEqual(self.store.authenticate(self.token), self.aid)
        self.assertNotEqual(self.a['actor']['payload']['key_hash'], hashlib.sha256(self.recovery.encode()).hexdigest())
        with self.assertRaises(Problem):
            self.store.authenticate(self.recovery)

    def test_legacy_login_preserves_immutable_author_and_disables_direct_old_key(self):
        legacy = self.store.register('旧身份')
        work = self.store.publish(legacy['token'], '旧作品'); before = deepcopy(self.store.rows())
        signed, payload = self.login(legacy['token'])
        self.assertEqual(signed['actor'], legacy['actor']); self.assertEqual(self.store.rows(), before)
        with self.assertRaises(Problem):
            self.store.publish(legacy['token'], '旧密钥不能直接写')
        revision = self.store.revise(payload['session_token'], work['exploration']['id'], work['revision']['id'], '新版', '原作者仍可修订')
        self.assertEqual(revision['actor_id'], legacy['actor']['id'])

    def test_wrong_recovery_does_not_create_session_or_change_records(self):
        before = self.store.export()
        with self.assertRaises(Problem):
            self.login(key())
        self.assertEqual(len(self.account.sessions(self.token)['sessions']), 1)
        self.assertEqual(self.store.export(), before)

    def test_independent_sessions_can_be_revoked_separately(self):
        other, payload = self.login()
        self.account.revoke(self.token, session_id=other['session_id'])
        self.assertEqual(self.store.authenticate(self.token), self.aid)
        with self.assertRaises(Problem):
            self.store.authenticate(payload['session_token'])
        self.account.revoke(self.token, session_id=other['session_id'])

    def test_logout_is_idempotent_and_old_issue_cannot_revive_session(self):
        self.account.logout(self.token); self.account.logout(self.token)
        with self.assertRaises(Problem):
            self.account.issue('register', self.registration)
        with self.assertRaises(Problem):
            self.store.authenticate(self.token)

    def test_rotation_revokes_every_old_session_and_old_recovery(self):
        _, second = self.login(); before = self.store.export()
        result, payload = self.rotate()
        self.assertEqual(result['actor']['id'], self.aid)
        for value in (self.token, second['session_token'], self.recovery):
            with self.assertRaises(Problem):
                self.store.authenticate(value)
        with self.assertRaises(Problem):
            self.login(self.recovery)
        self.assertEqual(self.store.authenticate(payload['session_token']), self.aid)
        self.login(payload['new_recovery'])
        self.assertEqual(self.store.export(), before)

    def test_lost_rotation_response_can_be_retried_but_not_after_revocation(self):
        result, payload = self.rotate()
        self.assertEqual(self.account.issue('rotate', payload), result)
        self.account.logout(payload['session_token'])
        with self.assertRaises(Problem):
            self.account.issue('rotate', payload)

    def test_recovery_reuse_and_changed_operation_payload_are_refused(self):
        changed = {**self.registration, 'alias': 'different'}
        with self.assertRaises(Problem):
            self.account.issue('register', changed)
        with self.assertRaises(Problem):
            self.account.issue('register', request(alias='duplicate', recovery=self.recovery))
        _, rotated = self.rotate()
        with self.assertRaises(Problem):
            self.account.issue('rotate', request(credential=rotated['new_recovery'], new_recovery=self.recovery, actor_id=self.aid))

    def test_cannot_revoke_other_actor_or_rotate_with_their_credential(self):
        payload = request(alias='B', recovery=key()); b = self.account.issue('register', payload)
        with self.assertRaises(Problem):
            self.account.revoke(self.token, session_id=b['session_id'])
        with self.assertRaises(Problem):
            self.account.issue('rotate', request(credential=payload['recovery'], new_recovery=key(), actor_id=self.aid))
        self.assertEqual(self.store.authenticate(payload['session_token']), b['actor']['id'])

    def test_revoke_others_keeps_current_session(self):
        _, payload = self.login()
        self.account.revoke(self.token, others=True)
        self.assertEqual(self.store.authenticate(self.token), self.aid)
        with self.assertRaises(Problem):
            self.store.authenticate(payload['session_token'])

    def test_session_expiry_is_checked_on_server_and_retry(self):
        future = self.a['expires_at'] + 1
        with patch('eksmu.accounts.time.time', return_value=future):
            with self.assertRaises(Problem):
                self.store.authenticate(self.token)
            with self.assertRaises(Problem):
                self.account.issue('register', self.registration)
        self.assertEqual(SESSION_SECONDS, 604800)

    def test_secrets_are_not_in_database_export_or_status(self):
        raw = '\n'.join(self.store.db.iterdump()) + json.dumps(self.store.export()) + json.dumps(self.account.status(self.token))
        for value in (self.token, self.recovery):
            self.assertNotIn(value, raw)
        self.assertNotIn('auth_sessions', json.dumps(self.store.export()))

    def test_restart_preserves_session_and_revocation(self):
        _, second = self.login(); self.account.logout(self.token)
        other = Store(self.path)
        try:
            self.assertEqual(other.authenticate(second['session_token']), self.aid)
            with self.assertRaises(Problem):
                other.authenticate(self.token)
        finally:
            other.close()

    def test_malformed_or_weak_secrets_do_not_write_partial_accounts(self):
        before = self.store.export()
        for value in ('short', 'oir1_abc', 'oir1_' + '!' * 43, 'oir1_' + 'a' * 44, None):
            with self.subTest(value=value), self.assertRaises(Problem):
                self.account.issue('register', request(alias='bad', recovery=value))
        for bad_alias in ('owner@example.com', '  Admin@Sub.Domain.Org '):
            with self.subTest(bad_alias=bad_alias):
                with self.assertRaises(Problem) as ctx:
                    self.account.issue('register', request(alias=bad_alias, recovery=key()))
                self.assertIn('不能使用邮箱地址', str(ctx.exception))
                with self.assertRaises(Problem) as ctx2:
                    self.store.register(bad_alias)
                self.assertIn('不能使用邮箱地址', str(ctx2.exception))
        self.assertEqual(self.store.export(), before)

    def test_operation_limit_rolls_back_registration(self):
        before = self.store.export()
        with patch('eksmu.accounts.MAX_OPERATIONS', 1), self.assertRaises(Problem):
            self.account.issue('register', request(alias='new', recovery=key()))
        self.assertEqual(self.store.export(), before)

    def test_failure_after_actor_insert_rolls_back_every_account_table(self):
        before = '\n'.join(self.store.db.iterdump())
        with self.assertRaises(Problem):
            self.account.issue('register', request(alias='rolled back', recovery=key(), label='x' * 65))
        self.assertEqual('\n'.join(self.store.db.iterdump()), before)

    def test_concurrent_duplicate_registration_commits_once(self):
        payload = request(alias='concurrent', recovery=key())
        def run(_):
            store = Store(self.path)
            try:
                return Accounts(store).issue('register', payload)
            finally:
                store.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(run, range(4)))
        self.assertEqual(results, [results[0]] * 4)
        self.assertEqual(len(self.store.rows()), 2)

    def test_revocation_between_initial_auth_and_append_is_rechecked(self):
        original = self.store.authenticate
        calls = 0
        def revoke_after_first(value):
            nonlocal calls
            result = original(value); calls += 1
            if calls == 1:
                other = Store(self.path)
                try:
                    Accounts(other).logout(value)
                finally:
                    other.close()
            return result
        with patch.object(self.store, 'authenticate', side_effect=revoke_after_first), self.assertRaises(Problem):
            self.store.publish(self.token, 'must not appear')
        self.assertEqual(len(self.store.rows()), 1)

    def test_missing_account_table_fails_closed(self):
        self.store.db.execute('DROP TABLE auth_sessions'); self.store.db.commit()
        with self.assertRaises(Problem):
            self.store.authenticate(self.recovery)
        with self.assertRaises(Problem):
            Accounts(self.store)

    def test_restored_backup_blocks_sessions_and_both_old_and_new_recovery_until_reconciled(self):
        work = self.store.publish(self.token, '恢复前作品')
        create_backup(self.path, self.root / 'backup', offline=True)
        _, rotated = self.rotate()
        restored = restore_backup(self.root / 'backup', self.root / 'restored')
        other = Store(restored)
        try:
            account = Accounts(other)
            for value in (self.token, rotated['session_token']):
                with self.assertRaises(Problem):
                    other.authenticate(value)
            for credential in (self.recovery, rotated['new_recovery']):
                with self.assertRaises(Problem):
                    account.issue('login', request(credential=credential))
            result = reconcile_accounts(restored, self.path, offline=True)
            self.assertTrue(result['all_sessions_revoked'])
            with self.assertRaises(Problem):
                account.issue('login', request(credential=self.recovery))
            fresh = request(credential=rotated['new_recovery']); signed = account.issue('login', fresh)
            self.assertEqual(signed['actor']['id'], self.aid)
            revision = other.revise(fresh['session_token'], work['exploration']['id'], work['revision']['id'], '恢复后', '仍为原作者')
            self.assertEqual(revision['actor_id'], self.aid)
            for value in (self.token, rotated['session_token']):
                with self.assertRaises(Problem):
                    other.authenticate(value)
        finally:
            other.close()

    def test_restore_reconciliation_refuses_wrong_instance_and_live_source(self):
        create_backup(self.path, self.root / 'backup', offline=True)
        restored = restore_backup(self.root / 'backup', self.root / 'restored')
        foreign = Store(self.root / 'foreign.sqlite')
        try:
            Accounts(foreign)
            with self.assertRaises(ValueError):
                reconcile_accounts(restored, foreign.path, offline=True)
        finally:
            foreign.close()
        server = make_server(self.path, 0)
        try:
            with self.assertRaises(ValueError):
                reconcile_accounts(restored, self.path, offline=True)
        finally:
            server.server_close()

    def test_reconciliation_refuses_older_credential_generation_atomically(self):
        older = self.root / 'older.sqlite'; shutil.copyfile(self.path, older)
        self.rotate()
        create_backup(self.path, self.root / 'backup', offline=True)
        restored = restore_backup(self.root / 'backup', self.root / 'restored')
        before = restored.read_bytes()
        with self.assertRaisesRegex(ValueError, 'older or conflicting'):
            reconcile_accounts(restored, older, offline=True)
        self.assertEqual(restored.read_bytes(), before)

    def test_upgrading_pre_account_restored_instance_stays_blocked(self):
        restored = self.root / 'old-restore'; restored.mkdir()
        (restored / 'restore-record.json').write_text('{}')
        legacy_store = Store(restored / 'community.sqlite')
        try:
            legacy_store.import_bundle(self.store.export())
            account = Accounts(legacy_store)
            self.assertTrue(account.info()['recovery_review_required'])
            result = reconcile_accounts(legacy_store.path, self.path, offline=True)
            self.assertTrue(result['all_sessions_revoked'])
            fresh = request(credential=self.recovery)
            self.assertEqual(account.issue('login', fresh)['actor']['id'], self.aid)
        finally:
            legacy_store.close()

    def test_wrong_restore_target_is_rejected_without_database_initialization(self):
        unrelated = self.root / 'unrelated.sqlite'
        with sqlite3.connect(unrelated) as db:
            db.execute('CREATE TABLE unrelated(value TEXT)')
            db.execute("INSERT INTO unrelated VALUES('keep')")
        before = unrelated.read_bytes()
        with self.assertRaisesRegex(ValueError, 'upgraded restored'):
            reconcile_accounts(unrelated, self.path, offline=True)
        self.assertEqual(unrelated.read_bytes(), before)


class AccountHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'community.sqlite'
        self.server = make_server(self.db, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.addCleanup(self.stop)
        self.payload = request(alias='HTTP A', recovery=key())
        code, self.a, self.headers = self.api('/api/auth/register', self.payload)
        self.assertEqual(code, 200)
        self.cookie = self.headers['Set-Cookie'].split(';')[0]

    def stop(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()

    def api(self, path, data=None, *, token=None, cookie=None, extra=None):
        headers = {'Content-Type': 'application/json', 'X-Eksmu-Client': 'local-prototype'}
        if token: headers['Authorization'] = 'Bearer ' + token
        if cookie: headers['Cookie'] = cookie
        if extra: headers.update(extra)
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        try:
            conn.request('GET' if data is None else 'POST', path, None if data is None else json.dumps(data), headers)
            res = conn.getresponse(); raw = res.read()
            return res.status, json.loads(raw), dict(res.getheaders())
        finally:
            conn.close()

    def test_cookie_flags_and_session_are_usable_without_body_secret(self):
        for flag in ('HttpOnly', 'SameSite=Strict', 'Path=/api', 'Max-Age='):
            self.assertIn(flag, self.headers['Set-Cookie'])
        self.assertNotIn(self.payload['session_token'], json.dumps(self.a))
        self.assertEqual(self.api('/api/me', cookie=self.cookie)[1]['id'], self.a['actor']['id'])
        self.assertEqual(self.api('/api/explorations', {'body': 'cookie 发布'}, cookie=self.cookie)[0], 201)

    def test_other_identity_cannot_edit_work_or_private_sessions(self):
        work = self.api('/api/explorations', {'body': 'A作品'}, cookie=self.cookie)[1]
        payload = request(alias='HTTP B', recovery=key()); _, b, _ = self.api('/api/auth/register', payload)
        token = payload['session_token']
        self.assertEqual(self.api('/api/revisions', {'exploration_id': work['exploration']['id'], 'parent_revision_id': work['revision']['id'], 'title': 'bad', 'body': 'bad'}, token=token)[0], 403)
        self.assertEqual(self.api('/api/community/details', {'exploration_id': work['exploration']['id'], 'topics': [], 'help': 'bad', 'expected_version': 0}, token=token)[0], 403)
        self.assertEqual(self.api('/api/research/upload', {'revision_id': work['revision']['id'], 'filename': 'a.txt', 'data_b64': 'aGk='}, token=token)[0], 403)
        self.assertEqual(self.api('/api/auth/revoke', {'session_id': self.a['session_id']}, token=token)[0], 403)
        sessions = self.api('/api/auth/sessions', token=token)[1]['sessions']
        self.assertEqual([s['id'] for s in sessions], [b['session_id']])

    def test_cookie_csrf_and_ambiguous_authorization_are_rejected(self):
        self.assertEqual(self.api('/api/auth/logout', {}, cookie=self.cookie, extra={'Origin': 'https://elsewhere.invalid'})[0], 403)
        self.assertEqual(self.api('/api/auth/logout', {}, cookie=self.cookie, extra={'Sec-Fetch-Site': 'cross-site'})[0], 403)
        self.assertEqual(self.api('/api/me', cookie=self.cookie, token=self.payload['session_token'])[0], 400)
        self.assertEqual(self.api('/api/auth/status?credential=not-a-real-secret')[0], 400)

    def test_logout_clears_cookie_and_invalidates_copied_token(self):
        code, _, headers = self.api('/api/auth/logout', {}, cookie=self.cookie)
        self.assertEqual(code, 200); self.assertIn('Max-Age=0', headers['Set-Cookie'])
        self.assertEqual(self.api('/api/me', token=self.payload['session_token'])[0], 401)
        self.assertIsNone(self.api('/api/auth/status', cookie=self.cookie)[1]['actor'])
        self.assertEqual(self.api('/api/auth/logout', {}, cookie=self.cookie)[0], 200)

    def test_stale_tab_cannot_publish_as_different_cookie_identity(self):
        payload = request(alias='HTTP B', recovery=key())
        _, _, headers = self.api('/api/auth/register', payload)
        cookie_b = headers['Set-Cookie'].split(';')[0]
        expected = {'X-Eksmu-Actor': self.a['actor']['id']}
        self.assertEqual(self.api('/api/explorations', {'body': 'wrong identity'}, cookie=cookie_b, extra=expected)[0], 409)
        self.assertEqual(self.api('/api/auth/logout', {}, cookie=cookie_b, extra=expected)[0], 409)
        self.assertEqual(self.api('/api/me', cookie=cookie_b)[0], 200)
        self.api('/api/auth/logout', {}, cookie=self.cookie, extra=expected)
        self.assertEqual(self.api('/api/auth/logout', {}, cookie=self.cookie, extra=expected)[0], 200)
        self.assertEqual(self.api('/api/auth/logout', {}, extra=expected)[0], 200)

    def test_revoked_cookie_cannot_write_any_component(self):
        work = self.api('/api/explorations', {'body': 'A作品'}, cookie=self.cookie)[1]
        self.api('/api/auth/logout', {}, cookie=self.cookie)
        for path, data in [('/api/explorations', {'body': 'bad'}),
                           ('/api/community/mark', {'exploration_id': work['exploration']['id'], 'kind': 'follow', 'enabled': True}),
                           ('/api/research/create-task', {'revision_id': work['revision']['id'], 'title': 'bad', 'description': '', 'category': 'reproduce'})]:
            with self.subTest(path=path):
                self.assertEqual(self.api(path, data, cookie=self.cookie)[0], 401)

    def test_write_waiting_for_lock_rechecks_revoked_credentials(self):
        work = self.api('/api/explorations', {'body': 'A作品'}, cookie=self.cookie)[1]
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            with RequestLock(self.db):
                waiting = pool.submit(self.api, '/api/community/mark', {'exploration_id': work['exploration']['id'], 'kind': 'follow', 'enabled': True}, cookie=self.cookie)
                store = Store(self.db)
                try:
                    Accounts(store).logout(self.payload['session_token'])
                finally:
                    store.close()
            self.assertEqual(waiting.result(timeout=10)[0], 401)


if __name__ == '__main__':
    unittest.main()
