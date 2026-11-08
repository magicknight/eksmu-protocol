"""Run with PYTHONPATH pointing to the separately supplied v0.2 baseline.

This explicitly is NOT an integration test of unavailable v0.3 A/B/C source.
"""
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from eksmu.store import Store
from eksmu_receipts.core import PendingRun, ReceiptBook, make_request, public_hex, verify


def rational(n, d=1):
    return {"op": "rational", "numerator": str(n), "denominator": str(d)}


class PublicationBoundary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.db = Store(Path(self.tmp.name) / "community.sqlite"); self.addCleanup(self.db.close)
        self.author = self.db.register("无机构的新参与者")
        self.work = self.db.publish(self.author["token"], "我发现了一个等式，欢迎任何人检查。")
        rev = self.work["revision"]
        self.req = make_request(rev["id"], rev["sha256"], "Only the supplied closed equality",
                                {"left": rational(1, 3), "right": rational(1, 2)}, allow_local=True)
        self.key = Ed25519PrivateKey.generate()

    def receipt(self):
        return PendingRun(self.req).execute(self.key, allow_local_compute=True)

    def test_verifier_never_mutates_community(self):
        before = self.db.export()
        self.receipt()
        self.assertEqual(before, self.db.export())

    def test_failed_check_does_not_prevent_publication(self):
        self.assertEqual(self.receipt()["body"]["result"]["status"], "failed")
        post = self.db.publish(self.author["token"], "失败后仍可继续发表。")
        self.assertEqual(post["exploration"]["actor_id"], self.author["actor"]["id"])

    def test_cancel_does_not_prevent_comments_or_revision(self):
        task = PendingRun(self.req); task.cancel()
        critic = self.db.register("质疑者")
        self.db.comment(critic["token"], self.work["revision"]["id"], "我的批评不会因为取消检查而消失。")
        self.db.revise(self.author["token"], self.work["exploration"]["id"], self.work["revision"]["id"], "修订", "继续探索")
        self.assertEqual(len([r for r in self.db.rows() if r["type"] == "comment"]), 1)

    def test_report_label_does_not_become_verified_receipt(self):
        row = self.db.evidence(self.author["token"], self.work["revision"]["id"], "lean", "全部", "自述", "通过", status="supported")
        self.assertFalse(row["payload"]["service_verified"])
        from eksmu_receipts.core import Rejected
        with self.assertRaises(Rejected): verify(row, self.req, public_hex(self.key))

    def test_backup_and_receipt_survive_independently(self):
        receipt = self.receipt()
        backup = self.db.export()
        target = Store(Path(self.tmp.name) / "second.sqlite")
        try:
            target.import_bundle(backup)
            target.revise(self.author["token"], self.work["exploration"]["id"], self.work["revision"]["id"], "迁移后修订", "不依赖验证器继续存在")
            self.assertTrue(verify(receipt, self.req, public_hex(self.key))["replay_matches"])
            self.assertEqual(receipt["body"]["target"]["revision_id"], self.work["revision"]["id"])
        finally:
            target.close()
