"""Offline consistency, fail-closed recovery and process-level maintenance leases."""
import json
import hashlib
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from eksmu.backup import create_backup, restore_backup, verify_backup, KEY_SUFFIX
from eksmu.maintenance import InstanceLock, RESTORING
from eksmu.server import make_server
from eksmu.store import Store
from eksmu.accounts import Accounts
from eksmu.community import Community
from eksmu.research import Research
from eksmu.account_recovery import reconcile_accounts
import secrets
import shutil
import uuid


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'data').mkdir()
        self.db = self.root / 'data/community.sqlite'
        store = Store(self.db)
        self.actor = store.register('备份测试身份')
        store.publish(self.actor['token'], '备份测试材料')
        self.records = store.export()
        store.close()
        self.backup = self.root / 'backup'
        self.secrets = self.root / 'secret-backup'
        self.restored = self.root / 'restored'

    def snapshot(self, **kwargs):
        return create_backup(self.db, self.backup, offline=True, **kwargs)

    def edit_manifest(self, action):
        path = self.backup / 'manifest.json'
        value = json.loads(path.read_text())
        action(value)
        path.write_text(json.dumps(value))

    def cli(self, *args):
        return subprocess.run([sys.executable, '-m', 'eksmu', '--db', str(self.db), *map(str, args)],
                              capture_output=True, text=True, timeout=10)

    def test_minimal_snapshot_restores_records_auth_and_optional_absence(self):
        result = self.snapshot()
        self.assertTrue(result['data_verified'])
        self.assertEqual({k for k, v in result['manifest']['components'].items() if v}, {'records'})
        target = restore_backup(self.backup, self.restored)
        store = Store(target)
        try:
            self.assertEqual(store.export(), self.records)
            with self.assertRaises(ValueError):
                store.authenticate(self.actor['token'])
            source = Store(self.db)
            try:
                Accounts(source)
            finally:
                source.close()
            reconcile_accounts(target, self.db, offline=True)
            payload = {'credential': self.actor['token'], 'operation_id': str(uuid.uuid4()),
                       'session_token': 'ois1_' + secrets.token_urlsafe(32), 'label': '恢复设备'}
            self.assertEqual(Accounts(store).issue('login', payload)['actor']['id'], self.actor['actor']['id'])
        finally:
            store.close()
        self.assertFalse(Path(str(target) + '.receipt-signer.key').exists())
        for directory in (self.backup, self.restored):
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
            for path in directory.iterdir():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_private_behavior_case_and_review_survive_instance_restore(self):
        store = Store(self.db)
        try:
            reporter = store.register('报告者')['actor']['id']
            rows = store.rows()
            revision = next(row['id'] for row in rows if row['type'] == 'revision')
            research = Research(str(self.db) + '.research.sqlite', rows)
            try:
                attachment = research.upload(self.actor['actor']['id'], revision, 'example.txt',
                                             'c3ludGhldGljIG1hdGVyaWFs')
                task = research.create_task(reporter, revision, '检查材料', '合成任务', 'reproduce')
                event_id = task['events'][0]['id']
            finally:
                research.close()
            research = Research(str(self.db) + '.research.sqlite', rows)
            hub = Community(str(self.db) + '.community.sqlite', rows)
            try:
                case = hub.submit_behavior(reporter, revision, 'spam', '请核对重复内容')
                hub.submit_behavior(reporter, attachment['id'], 'privacy', '合成附件反馈', research)
                hub.submit_behavior(self.actor['actor']['id'], event_id, 'spam', '合成协作反馈', research)
            finally:
                hub.close(); research.close()
        finally:
            store.close()
        reason = self.root / 'reason.txt'; reason.write_text('已查看，需线下进一步核对', encoding='utf-8')
        result = self.cli('review-behavior', case['id'], '--outcome', 'follow_up', '--reason-file', reason,
                          '--operator-id', 'operator_one')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('请核对重复内容', result.stdout)
        store = Store(self.db)
        try:
            hub = Community(str(self.db) + '.community.sqlite', store.rows())
            try: hub.appeal_behavior(reporter, case['id'], '请复议这一处理记录')
            finally: hub.close()
        finally: store.close()
        self.assertNotEqual(self.cli('review-behavior-appeal', case['id'], '--outcome', 'uphold',
                                     '--reason-file', reason, '--operator-id', 'operator_one').returncode, 0)
        reviewed = self.cli('review-behavior-appeal', case['id'], '--outcome', 'reopen',
                            '--reason-file', reason, '--operator-id', 'operator_two')
        self.assertEqual(reviewed.returncode, 0, reviewed.stderr)
        self.assertNotIn('请复议这一处理记录', reviewed.stdout)
        self.snapshot()
        self.assertTrue(verify_backup(self.backup)['data_verified'])
        restored = restore_backup(self.backup, self.restored)
        with sqlite3.connect(str(restored) + '.community.sqlite') as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM hub_behavior_cases').fetchone()[0], 3)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM hub_behavior_events').fetchone()[0], 3)
            self.assertEqual({r[0] for r in connection.execute('SELECT target FROM hub_behavior_cases')},
                             {revision, attachment['id'], event_id})
        server = make_server(self.db, 0)
        try:
            denied = self.cli('behavior-queue')
            self.assertNotEqual(denied.returncode, 0)
            self.assertIn('Instance is in use', denied.stderr)
            denied = self.cli('review-behavior-appeal', case['id'], '--outcome', 'uphold',
                              '--reason-file', reason, '--operator-id', 'operator_two')
            self.assertNotEqual(denied.returncode, 0)
            self.assertIn('Instance is in use', denied.stderr)
        finally:
            server.server_close()

    def test_moderation_labels_and_appeals_cli_and_backup_restore(self):
        store = Store(self.db)
        try:
            w2 = store.publish(self.actor['token'], '第二篇待复核测试')['exploration']['id']
            rows = store.rows()
            w1 = next(row['id'] for row in rows if row['type'] == 'exploration' and row['id'] != w2)
        finally:
            store.close()

        # 1. Offline CLI label-work
        r1 = self.cli('label-work', '--exploration', w1, '--label', 'commercial_promotion',
                      '--reason', '附带商品购买转化链接', '--operator', 'operator_one')
        self.assertEqual(r1.returncode, 0, r1.stderr)
        lbl1 = json.loads(r1.stdout)
        self.assertEqual(lbl1['label'], 'commercial_promotion')
        self.assertEqual(lbl1['state'], 'active')

        r2 = self.cli('label-work', '--exploration', w2, '--label', 'pending_review',
                      '--reason', 'AI 分诊待人工复核', '--operator', 'operator_ai', '--source', 'ai')
        self.assertEqual(r2.returncode, 0, r2.stderr)
        lbl2 = json.loads(r2.stdout)

        # 2. Author submits appeal on lbl1
        store = Store(self.db)
        try:
            hub = Community(str(self.db) + '.community.sqlite', store.rows())
            try:
                appeal = hub.appeal_label(self.actor['actor']['id'], lbl1['id'], '这是研究工具说明，非商业推广')
            finally:
                hub.close()
        finally:
            store.close()

        # 3. label-queue lists pending_review (lbl2) and pending_appeals (appeal)
        q_res = self.cli('label-queue')
        self.assertEqual(q_res.returncode, 0, q_res.stderr)
        queue = json.loads(q_res.stdout)
        self.assertEqual([x['id'] for x in queue['pending_review']], [lbl2['id']])
        self.assertEqual([x['id'] for x in queue['pending_appeals']], [appeal['id']])

        # 4. review-label-appeal rejects same operator as original labeler, succeeds with distinct operator
        same_op = self.cli('review-label-appeal', '--appeal-id', appeal['id'], '--decision', 'uphold',
                           '--reason', '同一操作者不能自审', '--operator', 'operator_one')
        self.assertNotEqual(same_op.returncode, 0)
        diff_op = self.cli('review-label-appeal', '--appeal-id', appeal['id'], '--decision', 'uphold',
                           '--reason', '经核对正文主要目的仍为商品引流', '--operator', 'operator_two')
        self.assertEqual(diff_op.returncode, 0, diff_op.stderr)

        # 5. Backup -> verify -> restore preserves hub_labels and hub_label_appeals
        self.snapshot()
        self.assertTrue(verify_backup(self.backup)['data_verified'])
        restored = restore_backup(self.backup, self.restored)
        store = Store(restored)
        try:
            hub = Community(str(restored) + '.community.sqlite', store.rows())
            try:
                self.assertEqual(hub.active_label(w1)['id'], lbl1['id'])
                self.assertEqual(hub.active_label(w2)['id'], lbl2['id'])
                status = hub.author_label_status(self.actor['actor']['id'], w1)
                self.assertEqual(status['appeal']['id'], appeal['id'])
                self.assertEqual(status['appeal']['decision'], 'uphold')
                self.assertEqual(status['appeal']['body'], '这是研究工具说明，非商业推广')
            finally:
                hub.close()
        finally:
            store.close()

        # 6. retract-label CLI command retracts lbl2 and immediately restores distribution
        ret = self.cli('retract-label', '--label-id', lbl2['id'], '--reason', '人工复核通过', '--operator', 'operator_two')
        self.assertEqual(ret.returncode, 0, ret.stderr)
        self.assertEqual(json.loads(ret.stdout)['state'], 'retracted')

        # 7. trust-author, list-trusted, untrust-author CLI commands and backup/restore persistence
        aid = self.actor['actor']['id']
        unknown_actor = str(uuid.uuid4())
        bad_trust = self.cli('trust-author', '--actor', unknown_actor, '--reason', '不存在的账户', '--operator', 'operator_one')
        self.assertNotEqual(bad_trust.returncode, 0)
        self.assertIn('找不到这位作者', bad_trust.stderr)

        t_res = self.cli('trust-author', '--actor', aid, '--reason', '已知研究者免除新作者冷静期', '--operator', 'operator_one')
        self.assertEqual(t_res.returncode, 0, t_res.stderr)
        trusted_doc = json.loads(t_res.stdout)
        self.assertEqual(trusted_doc['actor_id'], aid)
        self.assertEqual(trusted_doc['state'], 'active')

        list_res = self.cli('list-trusted')
        self.assertEqual(list_res.returncode, 0, list_res.stderr)
        self.assertEqual([x['actor_id'] for x in json.loads(list_res.stdout)['trusted_authors']], [aid])
        q_res2 = self.cli('label-queue')
        self.assertEqual([x['actor_id'] for x in json.loads(q_res2.stdout)['trusted_authors']], [aid])

        backup2 = self.root / 'backup-trusted'
        restored2 = self.root / 'restored-trusted'
        create_backup(self.db, backup2, offline=True)
        self.assertTrue(verify_backup(backup2)['data_verified'])
        restored_db2 = restore_backup(backup2, restored2)
        store = Store(restored_db2)
        try:
            hub = Community(str(restored_db2) + '.community.sqlite', store.rows())
            try:
                self.assertEqual(hub.trusted_author_ids(), {aid})
                self.assertEqual([x['actor_id'] for x in hub.list_trusted()['trusted_authors']], [aid])
            finally:
                hub.close()
        finally:
            store.close()

        ut_res = self.cli('untrust-author', '--actor', aid, '--reason', '撤销测试信任', '--operator', 'operator_two')
        self.assertEqual(ut_res.returncode, 0, ut_res.stderr)
        self.assertEqual(json.loads(ut_res.stdout)['state'], 'revoked')
        self.assertEqual(json.loads(self.cli('list-trusted').stdout)['trusted_authors'], [])

    def test_private_lean_consent_queue_and_withdrawal_survive_restore(self):
        import base64
        store = Store(self.db)
        try:
            rows = store.rows()
            revision = next(row['id'] for row in rows if row['type'] == 'revision')
            actor = self.actor['actor']['id']
            research = Research(str(self.db) + '.research.sqlite', rows)
            try:
                file = research.upload(actor, revision, 'Proof.lean',
                                       base64.b64encode(b'theorem demo : True := by trivial\n').decode())
                consent = {'local_check': True, 'result_visibility': 'private', 'external_transfer': False}
                queued = research.lean_enroll(actor, revision, file['id'], 'demo', '合成问题',
                                              str(uuid.uuid4()), consent)
                cancelled = research.lean_enroll(actor, revision, file['id'], 'other', '合成问题',
                                                 str(uuid.uuid4()), consent)
                research.lean_withdraw(actor, cancelled['id'])
            finally:
                research.close()
        finally:
            store.close()
        self.snapshot()
        restored = restore_backup(self.backup, self.restored)
        store = Store(restored)
        try:
            research = Research(str(restored) + '.research.sqlite', store.rows())
            try:
                self.assertEqual(research.next_lean_job()['id'], queued['id'])
                self.assertEqual(research.lean_enrollment(actor, cancelled['id'])['state'], 'withdrawn')
                self.assertIsNone(research.lean_enrollment(actor, queued['id'])['report'])
            finally:
                research.close()
        finally:
            store.close()

    def test_offline_acknowledgement_required_without_creating_output(self):
        with self.assertRaises(ValueError):
            create_backup(self.db, self.backup)
        self.assertFalse(self.backup.exists())

    def test_analysis_records_survive_restore_and_older_manifests_still_verify(self):
        from eksmu.analysis import Analysis
        store = Store(self.db)
        try:
            rev = next(r for r in store.rows() if r['type'] == 'revision')
            work = Analysis(str(self.db) + '.analysis.sqlite', store.rows())
            work.request(self.actor['actor']['id'], rev['id']); work.close()
        finally: store.close()
        result = self.snapshot()
        self.assertTrue(result['manifest']['components']['analysis'])
        restored = restore_backup(self.backup, self.restored)
        store = Store(restored)
        try:
            work = Analysis(str(restored) + '.analysis.sqlite', store.rows())
            self.assertEqual(work.view(rev['id'])['request']['state'], 'queued'); work.close()
        finally: store.close()
        # A backup made before the analysis component existed has no such key.
        shutil.rmtree(self.backup); Path(str(self.db) + '.analysis.sqlite').unlink(); self.snapshot()
        self.edit_manifest(lambda m: m['components'].pop('analysis'))
        self.assertTrue(verify_backup(self.backup)['data_verified'])

    def test_v2_inventory_requires_publication_component_even_when_absent(self):
        self.snapshot()
        self.edit_manifest(lambda m: m['components'].pop('publications'))
        with self.assertRaises(ValueError): verify_backup(self.backup)

    def test_legacy_v1_manifest_is_readable_but_cannot_hide_extra_component(self):
        self.snapshot()
        self.edit_manifest(lambda m: m.update(format='eksmu.instance-backup.v1'))
        with self.assertRaises(ValueError): verify_backup(self.backup)
        self.edit_manifest(lambda m: m['components'].pop('publications'))
        self.assertTrue(verify_backup(self.backup)['data_verified'])

    def test_missing_publication_database_from_snapshot_is_rejected(self):
        from eksmu.publications import Publications
        store = Store(self.db)
        try:
            pub = Publications(str(self.db) + '.publications.sqlite', store.rows())
            pub.close()
        finally: store.close()
        self.snapshot(); (self.backup / 'publications.sqlite').unlink()
        with self.assertRaises(ValueError): restore_backup(self.backup, self.restored)
        self.assertFalse(self.restored.exists())

    def test_running_server_blocks_backup_even_from_another_process(self):
        server = make_server(self.db, 0)
        try:
            result = self.cli('backup', self.backup, '--offline')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Instance is in use', result.stderr)
            self.assertFalse(self.backup.exists())
        finally:
            server.server_close()
        self.snapshot()

    def test_maintenance_blocks_server_and_record_import(self):
        with InstanceLock(self.db, exclusive=True):
            with self.assertRaises(ValueError):
                make_server(self.db, 0)
            result = self.cli('import', self.root / 'does-not-exist.json')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Instance is in use', result.stderr)
        server = make_server(self.db, 0)
        server.server_close()

    def test_failed_server_bind_releases_lease(self):
        with patch('eksmu.server._InstanceServer', side_effect=OSError('bind failure')):
            with self.assertRaises(OSError):
                make_server(self.db, 0)
        self.snapshot()

    def test_uncooperative_sqlite_writer_blocks_backup(self):
        connection = sqlite3.connect(self.db)
        try:
            connection.execute('BEGIN IMMEDIATE')
            with self.assertRaises(sqlite3.OperationalError):
                self.snapshot()
            self.assertFalse(self.backup.exists())
        finally:
            connection.close()
        self.snapshot()

    def test_committed_wal_records_are_preserved(self):
        connection = sqlite3.connect(self.db)
        store = None
        try:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('PRAGMA wal_autocheckpoint=0')
            store = Store(self.db)
            store.publish(self.actor['token'], '只在 WAL 中的新记录')
            expected = store.export()
            self.assertGreater(Path(str(self.db) + '-wal').stat().st_size, 0)
            self.snapshot()
        finally:
            if store is not None:
                store.close()
            connection.close()
        store = Store(restore_backup(self.backup, self.restored))
        try:
            self.assertEqual(store.export(), expected)
        finally:
            store.close()

    def test_signer_is_separate_required_and_preserved(self):
        raw = bytes(range(32))
        Path(str(self.db) + KEY_SUFFIX).write_bytes(raw)
        with self.assertRaises(ValueError):
            self.snapshot()
        result = self.snapshot(secrets_dir=self.secrets)
        self.assertTrue(result['secrets_verified'])
        self.assertFalse((self.backup / 'receipt-signer.key').exists())
        self.assertFalse(verify_backup(self.backup)['secrets_verified'])
        with self.assertRaises(ValueError):
            restore_backup(self.backup, self.restored)
        target = restore_backup(self.backup, self.restored, secrets_dir=self.secrets)
        self.assertEqual(Path(str(target) + KEY_SUFFIX).read_bytes(), raw)
        self.assertEqual(self.secrets.stat().st_mode & 0o777, 0o700)
        for path in self.secrets.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_missing_receipt_key_is_not_regenerated(self):
        Path(str(self.db) + '.receipt-tasks.sqlite').touch()
        with self.assertRaisesRegex(ValueError, 'no signing key'):
            self.snapshot()
        self.assertFalse(Path(str(self.db) + KEY_SUFFIX).exists())

    def test_wrong_secret_package_is_rejected(self):
        Path(str(self.db) + KEY_SUFFIX).write_bytes(bytes(range(32)))
        self.snapshot(secrets_dir=self.secrets)
        path = self.secrets / 'secrets.json'
        value = json.loads(path.read_text()); value['backup_id'] = 'wrong'
        path.write_text(json.dumps(value))
        with self.assertRaises(ValueError):
            restore_backup(self.backup, self.restored, secrets_dir=self.secrets)
        self.assertFalse(self.restored.exists())

    def test_existing_destinations_are_untouched_including_empty_ones(self):
        self.backup.mkdir(); sentinel = self.backup / 'keep'; sentinel.write_bytes(b'keep')
        with self.assertRaises(ValueError):
            self.snapshot()
        self.assertEqual(sentinel.read_bytes(), b'keep')
        sentinel.unlink(); self.backup.rmdir(); self.snapshot()
        self.restored.mkdir()
        with self.assertRaises(FileExistsError):
            restore_backup(self.backup, self.restored)
        self.assertTrue(self.restored.is_dir())
        self.assertEqual(list(self.restored.iterdir()), [])

    def test_restore_rejects_corrupt_database_and_keeps_original(self):
        self.snapshot(); before = self.db.read_bytes()
        with (self.backup / 'records.sqlite').open('r+b') as stream:
            stream.write(b'BROKEN')
        with self.assertRaises(ValueError):
            restore_backup(self.backup, self.restored)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertFalse(self.restored.exists())

    def test_matching_digest_does_not_bypass_database_validation(self):
        self.snapshot(); path = self.backup / 'records.sqlite'
        with sqlite3.connect(path) as connection:
            connection.execute('DROP TABLE records')
        self.edit_manifest(lambda value: value['components'].update({'records': {
            'size': path.stat().st_size, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}}))
        with self.assertRaisesRegex(ValueError, 'Unsupported database tables'):
            restore_backup(self.backup, self.restored)
        self.assertFalse(self.restored.exists())

    def test_missing_extra_and_partial_backup_files_are_rejected(self):
        self.snapshot()
        for name in ('.incomplete', 'unexpected.sqlite'):
            with self.subTest(name=name):
                path = self.backup / name; path.touch()
                with self.assertRaises(ValueError):
                    verify_backup(self.backup)
                path.unlink()
        (self.backup / 'records.sqlite').unlink()
        with self.assertRaises(ValueError):
            restore_backup(self.backup, self.restored)

    def test_manifest_shape_and_duplicate_keys_are_rejected(self):
        self.snapshot(); path = self.backup / 'manifest.json'; original = path.read_text()
        cases = ['[]', '{"format":1,"format":2}', original.replace('eksmu.instance-backup.v2', 'future-format')]
        for content in cases:
            with self.subTest(content=content[:40]):
                path.write_text(content)
                with self.assertRaises(ValueError):
                    verify_backup(self.backup)

    def test_runtime_mismatch_requires_explicit_version_recovery(self):
        self.snapshot()
        self.edit_manifest(lambda manifest: manifest['application']['files'].update({'eksmu/store.py': '0' * 64}))
        self.assertFalse(verify_backup(self.backup)['runtime_matches'])
        with self.assertRaisesRegex(ValueError, 'recorded runtime'):
            restore_backup(self.backup, self.restored)
        self.assertFalse(self.restored.exists())

    def test_unknown_component_is_refused_instead_of_silently_lost(self):
        Path(str(self.db) + '.future.sqlite').touch()
        with self.assertRaisesRegex(ValueError, 'Unknown instance sidecar'):
            self.snapshot()
        self.assertFalse(self.backup.exists())

    def test_missing_main_or_orphaned_sidecar_is_refused(self):
        orphan = Path(str(self.db) + '.research.sqlite-wal'); orphan.touch()
        with self.assertRaisesRegex(ValueError, 'Orphaned'):
            self.snapshot()
        orphan.unlink(); self.db.unlink()
        with self.assertRaisesRegex(ValueError, 'missing'):
            self.snapshot()
        self.assertFalse(self.db.exists())

    def test_source_schema_not_silently_extended(self):
        with sqlite3.connect(self.db) as connection:
            connection.execute('CREATE TABLE future_data(value TEXT)')
        with self.assertRaisesRegex(ValueError, 'Unsupported database tables'):
            self.snapshot()
        self.assertFalse(self.backup.exists())

    def test_symlink_and_nested_locations_are_refused(self):
        link = self.root / 'alias'; link.symlink_to(self.db.parent, target_is_directory=True)
        with self.assertRaises(ValueError):
            create_backup(link / self.db.name, self.backup, offline=True)
        with self.assertRaises(ValueError):
            create_backup(self.db, self.db.parent / 'nested', offline=True)
        sidecar = Path(str(self.db) + '-wal'); sidecar.symlink_to(self.db)
        with self.assertRaises(ValueError):
            self.snapshot()
        sidecar.unlink(); self.snapshot()
        (self.backup / 'records.sqlite').unlink()
        (self.backup / 'records.sqlite').symlink_to(self.db)
        with self.assertRaises(ValueError):
            restore_backup(self.backup, self.restored)

    def test_copy_failure_cleans_only_new_directory(self):
        self.snapshot(); before = self.db.read_bytes()
        with patch('eksmu.backup._copy', side_effect=OSError('test disk failure')):
            with self.assertRaises(OSError):
                restore_backup(self.backup, self.restored)
        self.assertFalse(self.restored.exists())
        self.assertEqual(self.db.read_bytes(), before)
        self.assertTrue(verify_backup(self.backup)['data_verified'])

    def test_crash_marker_prevents_starting_partial_restore(self):
        (self.db.parent / RESTORING).touch()
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            make_server(self.db, 0)

    def test_cli_backup_verify_restore_emit_only_summary(self):
        for args in [('backup', self.backup, '--offline'), ('verify-backup', self.backup),
                     ('restore', self.backup, '--into', self.restored)]:
            with self.subTest(command=args[0]):
                result = self.cli(*args)
                self.assertEqual(result.returncode, 0, result.stderr)
                json.loads(result.stdout)
                self.assertNotIn(self.actor['token'], result.stdout + result.stderr)
                self.assertNotIn('备份测试材料', result.stdout + result.stderr)
        self.assertFalse(json.loads(result.stdout)['service_started'])

    def test_full_two_identity_recovery_in_independent_processes(self):
        result = subprocess.run([sys.executable, 'scripts/demo_backup.py'],
                                capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['status'], 'PASS')
        self.assertEqual(report['identities'], 2)
        self.assertEqual(len(report['checks']), 8)


if __name__ == '__main__':
    unittest.main()
