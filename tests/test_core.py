import copy
import hashlib
import tempfile
import unittest
import uuid
from pathlib import Path

from eksmu.store import Problem, Store, canonical, digest


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "community.sqlite"
        self.store = Store(self.path)
        self.alice = self.store.register("初学者")
        self.bob = self.store.register("另一位探索者")
        self.key = self.alice["token"]
        self.other = self.bob["token"]

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def work(self):
        return self.store.publish(self.key, "只有一个问题，暂时没有证明。", "一个普通问题")

    def report(self, rev, status="challenged", source="human"):
        return self.store.evidence(self.other, rev, source, "第一个推论", "逐步核对", "这里仍有疑问", status)

    def target(self):
        return Store(Path(self.temp.name) / (str(uuid.uuid4()) + ".sqlite"))

    def reject_import(self, bundle):
        target = self.target()
        try:
            with self.assertRaises(Problem):
                target.import_bundle(bundle)
            self.assertEqual(target.rows(), [])
        finally:
            target.close()

    def test_publish_without_credentials_of_expertise_or_evaluation(self):
        work = self.work()
        self.assertEqual(work["revision"]["payload"]["body"], "只有一个问题，暂时没有证明。")
        self.assertEqual(len(self.store.rows()), 4)
        self.assertEqual(set(self.alice["actor"]["payload"]), {"alias", "key_hash"})

    def test_failed_attempt_can_be_published(self):
        work = self.store.publish(self.key, "这个计算失败了", kind="failed_attempt")
        self.assertEqual(work["exploration"]["payload"]["kind"], "failed_attempt")

    def test_untitled_publication_is_named_after_its_opening_words(self):
        work = self.store.publish(self.key, "# 为什么 *素数* 无穷多？\n\n正文")
        self.assertEqual(work["revision"]["payload"]["title"], "为什么 素数 无穷多？")
        long = self.store.publish(self.key, "长" * 300)
        self.assertEqual(len(long["revision"]["payload"]["title"]), 80)
        self.assertEqual(self.store.publish(self.key, "正文", "  ")["revision"]["payload"]["title"], "正文")

    def test_criticism_of_founder_or_sponsor_can_be_published(self):
        work = self.store.publish(self.other, "我不同意创办者、资助者和 AI 的意见。")
        self.assertIn("不同意", work["revision"]["payload"]["body"])

    def test_opposed_reports_coexist_and_do_not_remove_work(self):
        work = self.work()
        self.report(work["revision"]["id"], "challenged", "ai")
        self.report(work["revision"]["id"], "supported", "human")
        self.assertIn(work["exploration"], self.store.rows())
        self.assertEqual([r["payload"]["status"] for r in self.store.rows() if r["type"] == "evidence"], ["challenged", "supported"])
        self.store.publish(self.key, "即使有负面报告，也可以继续发表。")

    def test_only_original_author_can_revise(self):
        work = self.work(); before = self.store.rows()
        with self.assertRaises(Problem) as caught:
            self.store.revise(self.other, work["exploration"]["id"], work["revision"]["id"], "篡改", "不允许")
        self.assertEqual(caught.exception.status, 403)
        self.assertEqual(self.store.rows(), before)

    def test_stale_revision_rejected_without_losing_latest(self):
        work = self.work()
        rev = self.store.revise(self.key, work["exploration"]["id"], work["revision"]["id"], "第二版", "修改")
        with self.assertRaises(Problem) as caught:
            self.store.revise(self.key, work["exploration"]["id"], work["revision"]["id"], "旧窗口", "不覆盖")
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.store.rows()[-1], rev)

    def test_old_version_and_its_comments_remain_unchanged(self):
        work = self.work()
        comment = self.store.comment(self.other, work["revision"]["id"], "我还不理解这一步。")
        self.store.revise(self.key, work["exploration"]["id"], work["revision"]["id"], "第二版", "补充说明")
        self.assertIn(work["revision"], self.store.rows())
        self.assertIn(comment, self.store.rows())

    def test_reports_can_be_replied_to(self):
        rev = self.work()["revision"]["id"]
        report = self.report(rev)
        reply = self.store.comment(self.key, rev, "我对这个报告有不同意见。", report["id"])
        self.assertEqual(reply["payload"]["reply_to"], report["id"])

    def test_reply_cannot_cross_version_boundary(self):
        first, second = self.work(), self.work()
        report = self.report(first["revision"]["id"])
        with self.assertRaises(Problem):
            self.store.comment(self.key, second["revision"]["id"], "错误关联", report["id"])

    def test_lean_label_never_becomes_service_verification(self):
        rev = self.work()["revision"]["id"]
        report = self.report(rev, "supported", "lean")
        self.assertIs(report["payload"]["service_verified"], False)
        with self.assertRaises(Problem):
            self.report(rev, "proof_checked", "lean")

    def test_untrusted_text_is_stored_not_executed(self):
        marker = Path(self.temp.name) / "should-not-exist"
        text = f"__import__('pathlib').Path({str(marker)!r}).touch()"
        self.store.publish(self.key, text)
        self.assertFalse(marker.exists())  # There is no execution service in this milestone.

    def test_bad_identity_key_cannot_publish(self):
        with self.assertRaises(Problem) as caught:
            self.store.publish("not-a-key", "text")
        self.assertEqual(caught.exception.status, 401)

    def test_oversized_body_is_atomic(self):
        before = self.store.rows()
        with self.assertRaises(Problem):
            self.store.publish(self.key, "x" * 50_001)
        self.assertEqual(self.store.rows(), before)

    def test_bad_unicode_is_rejected(self):
        with self.assertRaises(Problem):
            self.store.publish(self.key, "\ud800")

    def test_wrong_enum_type_is_rejected(self):
        with self.assertRaises(Problem):
            self.store.publish(self.key, "test", kind=["question"])

    def test_export_does_not_contain_identity_keys(self):
        self.work()
        raw = canonical(self.store.export())
        self.assertNotIn(self.key, raw)
        self.assertNotIn(self.other, raw)
        self.assertIn("key_hash", raw)  # Public verifier only, not the random secret.

    def test_exact_roundtrip_and_original_author_can_continue(self):
        work = self.work()
        report = self.report(work["revision"]["id"])
        self.store.comment(self.key, work["revision"]["id"], "回应", report["id"])
        bundle = self.store.export()
        target = self.target()
        try:
            target.import_bundle(bundle)
            self.assertEqual(bundle, target.export())
            self.assertEqual(target.authenticate(self.key), self.alice["actor"]["id"])
            target.revise(self.key, work["exploration"]["id"], work["revision"]["id"], "迁移后", "继续探索")
        finally:
            target.close()

    def test_import_does_not_overwrite_nonempty_database(self):
        self.work(); before = self.store.export()
        with self.assertRaises(Problem) as caught:
            self.store.import_bundle(before)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.store.export(), before)

    def test_bundle_checksum_tampering_rejected(self):
        bundle = self.store.export(); bundle["sha256"] = "0" * 64
        self.reject_import(bundle)

    def test_record_tamper_even_with_updated_bundle_digest_rejected(self):
        self.work(); bundle = self.store.export()
        bundle["records"][-1]["payload"]["body"] = "tampered"
        bundle["sha256"] = digest(bundle["records"])
        self.reject_import(bundle)

    def test_dangling_reference_even_with_recomputed_digests_rejected(self):
        work = self.work(); self.report(work["revision"]["id"])
        bundle = self.store.export(); r = bundle["records"][-1]
        r["payload"]["revision_id"] = str(uuid.uuid4())
        r["sha256"] = digest({k:v for k,v in r.items() if k != "sha256"})
        bundle["sha256"] = digest(bundle["records"])
        self.reject_import(bundle)

    def test_claimed_service_verification_cannot_be_imported(self):
        work = self.work(); self.report(work["revision"]["id"])
        bundle = self.store.export(); r = bundle["records"][-1]
        r["payload"]["service_verified"] = True
        r["sha256"] = digest({k:v for k,v in r.items() if k != "sha256"})
        bundle["sha256"] = digest(bundle["records"])
        self.reject_import(bundle)

    def test_nonlist_records_rejected(self):
        self.reject_import({"format":"eksmu.bundle.v1", "records":{}, "sha256":digest({})})

    def test_unknown_bundle_fields_rejected(self):
        bundle = self.store.export(); bundle["admin"] = True
        self.reject_import(bundle)

    def test_duplicate_records_rejected(self):
        bundle = self.store.export(); bundle["records"].append(copy.deepcopy(bundle["records"][0]))
        bundle["sha256"] = digest(bundle["records"])
        self.reject_import(bundle)

    def test_checksum_is_not_a_signature(self):
        bundle = self.store.export(); actor = bundle["records"][0]
        actor["payload"]["alias"] = "修改过的显示名"
        actor["sha256"] = digest({k:v for k,v in actor.items() if k != "sha256"})
        bundle["sha256"] = digest(bundle["records"])
        target = self.target()
        try:
            target.import_bundle(bundle)  # A digest is recomputable: source trust is separate.
            self.assertEqual(target.rows()[0]["payload"]["alias"], "修改过的显示名")
        finally:
            target.close()

    def test_data_survives_reopening(self):
        self.work(); before = self.store.export()
        self.store.close(); self.store = Store(self.path)
        self.assertEqual(self.store.export(), before)


if __name__ == "__main__":
    unittest.main()
