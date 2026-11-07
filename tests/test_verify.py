"""Run with: python3 -m unittest discover tests"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import eksmu_verify as ev  # noqa: E402

FIXTURE = ROOT / 'tests' / 'fixture-mirror'


class VerifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.mirror = self.tmp / 'm'
        shutil.copytree(FIXTURE, self.mirror)
        self.manifest = json.loads((self.mirror / 'manifest.json').read_text(encoding='utf-8'))

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def save(self):
        (self.mirror / 'manifest.json').write_text(ev.canonical(self.manifest), encoding='utf-8')

    def revision(self):
        return next(r for r in self.manifest['records']['records'] if r['type'] == 'revision')

    def test_fixture_verifies(self):
        report = ev.verify(self.mirror)
        self.assertTrue(report['internally_consistent'])
        self.assertTrue(report['complete'])
        self.assertEqual(report['snapshots']['verified'], 1)
        self.assertEqual(len(report['withdrawn_works']), 1)
        self.assertEqual(len(report['not_verified']), 5)

    def test_chain_fold_vector(self):
        import hashlib
        self.assertEqual(ev.fold('00' * 32, '00' * 32),
                         hashlib.sha256(b'eksmu.record-chain.v1\x00' + bytes(64)).hexdigest())
        self.assertEqual(ev.fold('00' * 32, '00' * 32, 'oic'),
                         hashlib.sha256(b'oic.record-chain.v1\x00' + bytes(64)).hexdigest())

    def test_pre_rename_mirror_still_verifies(self):
        report = ev.verify(FIXTURE.parent / 'fixture-mirror-oic')
        self.assertTrue(report['internally_consistent'])
        self.assertEqual(report['snapshots']['verified'], 1)

    def test_mixed_generation_prefixes_fail(self):
        self.manifest['chain']['format'] = 'oic.record-chain.v1'
        self.save()
        with self.assertRaisesRegex(ev.Invalid, 'chain'):
            ev.verify(self.mirror)

    def test_edited_text_fails(self):
        self.revision()['payload']['body'] = 'tampered'
        self.save()
        with self.assertRaises(ev.Invalid):
            ev.verify(self.mirror)

    def test_rehashed_edit_breaks_chain(self):
        rev = self.revision()
        rev['payload']['body'] = 'tampered'
        rev['sha256'] = ev.digest({k: v for k, v in rev.items() if k != 'sha256'})
        self.manifest['records']['sha256'] = ev.digest(self.manifest['records']['records'])
        self.save()
        with self.assertRaisesRegex(ev.Invalid, 'chain'):
            ev.verify(self.mirror)

    def test_removed_record_fails(self):
        del self.manifest['records']['records'][-1]
        self.manifest['records']['sha256'] = ev.digest(self.manifest['records']['records'])
        self.save()
        with self.assertRaises(ev.Invalid):
            ev.verify(self.mirror)

    def test_foreign_revision_fails(self):
        rows = self.manifest['records']['records']
        reader = [r for r in rows if r['type'] == 'actor'][1]
        rev = self.revision()
        rev['actor_id'] = reader['id']
        rev['sha256'] = ev.digest({k: v for k, v in rev.items() if k != 'sha256'})
        self.manifest['records']['sha256'] = ev.digest(rows)
        self.save()
        with self.assertRaisesRegex(ev.Invalid, 'author'):
            ev.verify(self.mirror)

    def test_anchor_must_bind(self):
        self.manifest['anchors'][0]['head'] = 'ab' * 32
        self.save()
        with self.assertRaisesRegex(ev.Invalid, 'anchor'):
            ev.verify(self.mirror)

    def test_altered_material_fails_and_missing_package_is_reported(self):
        package_path = next((self.mirror / 'publications').glob('*.json'))
        package = json.loads(package_path.read_text(encoding='utf-8'))
        package['blobs'][0]['data_b64'] = 'b3RoZXI='
        package_path.write_text(ev.canonical(package), encoding='utf-8')
        with self.assertRaises(ev.Invalid):
            ev.verify(self.mirror)
        package_path.unlink()
        report = ev.verify(self.mirror)
        self.assertFalse(report['complete'])
        self.assertEqual(len(report['snapshots']['missing']), 1)

    def test_hidden_withdrawal_breaks_notice_history(self):
        self.manifest['publications']['events'][0]['previous_event_id'] = '00000000-0000-4000-8000-000000000000'
        self.save()
        with self.assertRaises(ev.Invalid):
            ev.verify(self.mirror)

    def add_material(self):
        import uuid
        rows = self.manifest['records']['records']
        rev = self.revision()
        row = {'id': str(uuid.uuid4()), 'type': 'material', 'actor_id': rev['actor_id'],
               'created_at': '2026-11-06T00:00:00+00:00',
               'payload': {'attachment_id': str(uuid.uuid4()), 'revision_id': rev['id'],
                           'size': 10, 'content_sha256': 'ab' * 32, 'origin': 'upload',
                           'derived_from_sha256': None}}
        row['sha256'] = ev.digest(row)
        rows.append(row)
        self.manifest['records']['sha256'] = ev.digest(rows)
        entries = self.manifest['chain']['entries']
        entries.append({'seq': len(entries) + 1, 'record_id': row['id'],
                        'record_sha256': row['sha256'],
                        'head': ev.fold(entries[-1]['head'], row['sha256'], 'eksmu'),
                        'chained_at': row['created_at'], 'backfilled': True})
        self.manifest['format'] = 'eksmu.public-mirror.v2'
        return row

    def test_experimental_v2_material_and_v1_rejection(self):
        self.add_material()
        self.save()
        self.assertTrue(ev.verify(self.mirror)['internally_consistent'])
        self.manifest['format'] = 'eksmu.public-mirror.v1'
        self.save()
        with self.assertRaisesRegex(ev.Invalid, 'require mirror v2'):
            ev.verify(self.mirror)

    def test_experimental_material_tampering(self):
        row = self.add_material()
        row['payload']['content_sha256'] = 'cd' * 32
        row['sha256'] = ev.digest({k: v for k, v in row.items() if k != 'sha256'})
        self.manifest['records']['sha256'] = ev.digest(self.manifest['records']['records'])
        self.save()
        with self.assertRaisesRegex(ev.Invalid, 'chain'):
            ev.verify(self.mirror)

    def test_experimental_material_must_match_author(self):
        row = self.add_material()
        row['actor_id'] = next(r['id'] for r in self.manifest['records']['records']
                               if r['type'] == 'actor' and r['id'] != row['actor_id'])
        row['sha256'] = ev.digest({k: v for k, v in row.items() if k != 'sha256'})
        self.manifest['records']['sha256'] = ev.digest(self.manifest['records']['records'])
        self.save()
        with self.assertRaisesRegex(ev.Invalid, 'owner mismatch'):
            ev.verify(self.mirror)

    def test_eksmu_v1_and_v2_minimal_mirrors(self):
        import uuid
        actor_id, work_id, revision_id = (str(uuid.uuid4()) for _ in range(3))
        def rec(rid, kind, actor, payload):
            value = {'id': rid, 'type': kind, 'actor_id': actor,
                     'created_at': '2026-11-06T00:00:00+00:00', 'payload': payload}
            value['sha256'] = ev.digest(value)
            return value
        rows = [rec(actor_id, 'actor', actor_id, {'alias': 'A', 'key_hash': '00' * 32}),
                rec(work_id, 'exploration', actor_id, {'kind': 'question'}),
                rec(revision_id, 'revision', actor_id, {'exploration_id': work_id, 'parent_revision_id': None,
                    'title': 'T', 'body': 'B', 'note': ''})]
        self.manifest = {'format': 'eksmu.public-mirror.v1', 'generated_at': '', 'origin': None,
            'records': {'format': 'eksmu.bundle.v1', 'records': rows, 'sha256': ev.digest(rows)},
            'chain': {'format': 'eksmu.record-chain.v1', 'entries': []}, 'anchors': [],
            'publications': {'snapshots': [], 'events': []}}
        head = '00' * 32
        for i, row in enumerate(rows, 1):
            head = ev.fold(head, row['sha256'], 'eksmu')
            self.manifest['chain']['entries'].append({'seq': i, 'record_id': row['id'],
                'record_sha256': row['sha256'], 'head': head, 'backfilled': False})
        self.save()
        self.assertTrue(ev.verify(self.mirror)['internally_consistent'])
        material = rec(str(uuid.uuid4()), 'material', actor_id, {'attachment_id': str(uuid.uuid4()),
            'revision_id': revision_id, 'size': 1, 'content_sha256': 'ab' * 32,
            'origin': 'upload', 'derived_from_sha256': None})
        rows.append(material)
        self.manifest['records']['sha256'] = ev.digest(rows)
        self.manifest['chain']['entries'].append({'seq': 4, 'record_id': material['id'],
            'record_sha256': material['sha256'], 'head': ev.fold(head, material['sha256'], 'eksmu'),
            'backfilled': True})
        self.manifest['format'] = 'eksmu.public-mirror.v2'
        self.save()
        self.assertTrue(ev.verify(self.mirror)['internally_consistent'])

    def test_pull_requires_https(self):
        with self.assertRaises(ev.Invalid):
            ev.pull('http://example.com', self.tmp / 'x')

    def test_verify_file_against_mirror_and_record_proof(self):
        import hashlib
        raw = b'%PDF-1.4 test-attachment-bytes\n'
        sha = hashlib.sha256(raw).hexdigest()
        row = self.add_material()
        row['payload']['content_sha256'] = sha
        row['payload']['size'] = len(raw)
        row['sha256'] = ev.digest({k: v for k, v in row.items() if k != 'sha256'})
        self.manifest['records']['sha256'] = ev.digest(self.manifest['records']['records'])
        entries = self.manifest['chain']['entries']
        entries[-1]['record_sha256'] = row['sha256']
        entries[-1]['head'] = ev.fold(entries[-2]['head'], row['sha256'], 'eksmu')
        self.save()
        f = self.tmp / 'paper.pdf'
        f.write_bytes(raw)
        rep = ev.verify_file(self.mirror, f)
        self.assertTrue(rep['internally_consistent'])
        self.assertEqual(rep['matched_materials'][0]['record_id'], row['id'])
        proof_file = self.tmp / 'proof.json'
        proof_file.write_text(json.dumps({
            'format': 'eksmu.record-proof.v1',
            'record': row,
            'seq': entries[-1]['seq'],
            'backfilled': True,
            'previous_head': entries[-2]['head'],
            'following_record_sha256': [],
            'anchor': {'id': 9, 'seq': entries[-1]['seq'], 'head': entries[-1]['head'],
                       'state': 'confirmed', 'bitcoin': {'height': 971234}},
        }), encoding='utf-8')
        rep2 = ev.verify_file(proof_file, f)
        self.assertTrue(rep2['internally_consistent'])
        self.assertEqual(rep2['matched_materials'][0]['covering_anchors'][0]['bitcoin']['height'], 971234)
        f.write_bytes(raw + b'!')
        with self.assertRaises(ev.Invalid):
            ev.verify_file(self.mirror, f)
        with self.assertRaises(ev.Invalid):
            ev.verify_file(proof_file, f)


if __name__ == '__main__':
    unittest.main()
