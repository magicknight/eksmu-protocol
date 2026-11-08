import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from eksmu.store import Store, Problem
from eksmu.research import Research
from eksmu.material_chain import material_record_id, materials_status, reconcile, reconcile_instance
from eksmu import record_chain, work_page
from eksmu.mirror import write_local
from tools.eksmu_verify import verify as verify_standalone, verify_file as verify_file_standalone, Invalid
import json
import subprocess
import sys


class MaterialChainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / 'data.sqlite')
        self.addCleanup(self.store.close)
        author = self.store.register('Author')
        self.author = author['actor']['id']
        self.revision = self.store.publish(author['token'], 'Text', 'Title')['revision']
        self.research = Research(str(self.root / 'data.sqlite') + '.research.sqlite', self.store.rows())
        self.addCleanup(self.research.close)

    def upload(self, name, raw):
        return self.research.upload(self.author, self.revision['id'], name, base64.b64encode(raw).decode())

    def test_backfill_restart_and_offline_mirror(self):
        self.upload('a.pdf', b'%PDF-1.4 test')
        with self.assertRaises(Problem):
            reconcile(self.store, self.research)
        with patch('eksmu.record_chain.append', side_effect=RuntimeError('interrupted')):
            with self.assertRaises(RuntimeError):
                reconcile(self.store, self.research, experimental=True)
        self.assertEqual(sum(r['type'] == 'material' for r in self.store.rows()), 0)
        result = reconcile(self.store, self.research, experimental=True)
        self.assertEqual(result['added'], 1)
        self.assertEqual(reconcile(self.store, self.research, experimental=True)['already_committed'], 1)
        row = next(r for r in self.store.rows() if r['type'] == 'material')
        proof = record_chain.record_proof(self.store.db, row['id'])
        self.assertTrue(proof['backfilled'])
        self.assertIsNone(proof['anchor'])
        self.assertTrue(record_chain.verify(self.store.db)['consistent'])
        report = write_local(self.store, self.root / 'data.sqlite', self.root / 'mirror')
        self.assertEqual(report['format'], 'eksmu.public-mirror.v2')
        self.assertTrue(verify_standalone(self.root / 'mirror')['internally_consistent'])
        manifest = self.root / 'mirror' / 'manifest.json'
        data = json.loads(manifest.read_text())
        data['format'] = 'eksmu.public-mirror.v1'
        manifest.write_text(json.dumps(data))
        with self.assertRaises(Invalid):
            verify_standalone(self.root / 'mirror')

    def test_corrupt_file_is_never_committed(self):
        item = self.upload('a.pdf', b'%PDF-1.4 test')
        self.research.db.execute('UPDATE research_files SET data=? WHERE id=?', (b'corrupt', item['id']))
        with self.assertRaises(Problem):
            reconcile(self.store, self.research, experimental=True)
        self.assertFalse(any(r['type'] == 'material' for r in self.store.rows()))

    def test_changed_metadata_detected(self):
        item = self.upload('a.pdf', b'%PDF-1.4 test')
        reconcile(self.store, self.research, experimental=True)
        self.research.db.execute('UPDATE research_files SET origin=? WHERE id=?', ('other', item['id']))
        with self.assertRaises(Problem):
            reconcile(self.store, self.research, experimental=True)

    def test_reconcile_cli_work_page_display_and_standalone_file_verification(self):
        pdf_bytes = b'%PDF-1.4 distributed-chain-test\n'
        item = self.upload('paper.pdf', pdf_bytes)
        db_path = self.root / 'data.sqlite'
        self.research.close()
        self.store.close()
        proc = subprocess.run(
            [sys.executable, '-m', 'eksmu', '--db', str(db_path), 'reconcile-materials', '--offline'],
            capture_output=True, text=True, check=True,
        )
        summary = json.loads(proc.stdout)
        self.assertEqual(summary['added'], 1)
        self.assertTrue(summary['backfilled'])
        self.assertEqual(reconcile_instance(db_path, offline=True)['already_committed'], 1)

        self.store = Store(db_path)
        self.research = Research(str(db_path) + '.research.sqlite', self.store.rows())
        mat_rid = material_record_id(item['id'])
        status_map = materials_status(self.store.db, self.store.rows(), self.revision['id'])
        self.assertIn(item['id'], status_map)
        self.assertEqual(status_map[item['id']]['record_id'], mat_rid)
        self.assertIsNone(status_map[item['id']]['anchor'])

        # Render work page before Bitcoin anchor confirmation (pending anchor state)
        exp_id = self.revision['payload']['exploration_id']
        data_pending = work_page.load(str(db_path), self.store.rows(), {}, exp_id, self.revision['id'], chain_db=self.store.db)
        zh_pending = work_page.page(data_pending, 'zh', 'https://eksmu.com').decode('utf-8')
        en_pending = work_page.page(data_pending, 'en', 'https://eksmu.com').decode('utf-8')
        seq = status_map[item['id']]['seq']
        self.assertIn(f'记录链 #{seq}（补链）（待锚定）', zh_pending)
        self.assertIn(f'/api/chain/proof?record={mat_rid}', zh_pending)
        self.assertIn('本版 1/1 份附件的内容摘要已写入记录链（其中 0 份已获比特币区块锚定', zh_pending)
        self.assertIn(f'Chain #{seq} (backfilled) (awaiting anchor)', en_pending)
        self.assertIn('Attachment chain proof', en_pending)

        # Record a confirmed Bitcoin anchor covering the material record
        _, head = record_chain.tip(self.store.db)
        with self.store.db:
            self.store.db.execute(
                'INSERT INTO chain_anchors(seq, head, method, submitted_at, updated_at, proof, state, bitcoin, checked) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (seq, head, 'opentimestamps', '2026-11-07T00:00:00+00:00', '2026-11-07T01:00:00+00:00',
                 b'\x00OpenTimestamps\x00\x00Proof\x00\xbf\x89\xe2\xe8\x84\xe8\x92\x94\x08',
                 'confirmed',
                 json.dumps({'height': 971234, 'merkle_root': 'a' * 64}),
                 json.dumps({'matched': 2, 'block_time': '2026-11-07T01:00:00+00:00'})),
            )

        data_anchored = work_page.load(str(db_path), self.store.rows(), {}, exp_id, self.revision['id'], chain_db=self.store.db)
        zh_anchored = work_page.page(data_anchored, 'zh', 'https://eksmu.com').decode('utf-8')
        en_anchored = work_page.page(data_anchored, 'en', 'https://eksmu.com').decode('utf-8')
        self.assertIn(f'记录链 #{seq}（补链） · 比特币区块 #971234', zh_anchored)
        self.assertIn('本版全部 1 份附件的内容摘要已补链入记录链并获比特币区块锚定', zh_anchored)
        self.assertIn(f'Chain #{seq} (backfilled) · Bitcoin block #971234', en_anchored)
        self.assertIn('Content digests of all 1 attachment(s) in this version are backfilled into the record chain and anchored in a Bitcoin block', en_anchored)

        # Standalone file verification against both v2 mirror and single-record proof JSON
        local_pdf = self.root / 'downloaded-paper.pdf'
        local_pdf.write_bytes(pdf_bytes)
        mirror_dir = self.root / 'mirror-v2'
        write_local(self.store, db_path, mirror_dir)
        file_report_mirror = verify_file_standalone(mirror_dir, local_pdf)
        self.assertTrue(file_report_mirror['internally_consistent'])
        self.assertEqual(file_report_mirror['matched_materials'][0]['record_id'], mat_rid)
        self.assertEqual(file_report_mirror['matched_materials'][0]['covering_anchors'][0]['bitcoin']['height'], 971234)

        proof_path = self.root / 'material-proof.json'
        proof_doc = record_chain.record_proof(self.store.db, mat_rid)
        proof_path.write_text(json.dumps(proof_doc), encoding='utf-8')
        file_report_proof = verify_file_standalone(proof_path, local_pdf)
        self.assertTrue(file_report_proof['internally_consistent'])
        self.assertEqual(file_report_proof['matched_materials'][0]['seq'], seq)

        # CLI invocation of tools/eksmu_verify.py verify-file
        cli_proc = subprocess.run(
            [sys.executable, 'tools/eksmu_verify.py', 'verify-file', str(proof_path), str(local_pdf)],
            capture_output=True, text=True, check=True,
        )
        self.assertTrue(json.loads(cli_proc.stdout)['internally_consistent'])

        # 1-byte tampering of the local PDF must fail verification
        tampered_pdf = self.root / 'tampered-paper.pdf'
        tampered_pdf.write_bytes(pdf_bytes + b'X')
        with self.assertRaises(Invalid):
            verify_file_standalone(mirror_dir, tampered_pdf)
        with self.assertRaises(Invalid):
            verify_file_standalone(proof_path, tampered_pdf)

    def test_redact_attachment_preserves_chain_and_backup(self):
        from eksmu.backup import create_backup, verify_backup

        pdf_bytes = b'%PDF-1.4\nPrivate Personal Data\n%%EOF\n'
        att = self.upload('sensitive.pdf', pdf_bytes)
        db_path = self.root / 'data.sqlite'
        self.research.close()
        self.store.close()

        # Commit attachment to record_chain
        rep1 = reconcile_instance(db_path, offline=True)
        self.assertEqual(rep1['added'], 1)

        # Redact attachment via CLI
        redact_proc = subprocess.run(
            [
                sys.executable, '-m', 'eksmu', '--db', str(db_path),
                'redact-attachment',
                '--attachment', att['id'],
                '--reason-code', 'privacy',
                '--reason', 'Contains exposed personal phone number',
                '--operator', 'maintainer_1',
                '--offline',
            ],
            capture_output=True, text=True, check=True,
        )
        redact_out = json.loads(redact_proc.stdout)
        self.assertEqual(redact_out['attachment_id'], att['id'])
        self.assertTrue(redact_out['bytes_removed'])

        # Downloading the redacted attachment raises 410 Problem
        self.store = Store(db_path)
        head_before = record_chain.tip(self.store.db)[1]
        self.research = Research(str(db_path) + '.research.sqlite', self.store.rows())
        with self.assertRaises(Problem) as ctx:
            self.research.download(att['id'])
        self.assertEqual(ctx.exception.status, 410)
        listed = self.research.files(self.revision['id'])['items']
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]['redacted']['reason_code'], 'privacy')

        # Re-running reconcile_instance does not fail and preserves chain head
        self.research.close()
        self.store.close()
        rep2 = reconcile_instance(db_path, offline=True)
        self.assertEqual(rep2['added'], 0)
        self.assertEqual(rep2['already_committed'], 1)

        # Work page renders redaction notice while keeping chain badge
        self.store = Store(db_path)
        self.assertEqual(record_chain.tip(self.store.db)[1], head_before)
        self.research = Research(str(db_path) + '.research.sqlite', self.store.rows())
        exp_id = self.revision['payload']['exploration_id']
        data_redacted = work_page.load(str(db_path), self.store.rows(), {}, exp_id, self.revision['id'], chain_db=self.store.db)
        zh_html = work_page.page(data_redacted, 'zh', 'https://eksmu.com').decode('utf-8')
        self.assertIn('已因合规原因移除：Contains exposed personal phone number', zh_html)
        self.assertIn('记录链 #4（补链）（待锚定）', zh_html)

        # Mirror export includes redactions and passes both verify_mirror and standalone verifier
        mirror_dir = self.root / 'mirror-redacted'
        m_rep = write_local(self.store, db_path, mirror_dir)
        self.assertEqual(m_rep['redactions'], 1)
        standalone_rep = verify_standalone(mirror_dir)
        self.assertEqual(standalone_rep['redactions'], 1)

        # Offline backup and verify-backup succeed even though the redacted blob was deleted
        self.research.close()
        self.store.close()
        with tempfile.TemporaryDirectory() as btmp:
            backup_dir = Path(btmp) / 'backup-redacted'
            b_rep = create_backup(db_path, backup_dir, offline=True)
            self.assertTrue(b_rep['data_verified'])
            v_rep = verify_backup(backup_dir)
            self.assertTrue(v_rep['data_verified'])
        self.store = Store(db_path)
        self.research = Research(str(db_path) + '.research.sqlite', self.store.rows())


if __name__ == '__main__':
    unittest.main()
