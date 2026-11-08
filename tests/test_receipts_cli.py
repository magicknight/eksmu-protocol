import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

from eksmu_receipts.core import load

ROOT = Path(__file__).resolve().parents[1]


class CLI(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.private, self.public = self.work / "verifier.key", self.work / "verifier.pub"
        self.request, self.receipt = self.work / "request.json", self.work / "receipt.json"

    def invoke(self, *args, success=True):
        run = subprocess.run([sys.executable, "-m", "eksmu_receipts", *map(str, args)], cwd=ROOT, text=True, capture_output=True, timeout=10)
        self.assertEqual(run.returncode == 0, success, run.stdout + run.stderr)
        return run

    def prepare(self, consent=True):
        args = ["prepare", "--input", ROOT / "examples/rational-equality.json", "--revision-id", str(uuid.uuid4()),
                "--revision-sha256", "aa" * 32, "--scope", "Only this explicit arithmetic equality", "--out", self.request]
        if consent: args.append("--allow-local-compute")
        return self.invoke(*args)

    def test_full_offline_roundtrip(self):
        self.invoke("keygen", "--private", self.private, "--public", self.public)
        self.prepare()
        self.invoke("run", "--request", self.request, "--private", self.private, "--out", self.receipt, "--allow-local-compute")
        result = self.invoke("verify", "--request", self.request, "--receipt", self.receipt, "--public", self.public)
        self.assertTrue(json.loads(result.stdout)["replay_matches"])
        self.assertEqual(self.private.stat().st_mode & 0o777, 0o600)

    def test_key_overwrite_refused(self):
        self.invoke("keygen", "--private", self.private, "--public", self.public)
        old = self.private.read_bytes()
        self.invoke("keygen", "--private", self.private, "--public", self.public, success=False)
        self.assertEqual(old, self.private.read_bytes())

    def test_record_only_no_output(self):
        self.invoke("keygen", "--private", self.private, "--public", self.public)
        self.prepare(False)
        self.invoke("run", "--request", self.request, "--private", self.private, "--out", self.receipt, "--allow-local-compute", success=False)
        self.assertFalse(self.receipt.exists())

    def test_missing_operator_consent(self):
        self.invoke("keygen", "--private", self.private, "--public", self.public)
        self.prepare()
        self.invoke("run", "--request", self.request, "--private", self.private, "--out", self.receipt, success=False)
        self.assertFalse(self.receipt.exists())

    def test_request_no_overwrite(self):
        self.prepare(); before = self.request.read_bytes()
        self.invoke("prepare", "--input", ROOT / "examples/rational-equality.json", "--revision-id", str(uuid.uuid4()),
                    "--revision-sha256", "bb" * 32, "--scope", "test", "--out", self.request, success=False)
        self.assertEqual(self.request.read_bytes(), before)

    def test_unknown_key_fails(self):
        self.invoke("keygen", "--private", self.private, "--public", self.public)
        self.prepare()
        self.invoke("run", "--request", self.request, "--private", self.private, "--out", self.receipt, "--allow-local-compute")
        self.invoke("keygen", "--private", self.work / "another.key", "--public", self.work / "another.pub")
        self.invoke("verify", "--request", self.request, "--receipt", self.receipt, "--public", self.work / "another.pub", success=False)
