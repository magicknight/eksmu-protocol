"""Rebuilding an instance's public records from a verified mirror (REBUILD.md)."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# The verifier fixture lives under public-overlay/ in the private repo and under tests/ in the public tree.
FIXTURE = next(p for p in (ROOT / 'public-overlay' / 'tests' / 'fixture-mirror', ROOT / 'tests' / 'fixture-mirror') if p.is_dir())


def cli(db, *args):
    return subprocess.run([sys.executable, '-m', 'eksmu', '--db', str(db), *args], cwd=ROOT,
                          capture_output=True, text=True)


class RebuildTests(unittest.TestCase):
    def test_mirror_rebuilds_the_same_chain_head(self):
        head = json.loads((FIXTURE / 'manifest.json').read_text())['chain']['entries'][-1]['head']
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / 'data' / 'community.sqlite'
            done = cli(db, 'mirror-import', str(FIXTURE))
            self.assertEqual(done.returncode, 0, done.stderr)
            report = json.loads(done.stdout)
            self.assertEqual(report['chain_head'], head)
            self.assertIn('accounts and credentials', report['not_restored'])
            check = cli(db, 'verify-chain')
            self.assertEqual(check.returncode, 0, check.stderr)
            self.assertEqual(json.loads(check.stdout)['head'], head)
            # The target must be empty: a second import is refused, not merged.
            self.assertNotEqual(cli(db, 'mirror-import', str(FIXTURE)).returncode, 0)

    def test_tampered_mirror_is_refused_before_import(self):
        with tempfile.TemporaryDirectory() as temp:
            mirror = Path(temp) / 'mirror'
            mirror.mkdir()
            text = (FIXTURE / 'manifest.json').read_text()
            (mirror / 'manifest.json').write_text(text.replace('"title":"', '"title":"X', 1))
            db = Path(temp) / 'data' / 'community.sqlite'
            self.assertNotEqual(cli(db, 'mirror-import', str(mirror)).returncode, 0)


if __name__ == '__main__':
    unittest.main()
