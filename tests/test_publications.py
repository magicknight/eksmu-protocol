import base64
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

from eksmu.store import Problem, Store, canonical
from eksmu.research import Research
from eksmu.publications import Publications
from eksmu.publication_contract import citation, seal, verify_package
from eksmu.time_evidence import create as create_time_evidence, verify as verify_time_evidence, commitment


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.store = Store(self.root / 'community.sqlite')
        self.a = self.store.register('演示甲'); self.b = self.store.register('演示乙')
        self.work = self.store.publish(self.a['token'], '固定正文', '演示标题')
        self.rev = self.work['revision']; self.wid = self.work['exploration']['id']; self.aid = self.a['actor']['id']
        self.research = Research(self.root / 'research.sqlite', self.store.rows())
        self.path = self.root / 'publications.sqlite'; self.pub = Publications(self.path, self.store.rows())

    def tearDown(self):
        self.pub.close(); self.research.close(); self.store.close(); self.temp.cleanup()

    def upload(self, name='data.csv', raw=b'n,total\n1,1\n2,3\n'):
        return self.research.upload(self.aid, self.rev['id'], name, base64.b64encode(raw).decode())

    def snapshot(self, items=(), previous=None, operation=None, **kwargs):
        return self.pub.create_snapshot(self.research, self.aid, self.rev['id'], list(items), previous,
                                         operation or str(uuid.uuid4()), **kwargs)

    def event(self, kind='note', previous=None, operation=None):
        return self.pub.event(self.aid, self.rev['id'], kind, '演示说明', previous, operation or str(uuid.uuid4()))

    def test_text_only_snapshot_and_no_identity_verifier_in_package(self):
        snap = self.snapshot(); package = self.pub.export(snap['id'])
        result = verify_package(package, snap['sha256'])
        self.assertTrue(result['content_integrity']); self.assertEqual(result['materials'], 0)
        self.assertFalse(result['external_time_verified']); self.assertFalse(result['authorship_verified'])
        raw = canonical(package)
        for secret in (self.a['token'], self.a['actor']['payload']['key_hash'], 'key_hash'):
            self.assertNotIn(secret, raw)

    def test_later_upload_does_not_modify_or_enter_old_snapshot(self):
        old = self.upload(); snap = self.snapshot([old['id']]); package = self.pub.export(snap['id'])
        later = self.upload('later.csv', b'new material')
        self.assertEqual(self.pub.export(snap['id']), package)
        with self.assertRaises(Problem): self.pub.file(snap['id'], later['id'])
        new = self.snapshot([old['id'], later['id']], snap['id'])
        self.assertNotEqual(new['sha256'], snap['sha256'])
        self.assertEqual(new['previous_snapshot_id'], snap['id'])
        self.assertEqual(verify_package(self.pub.export(new['id']))['materials'], 2)

    def test_snapshot_uses_archived_bytes_if_live_material_is_changed_or_missing(self):
        item = self.upload(); snap = self.snapshot([item['id']]); expected = self.pub.export(snap['id'])
        self.research.db.execute('UPDATE research_files SET data=? WHERE id=?', (b'corrupt live file', item['id']))
        self.assertEqual(self.pub.export(snap['id']), expected)
        self.research.db.execute('DELETE FROM research_files WHERE id=?', (item['id'],))
        self.assertEqual(self.pub.export(snap['id']), expected)

    def test_only_explicit_selection_is_captured(self):
        one = self.upload(); two = self.upload('not-selected.txt', b'not included')
        snap = self.snapshot([one['id']]); self.assertEqual([m['id'] for m in snap['materials']], [one['id']])
        with self.assertRaises(Problem): self.pub.file(snap['id'], two['id'])

    def test_same_bytes_are_deduplicated_without_losing_filenames(self):
        one = self.upload('first.txt', b'identical'); two = self.upload('second.txt', b'identical')
        snap = self.snapshot([one['id'], two['id']]); package = self.pub.export(snap['id'])
        self.assertEqual(len(package['blobs']), 1); self.assertEqual(verify_package(package)['materials'], 2)
        self.assertEqual(self.pub.file(snap['id'], two['id'])[2], 'second.txt')

    def test_mixed_or_unknown_attachments_reject_entire_snapshot(self):
        good = self.upload(); other = self.store.publish(self.a['token'], 'other')
        self.research.close(); self.research = Research(self.root / 'research.sqlite', self.store.rows())
        wrong = self.research.upload(self.aid, other['revision']['id'], 'other.txt', base64.b64encode(b'other').decode())
        for ids in ([good['id'], wrong['id']], [good['id'], str(uuid.uuid4())], [good['id'], good['id']]):
            with self.assertRaises(Problem): self.snapshot(ids)
        self.assertEqual(self.pub.db.execute('SELECT COUNT(*) FROM publication_snapshots').fetchone()[0], 0)
        self.assertEqual(self.pub.db.execute('SELECT COUNT(*) FROM publication_blobs').fetchone()[0], 0)

    def test_snapshot_retry_survives_restart_and_newer_snapshot(self):
        operation = str(uuid.uuid4()); old = self.snapshot(operation=operation)
        self.snapshot(previous=old['id'])
        self.pub.close(); self.pub = Publications(self.path, self.store.rows())
        self.assertEqual(self.snapshot(operation=operation), old)
        self.assertEqual(self.pub.db.execute('SELECT COUNT(*) FROM publication_snapshots').fetchone()[0], 2)

    def test_snapshot_stale_head_and_operation_reuse_rejected(self):
        item = self.upload(); snap = self.snapshot()
        with self.assertRaises(Problem) as caught: self.snapshot()
        self.assertEqual(caught.exception.status, 409)
        with self.assertRaises(Problem): self.snapshot([item['id']], operation=snap['id'])
        self.assertEqual(self.pub.db.execute('SELECT COUNT(*) FROM publication_snapshots').fetchone()[0], 1)

    def test_concurrent_snapshot_creators_cannot_fork_the_work_head(self):
        records = self.store.rows(); barrier = threading.Barrier(2)
        def create():
            pub = Publications(self.path, records); research = Research(self.root / 'research.sqlite', records)
            try:
                barrier.wait(timeout=5)
                try:
                    pub.create_snapshot(research, self.aid, self.rev['id'], [], None, str(uuid.uuid4()))
                    return 200
                except Problem as error: return error.status
            finally: pub.close(); research.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(create) for _ in range(2)]
            self.assertEqual(sorted(f.result(timeout=10) for f in futures), [200, 409])
        self.assertEqual(self.pub.db.execute('SELECT COUNT(*) FROM publication_snapshots').fetchone()[0], 1)

    def test_other_account_cannot_snapshot_or_withdraw(self):
        for operation in (lambda: self.pub.create_snapshot(self.research, self.b['actor']['id'], self.rev['id'], [], None, str(uuid.uuid4())),
                          lambda: self.pub.event(self.b['actor']['id'], self.rev['id'], 'withdraw', 'no', None, str(uuid.uuid4()))):
            with self.assertRaises(Problem) as caught: operation()
            self.assertEqual(caught.exception.status, 403)

    def test_bad_live_digest_and_archive_corruption_fail_closed(self):
        item = self.upload(); snap = self.snapshot([item['id']])
        self.research.db.execute('UPDATE research_files SET data=? WHERE id=?', (b'bad', item['id']))
        with self.assertRaises(Problem): self.snapshot([item['id']], snap['id'])
        self.pub.db.execute('UPDATE publication_blobs SET data=?', (b'bad',))
        with self.assertRaises(Problem): self.pub.export(snap['id'])

    def test_blob_quota_failure_rolls_back_both_blob_and_manifest(self):
        one = self.upload('one.txt', b'111'); two = self.upload('two.txt', b'222')
        with patch('eksmu.publications.MAX_BLOBS_BYTES', 4):
            with self.assertRaises(Problem): self.snapshot([one['id'], two['id']])
        self.assertEqual(self.pub.db.execute('SELECT COUNT(*) FROM publication_blobs').fetchone()[0], 0)
        self.assertEqual(self.pub.db.execute('SELECT COUNT(*) FROM publication_snapshots').fetchone()[0], 0)
        self.store.publish(self.a['token'], '普通发表仍可继续')

    def test_document_quota_failure_does_not_restrict_normal_publication(self):
        with patch('eksmu.publications.MAX_DOCUMENTS_BYTES', 0):
            with self.assertRaises(Problem): self.snapshot()
            with self.assertRaises(Problem): self.event()
        self.assertEqual(self.pub.status(self.wid)['events'], [])
        self.store.publish(self.a['token'], 'ordinary publication')

    def test_withdrawal_preserves_snapshots_comments_and_new_revision_right(self):
        snap = self.snapshot(); package = self.pub.export(snap['id'])
        comment = self.store.comment(self.b['token'], self.rev['id'], '公开质疑')
        event = self.event('withdraw'); self.assertTrue(self.pub.status(self.wid)['withdrawn'])
        self.assertEqual(self.pub.export(snap['id']), package)
        self.assertIn(comment, self.store.rows())
        newer = self.store.revise(self.a['token'], self.wid, self.rev['id'], 'new', 'new content')
        self.assertIn(newer, self.store.rows())
        self.assertTrue(self.pub.status(self.wid)['withdrawn'])
        self.event('reinstate', event['id']); self.assertFalse(self.pub.status(self.wid)['withdrawn'])
        self.assertEqual(len(self.pub.status(self.wid)['events']), 2)

    def test_event_retry_and_conflict_preserve_history(self):
        operation = str(uuid.uuid4()); withdrawal = self.event('withdraw', operation=operation)
        note = self.event('note', withdrawal['id'])
        self.assertEqual(self.event('withdraw', operation=operation), withdrawal)
        with self.assertRaises(Problem): self.event('withdraw', note['id'])
        with self.assertRaises(Problem): self.event('reinstate')
        with self.assertRaises(Problem): self.event('note', operation=operation)
        self.assertEqual(len(self.pub.status(self.wid)['events']), 2)

    def test_event_tamper_is_not_a_current_status(self):
        self.event('withdraw')
        row = self.pub.db.execute('SELECT document FROM publication_events').fetchone()[0]
        value = json.loads(row); value['body'] = 'tampered'
        self.pub.db.execute('UPDATE publication_events SET document=?', (canonical(value),))
        with self.assertRaises(Problem): self.pub.status(self.wid)

    def test_package_tampering_missing_extra_and_wrong_size_are_rejected(self):
        item = self.upload(); snap = self.snapshot([item['id']]); package = self.pub.export(snap['id'])
        cases = []
        bad = deepcopy(package); bad['snapshot']['revision']['payload']['body'] += 'tamper'; cases.append(bad)
        bad = deepcopy(package); bad['snapshot']['publisher']['alias'] = 'forged'; cases.append(bad)
        bad = deepcopy(package); bad['blobs'][0]['data_b64'] = base64.b64encode(b'other').decode(); cases.append(bad)
        bad = deepcopy(package); bad['blobs'] = []; cases.append(bad)
        bad = deepcopy(package); bad['blobs'].append(bad['blobs'][0]); cases.append(bad)
        bad = deepcopy(package); bad['snapshot']['materials'][0]['size'] += 1
        bad['snapshot'] = seal({k:v for k,v in bad['snapshot'].items() if k!='sha256'}); cases.append(bad)
        for bad in cases:
            with self.assertRaises(ValueError): verify_package(bad)
        with self.assertRaises(Problem): verify_package(package, '0'*64)

    def test_manifest_storage_reference_cannot_be_swapped(self):
        snap = self.snapshot()
        self.pub.db.execute('UPDATE publication_snapshots SET revision_id=?', (str(uuid.uuid4()),))
        with self.assertRaises(Problem): self.pub.snapshot(snap['id'])

    def test_legacy_imported_attachment_timezone_is_preserved(self):
        item = self.upload(); stamp = '2020-01-01T08:00:00+08:00'
        self.research.db.execute('UPDATE research_files SET created_at=?', (stamp,))
        snap = self.snapshot([item['id']]); self.assertEqual(snap['materials'][0]['created_at'], stamp)

    def test_citation_binds_exact_revision_or_snapshot_without_fake_doi_or_url(self):
        pub = self.pub.publisher(self.rev); plain = citation(self.rev, pub); snap = self.snapshot()
        frozen = citation(self.rev, pub, snap)
        self.assertIn(self.rev['id'], plain['bibtex']); self.assertIn(snap['sha256'], frozen['bibtex'])
        self.assertIn('revision='+self.rev['id'], plain['relative_url'])
        self.assertEqual(frozen['relative_url'], '/?snapshot='+snap['id'])
        self.assertNotIn('doi =', frozen['bibtex']); self.assertNotIn('year =', frozen['bibtex'])
        self.assertNotIn('127.0.0.1', frozen['bibtex']); self.assertNotIn('url =', frozen['bibtex'])

    def test_unmatched_braces_tex_commands_and_and_in_names_are_escaped(self):
        from eksmu.publication_contract import tex
        result = tex(r'\input{secret & 100% #$^~_')
        self.assertNotIn(r'\input', result); self.assertIn(r'\textbackslash{}input\char123{}', result)
        pub = {'id': self.aid, 'alias': 'First and Second {Group'}
        output = citation(self.rev, pub)['bibtex']
        self.assertIn('author = {{First and Second', output)
        parsed = subprocess.run(['pandoc', '--sandbox', '-f', 'bibtex', '-t', 'csljson'],
                                input=output, text=True, capture_output=True, timeout=10)
        self.assertEqual(parsed.returncode, 0, parsed.stderr)
        self.assertEqual(len(json.loads(parsed.stdout)[0]['author']), 1)

    def test_offline_cli_checks_package_without_opening_or_creating_database(self):
        snap = self.snapshot([self.upload()['id']]); package = self.pub.export(snap['id'])
        path = self.root / 'package.json'; path.write_text(canonical(package))
        unused = self.root / 'no-db' / 'community.sqlite'
        args = [sys.executable, '-m', 'eksmu', '--db', str(unused), 'verify-publication', str(path), '--expect-sha256', snap['sha256']]
        result = subprocess.run(args, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['content_integrity']); self.assertFalse(unused.parent.exists())
        path.write_text('{"format":1,"format":2}')
        self.assertNotEqual(subprocess.run(args, capture_output=True, timeout=10).returncode, 0)

    def test_local_time_evidence_binds_exact_snapshot_and_materials(self):
        item = self.upload(); old = self.snapshot([item['id']]); package = self.pub.export(old['id'])
        nonce = '00' * 31 + '01'
        evidence = create_time_evidence(package, nonce)
        result = verify_time_evidence(evidence, evidence['commitment']['sha256'])
        self.assertEqual(result['status'], 'local_only')
        self.assertFalse(result['external_time_verified'])
        self.assertIsNone(result['public_publication_event'])
        self.assertIsNone(result['external_time_evidence'])
        self.assertEqual(result['materials'], 1)
        self.assertEqual(result['commitment_sha256'], commitment(old['sha256'], nonce))
        later = self.upload('later.csv', b'later')
        newer = self.snapshot([item['id'], later['id']], old['id'])
        self.assertNotEqual(create_time_evidence(self.pub.export(newer['id']), nonce)['commitment']['sha256'],
                            result['commitment_sha256'])
        changed = deepcopy(evidence)
        changed['publication_package']['blobs'][0]['data_b64'] = base64.b64encode(b'tampered').decode()
        with self.assertRaises(Problem): verify_time_evidence(changed)
        changed = deepcopy(evidence); changed['publication_package']['snapshot']['publisher']['alias'] = 'other'
        with self.assertRaises(Problem): verify_time_evidence(changed)
        changed = deepcopy(evidence); changed['commitment']['nonce_hex'] = '00' * 32
        with self.assertRaises(Problem): verify_time_evidence(changed)
        changed = deepcopy(evidence); changed['external_proof'] = {'claimed': 'verified'}
        with self.assertRaises(Problem): verify_time_evidence(changed)
        with self.assertRaises(Problem): verify_time_evidence(evidence, '0' * 64)

    def test_local_time_evidence_cli_is_offline_and_rejects_missing_nonce(self):
        snap = self.snapshot([self.upload()['id']]); source = self.root / 'publication.json'
        source.write_text(canonical(self.pub.export(snap['id'])))
        evidence_file = self.root / 'evidence.json'; unused = self.root / 'no-db' / 'community.sqlite'
        prefix = [sys.executable, '-m', 'eksmu', '--db', str(unused)]
        made = subprocess.run(prefix + ['create-time-evidence', str(source), str(evidence_file)],
                              capture_output=True, text=True, timeout=10)
        self.assertEqual(made.returncode, 0, made.stderr)
        self.assertFalse(unused.parent.exists())
        expected = json.loads(made.stdout)['commitment_sha256']
        checked = subprocess.run(prefix + ['verify-time-evidence', str(evidence_file),
                                           '--expect-commitment', expected], capture_output=True, text=True, timeout=10)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertEqual(json.loads(checked.stdout)['status'], 'local_only')
        self.assertNotEqual(subprocess.run(prefix + ['create-time-evidence', str(source), str(evidence_file)],
                                           capture_output=True, timeout=10).returncode, 0)
        value = json.loads(evidence_file.read_text()); del value['commitment']['nonce_hex']
        evidence_file.write_text(canonical(value))
        self.assertNotEqual(subprocess.run(prefix + ['verify-time-evidence', str(evidence_file)],
                                           capture_output=True, timeout=10).returncode, 0)

    def test_local_commitment_fixed_vector(self):
        # Fixed bytes also permit an implementation in another language to reproduce the digest.
        self.assertEqual(commitment('00' * 32, '00' * 31 + '01'),
                         '9f33977eddd8b45356a98eb5bff91c2fd930bd742e3c25d3a62d7aeb53e31cbc')

    def test_partial_or_foreign_sidecar_is_refused(self):
        path = self.root / 'foreign.sqlite'
        with sqlite3.connect(path) as db: db.execute('CREATE TABLE publication_blobs(sha256 TEXT, data BLOB)')
        with self.assertRaises(Problem): Publications(path, self.store.rows())


if __name__ == '__main__': unittest.main()
