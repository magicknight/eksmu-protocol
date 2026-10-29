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

    def test_pull_requires_https(self):
        with self.assertRaises(ev.Invalid):
            ev.pull('http://example.com', self.tmp / 'x')


if __name__ == '__main__':
    unittest.main()
