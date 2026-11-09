"""GET /api/source and the independent ``eksmu_verify.py verify-source`` check."""
import hashlib
import http.client
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest

import importlib.util

from eksmu import source_attestation
from eksmu.server import make_server

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location('eksmu_verify_source', ROOT / 'tools' / 'eksmu_verify.py')
eksmu_verify = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(eksmu_verify)


def coreutils_tree(root, paths):
    """What `sha256sum $(paths) | LC_ALL=C sort -k2 | sha256sum` yields, recomputed in Python."""
    lines = sorted((f'{hashlib.sha256((root / p).read_bytes()).hexdigest()}  {p}\n' for p in paths),
                   key=lambda line: line.split('  ', 1)[1].encode())
    return hashlib.sha256(''.join(lines).encode()).hexdigest()


class AttestationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        manifest = json.loads((ROOT / source_attestation.MANIFEST).read_text())
        self.paths = sorted(manifest['files'])
        for rel in self.paths + [source_attestation.MANIFEST]:
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / rel, self.root / rel)

    def tearDown(self):
        self.temp.cleanup()

    def test_checkout_matches_its_own_manifest(self):
        value = source_attestation.compute(ROOT)
        self.assertTrue(value['available'])
        self.assertEqual((value['mismatched'], value['missing']), ([], []),
                         'deploy manifest is stale: regenerate deployment/google-cloud/source-manifest.json')
        self.assertEqual(value['tree_sha256'], coreutils_tree(ROOT, self.paths))
        self.assertEqual(value['tree_sha256'], eksmu_verify.source_tree_digest(value['files']))

    def test_drift_is_reported_and_rejected_by_verifier(self):
        target = self.root / self.paths[0]
        target.write_bytes(target.read_bytes() + b'\n# drift\n')
        (self.root / self.paths[1]).unlink()
        value = source_attestation.compute(self.root)
        self.assertEqual(value['mismatched'], [self.paths[0]])
        self.assertEqual(value['missing'], [self.paths[1]])
        doc = self.root / 'attestation.json'
        doc.write_text(json.dumps(value))
        # The checkout (ROOT) no longer matches what this "site" runs.
        with self.assertRaises(eksmu_verify.Invalid):
            eksmu_verify.verify_source(str(doc), str(ROOT))
        # It does match the modified tree it describes, and says the site drifted from its manifest.
        (self.root / self.paths[1]).write_bytes(b'')
        value['files'][self.paths[1]] = hashlib.sha256(b'').hexdigest()
        value['tree_sha256'] = eksmu_verify.source_tree_digest(value['files'])
        doc.write_text(json.dumps(value))
        report = eksmu_verify.verify_source(str(doc), str(self.root))
        self.assertTrue(report['matches_checkout']); self.assertTrue(report['site_reports_drift_from_manifest'])

    def test_verifier_rejects_forged_tree_digest_and_path_escape(self):
        value = source_attestation.compute(ROOT)
        doc = self.root / 'a.json'
        doc.write_text(json.dumps({**value, 'tree_sha256': '0' * 64}))
        with self.assertRaises(eksmu_verify.Invalid):
            eksmu_verify.verify_source(str(doc), str(ROOT))
        files = {'../outside.py': '0' * 64}
        doc.write_text(json.dumps({**value, 'files': files, 'tree_sha256': eksmu_verify.source_tree_digest(files)}))
        with self.assertRaises(eksmu_verify.Invalid):
            eksmu_verify.verify_source(str(doc), str(ROOT))

    def test_missing_manifest_is_explicit(self):
        value = source_attestation.compute(self.root / 'nowhere')
        self.assertEqual((value['available'], value['format']), (False, 'eksmu.source-attestation.v1'))

    @unittest.skipUnless(shutil.which('sha256sum') and shutil.which('sort'), 'coreutils not installed')
    def test_tree_digest_reproducible_with_coreutils(self):
        listing = subprocess.run(['sha256sum', *self.paths], cwd=ROOT, check=True, capture_output=True).stdout
        ordered = subprocess.run(['sort', '-k2'], input=listing, check=True, capture_output=True,
                                 env={'LC_ALL': 'C'}).stdout
        self.assertEqual(hashlib.sha256(ordered).hexdigest(), source_attestation.compute(ROOT)['tree_sha256'])


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = make_server(Path(self.temp.name) / 'community.sqlite', 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(); self.temp.cleanup()

    def call(self, path):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        try:
            conn.request('GET', path, headers={'X-Eksmu-Client': 'local-prototype'})
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def test_endpoint_serves_attestation_that_verifies_against_checkout(self):
        code, body = self.call('/api/source')
        self.assertEqual(code, 200)
        value = json.loads(body)
        self.assertEqual((value['format'], value['license'], value['self_reported']),
                         ('eksmu.source-attestation.v1', 'AGPL-3.0-or-later', True))
        self.assertEqual(self.call('/api/source?x=1')[0], 400)
        report = eksmu_verify.verify_source(f'http://127.0.0.1:{self.server.server_port}', str(ROOT))
        self.assertTrue(report['matches_checkout'])
        self.assertFalse(report['site_reports_drift_from_manifest'])

    def test_descriptor_and_conformance_of_a_fresh_instance(self):
        code, body = self.call('/.well-known/eksmu.json')
        self.assertEqual(code, 200)
        value = json.loads(body)
        self.assertEqual((value['format'], value['protocol']['record_chain']), ('eksmu.instance.v1', 'eksmu.record-chain.v1'))
        self.assertTrue(any('federation' in item for item in value['not_claimed']))
        report = eksmu_verify.conform(f'http://127.0.0.1:{self.server.server_port}')
        self.assertTrue(report['conformant'], report['checks'])
        self.assertEqual([c['ok'] for c in report['checks']], [True, True, True])

    def test_conformance_fails_without_descriptor(self):
        report = eksmu_verify.conform('http://127.0.0.1:1')
        self.assertFalse(report['conformant'])
        self.assertFalse(any(c['ok'] for c in report['checks']))


if __name__ == '__main__':
    unittest.main()
