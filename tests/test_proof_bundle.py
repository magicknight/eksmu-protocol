import base64
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from eksmu.mirror import write_local
from eksmu import record_chain
from eksmu.store import Store
from tools.proof_bundle import create, verify


class ProofBundleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db = root / 'test.sqlite'
        self.store = Store(self.db)
        actor = self.store.register('Secret name should not appear')
        self.store.publish(actor['token'], 'secret research body', 'secret title')
        seq, head = record_chain.tip(self.store.db)
        with self.store.db:
            self.store.db.execute("INSERT INTO chain_anchors(seq,head,method,submitted_at,state,proof,updated_at) VALUES(?,?,'opentimestamps','t','pending',?,'t')", (seq, head, b'opaque secret proof bytes'))
        self.source = root / 'mirror'
        write_local(self.store, self.db, self.source)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_minimal_and_honest(self):
        bundle = create(self.source)
        serialized = json.dumps(bundle)
        self.assertNotIn('secret', serialized)
        self.assertNotIn(base64.b64encode(b'opaque secret proof bytes').decode(), serialized)
        self.assertEqual(bundle['anchors'][0]['proof_sha256'], hashlib.sha256(b'opaque secret proof bytes').hexdigest())
        report = verify(bundle, (len(bundle['entries']), bundle['entries'][-1]['head']))
        self.assertEqual(report['anchors_bound_to_chain'], 1)
        self.assertTrue(report['checkpoint_matched'])
        self.assertFalse(report['bitcoin_verified'])
        self.assertFalse(report['proofs_verified'])
        self.assertFalse(report['records_verified'])
        self.assertFalse(report['proof_bytes_included'])

    def test_tamper_anchor_and_record_hash_rejected(self):
        bundle = create(self.source)
        changed = copy.deepcopy(bundle)
        changed['anchors'][0]['head'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'anchor does not match'):
            verify(changed)
        changed = copy.deepcopy(bundle)
        changed['entries'][0]['record_sha256'] = 'a' * 64
        with self.assertRaisesRegex(ValueError, 'chain mismatch'):
            verify(changed)

    def test_independent_checkpoint_detects_truncation(self):
        bundle = create(self.source)
        cp = (len(bundle['entries']), bundle['entries'][-1]['head'])
        bundle['entries'].pop()
        bundle['anchors'] = []
        with self.assertRaisesRegex(ValueError, 'truncated checkpoint'):
            verify(bundle, cp)

    def test_source_tamper_rejected(self):
        manifest_file = self.source / 'manifest.json'
        manifest = json.loads(manifest_file.read_bytes())
        manifest['chain']['entries'][0]['head'] = 'a' * 64
        manifest_file.write_text(json.dumps(manifest))
        with self.assertRaises(ValueError):
            create(self.source)

    def test_cli_creates_once_and_verifies(self):
        target = Path(self.tmp.name) / 'witness.json'
        script = Path(__file__).resolve().parents[1] / 'tools' / 'proof_bundle.py'
        make = [sys.executable, str(script), 'create', str(self.source), str(target)]
        self.assertEqual(subprocess.run(make, capture_output=True).returncode, 0)
        out = subprocess.run([sys.executable, str(script), 'verify', str(target)], capture_output=True)
        self.assertEqual(out.returncode, 0)
        self.assertFalse(json.loads(out.stdout)['bitcoin_verified'])
        self.assertNotEqual(subprocess.run(make, capture_output=True).returncode, 0)


if __name__ == '__main__':
    unittest.main()
