import copy
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid

from verification_lab.__main__ import demonstration, example_revision
from verification_lab.protocol import (
    AUDIT_SCHEMA, BUDGET, LOG, OUTCOMES, POLICY, Lab, Rejected,
    canonical, read_v1_revision, revision_binding, run_fixture, sha, sign_fixture,
)


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "lab.sqlite"
        self.keys = {"fixture.a": secrets.token_bytes(32), "fixture.b": secrets.token_bytes(32)}
        self.lab = Lab(self.path, self.keys)
        self.revision = example_revision()
        self.prepared = self.lab.prepare(self.revision, "Check only a named subclaim", "lean_check")
        self.task = self.prepared["task"]["id"]
        self.control = self.prepared["control_key"]

    def tearDown(self):
        self.lab.close()
        self.directory.cleanup()

    def grant(self, verifier="fixture.a"):
        return self.lab.authorize(self.task, self.control, verifier)

    def packet(self, grant=None, outcome="check_completed"):
        grant = self.grant() if grant is None else grant
        ticket = self.lab.begin(grant, "fixture.a", self.keys["fixture.a"])
        return sign_fixture(ticket, self.keys["fixture.a"], outcome)

    def resign(self, packet, worker="fixture.a"):
        packet["hmac_sha256"] = hmac.new(self.keys[worker], canonical(packet["body"]), "sha256").hexdigest()
        return packet

    def test_preparation_is_not_execution_or_consent(self):
        audit = self.lab.export()
        self.assertEqual(audit["grants"], [])
        self.assertEqual(audit["tasks"][0]["task"]["policy"], POLICY)
        self.assertEqual(self.prepared["task"]["revision_id"], self.revision["id"])

    def test_only_task_controller_authorizes(self):
        before = self.lab.export()
        with self.assertRaises(Rejected):
            self.lab.authorize(self.task, "wrong", "fixture.a")
        self.assertEqual(before, self.lab.export())

    def test_worker_key_does_not_control_tasks(self):
        with self.assertRaises(Rejected):
            self.lab.cancel(self.task, self.keys["fixture.a"].hex())
        self.assertIsNone(self.lab.export()["tasks"][0]["cancelled_at"])

    def test_control_key_is_scoped_to_one_task(self):
        second = self.lab.prepare(self.revision, "Other scope", "ai_review")
        with self.assertRaises(Rejected):
            self.lab.authorize(second["task"]["id"], self.control, "fixture.a")

    def test_no_grant_no_execution(self):
        with self.assertRaises(Rejected):
            self.lab.begin(str(uuid.uuid4()), "fixture.a", self.keys["fixture.a"])

    def test_wrong_worker_cannot_claim(self):
        grant = self.grant()
        with self.assertRaises(Rejected):
            self.lab.begin(grant, "fixture.b", self.keys["fixture.b"])
        self.assertIsNone(self.lab.export()["grants"][0]["ticket"])

    def test_wrong_worker_secret_cannot_claim(self):
        with self.assertRaises(Rejected):
            self.lab.begin(self.grant(), "fixture.a", b"x" * 32)

    def test_task_control_is_not_a_worker_credential(self):
        with self.assertRaises(Rejected):
            self.lab.begin(self.grant(), "fixture.a", self.control.encode())

    def test_two_verifiers_and_disagreeing_outcomes_coexist(self):
        for worker, outcome in [("fixture.a", "check_completed"), ("fixture.b", "check_failed")]:
            run_fixture(self.lab, self.grant(worker), worker, self.keys[worker], outcome)
        self.assertEqual([g["receipt"]["body"]["outcome"] for g in self.lab.export()["grants"]],
                         ["check_completed", "check_failed"])

    def test_begin_retries_have_one_attempt(self):
        grant = self.grant()
        first = self.lab.begin(grant, "fixture.a", self.keys["fixture.a"])
        self.assertEqual(first, self.lab.begin(grant, "fixture.a", self.keys["fixture.a"]))

    def test_same_receipt_retry_is_idempotent(self):
        packet = self.packet()
        first = self.lab.finish(packet)
        before = self.lab.export()
        self.assertEqual(first, self.lab.finish(copy.deepcopy(packet)))
        self.assertEqual(before, self.lab.export())

    def test_conflicting_retry_cannot_overwrite(self):
        packet = self.packet()
        self.lab.finish(packet)
        packet["body"]["outcome"] = "execution_error"
        with self.assertRaises(Rejected):
            self.lab.finish(self.resign(packet))
        self.assertEqual(self.lab.export()["grants"][0]["receipt"]["body"]["outcome"], "check_completed")

    def test_new_grant_allows_explicit_retry(self):
        run_fixture(self.lab, self.grant(), "fixture.a", self.keys["fixture.a"], "execution_error")
        run_fixture(self.lab, self.grant(), "fixture.a", self.keys["fixture.a"], "check_completed")
        self.assertEqual(len(self.lab.export()["grants"]), 2)

    def test_unknown_outcome_is_rejected(self):
        with self.assertRaises(Rejected):
            sign_fixture({}, self.keys["fixture.a"], "entire_theory_proven")

    def test_all_result_categories_remain_simulations(self):
        for outcome in sorted(OUTCOMES):
            with self.subTest(outcome=outcome):
                run_fixture(self.lab, self.grant(), "fixture.a", self.keys["fixture.a"], outcome)
        for grant in self.lab.export()["grants"]:
            self.assertIs(grant["receipt"]["body"]["simulation"], True)
            self.assertIs(grant["receipt"]["body"]["academic_verification"], False)

    def test_forged_receipt_is_rejected_even_after_rehash(self):
        packet = self.packet()
        packet["body"]["outcome"] = "check_failed"
        packet["hmac_sha256"] = sha(packet["body"])
        with self.assertRaises(Rejected):
            self.lab.finish(packet)
        self.assertIsNone(self.lab.export()["grants"][0]["receipt"])

    def test_recomputed_authentication_cannot_escalate_rights(self):
        for field, value in [("academic_verification", True), ("simulation", False),
                             ("publication_write", True), ("funds", 100), ("tool", "Lean")]:
            with self.subTest(field=field):
                packet = self.packet()
                packet["body"][field] = value
                with self.assertRaises(Rejected):
                    self.lab.finish(self.resign(packet))

    def test_malformed_authentication_tags_fail_cleanly(self):
        packet = self.packet()
        for tag in [None, {}, 1, "x" * 64, "😀" * 64, "a" * 63]:
            with self.subTest(tag=tag):
                packet["hmac_sha256"] = tag
                with self.assertRaises(Rejected):
                    self.lab.finish(packet)

    def test_receipt_binding_cannot_change_even_with_valid_mac(self):
        for field in ["attempt_id", "grant_id", "task_id", "revision_id", "revision_sha256", "task_sha256", "started_at"]:
            with self.subTest(field=field):
                packet = self.packet()
                packet["body"]["ticket"][field] = str(uuid.uuid4())
                with self.assertRaises(Rejected):
                    self.lab.finish(self.resign(packet))

    def test_log_digest_and_tool_cannot_be_faked(self):
        for field in ["log", "log_sha256", "schema"]:
            with self.subTest(field=field):
                packet = self.packet()
                packet["body"][field] = "other"
                with self.assertRaises(Rejected):
                    self.lab.finish(self.resign(packet))

    def test_unstarted_grant_rejects_signed_receipt(self):
        packet = self.packet()
        packet["body"]["ticket"]["grant_id"] = self.grant()
        with self.assertRaises(Rejected):
            self.lab.finish(self.resign(packet))

    def test_cancel_before_start_prevents_execution(self):
        grant = self.grant()
        self.lab.cancel(self.task, self.control)
        with self.assertRaises(Rejected):
            self.lab.begin(grant, "fixture.a", self.keys["fixture.a"])
        with self.assertRaises(Rejected):
            self.grant()

    def test_cancel_is_idempotent(self):
        first = self.lab.cancel(self.task, self.control)
        self.assertEqual(first, self.lab.cancel(self.task, self.control))

    def test_cancellation_preserves_existing_and_late_receipts(self):
        self.lab.finish(self.packet())
        inflight = self.packet(outcome="resource_limited")
        self.lab.cancel(self.task, self.control)
        acceptance = self.lab.finish(inflight)
        self.assertTrue(acceptance["received_after_cancellation"])
        grants = self.lab.export()["grants"]
        self.assertFalse(grants[0]["acceptance"]["received_after_cancellation"])
        self.assertEqual(len(grants), 2)

    def test_cancellation_does_not_delete_revision_input(self):
        before = copy.deepcopy(self.revision)
        self.lab.cancel(self.task, self.control)
        self.assertEqual(before, self.revision)
        self.lab.prepare(self.revision, "Independent request survives", "lean_check")

    def test_cancellation_visible_across_database_connections(self):
        other = Lab(self.path, self.keys)
        try:
            grant = self.grant()
            other.cancel(self.task, self.control)
            with self.assertRaises(Rejected):
                self.lab.begin(grant, "fixture.a", self.keys["fixture.a"])
        finally:
            other.close()

    def test_concurrent_claims_obtain_the_same_ticket(self):
        grant, tickets, errors = self.grant(), [], []
        barrier = threading.Barrier(2)
        def claim():
            connection = Lab(self.path, self.keys)
            try:
                barrier.wait(timeout=5)
                tickets.append(connection.begin(grant, "fixture.a", self.keys["fixture.a"]))
            except Exception as exc:
                errors.append(str(exc))
            finally:
                connection.close()
        threads = [threading.Thread(target=claim) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=10)
        self.assertFalse(errors)
        self.assertEqual(len(tickets), 2)
        self.assertEqual(tickets[0], tickets[1])

    def test_sqlite_failure_rolls_back(self):
        self.lab.db.execute("CREATE TRIGGER fail_grant BEFORE INSERT ON lab_grants BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
        before = self.lab.export()
        with self.assertRaises(sqlite3.IntegrityError):
            self.grant()
        self.assertEqual(before, self.lab.export())
        self.lab.db.execute("DROP TRIGGER fail_grant")
        self.grant()

    def test_failure_outcome_does_not_block_further_requests(self):
        run_fixture(self.lab, self.grant(), "fixture.a", self.keys["fixture.a"], "check_failed")
        self.lab.prepare(self.revision, "Continue exploration", "ai_review")
        self.assertEqual(len(self.lab.export()["tasks"]), 2)

    def test_export_excludes_body_and_secrets(self):
        self.lab.finish(self.packet())
        rendered = canonical(self.lab.export()).decode()
        for secret in [self.control, self.keys["fixture.a"].hex(), self.revision["payload"]["body"], "control_hash"]:
            self.assertNotIn(secret, rendered)
        self.assertIn(self.revision["sha256"], rendered)

    def test_export_integrity(self):
        result = self.lab.export()
        self.assertEqual(result["schema"], AUDIT_SCHEMA)
        digest = result.pop("sha256")
        self.assertEqual(digest, sha(result))

    def test_audit_persists_across_restart(self):
        self.lab.finish(self.packet())
        before = self.lab.export()
        other = Lab(self.path, self.keys)
        try:
            self.assertEqual(before, other.export())
        finally:
            other.close()

    def test_mutating_returned_task_does_not_mutate_database(self):
        self.prepared["task"]["policy"]["network"] = True
        self.assertFalse(self.lab.export()["tasks"][0]["task"]["policy"]["network"])

    def test_input_revision_must_have_valid_digest(self):
        self.revision["payload"]["body"] += "changed"
        with self.assertRaises(Rejected):
            self.lab.prepare(self.revision, "scope", "lean_check")

    def test_metadata_binds_original_not_future_revision(self):
        original = self.prepared["task"]["revision_sha256"]
        self.revision["payload"]["body"] += "new version"
        self.assertEqual(original, self.lab.export()["tasks"][0]["task"]["revision_sha256"])

    def test_budget_types_and_caps(self):
        for field, cap in BUDGET.items():
            for value in [True, 0, -1, cap+1, "1", 1.5]:
                with self.subTest(field=field, value=value):
                    budget = {**BUDGET, field: value}
                    with self.assertRaises(Rejected):
                        self.lab.prepare(self.revision, "scope", "lean_check", budget)

    def test_budget_cannot_add_permission(self):
        with self.assertRaises(Rejected):
            self.lab.prepare(self.revision, "scope", "lean_check", {**BUDGET, "network": True})

    def test_output_limit_is_checked_not_just_recorded(self):
        task = self.lab.prepare(self.revision, "small output", "lean_check", {**BUDGET, "output_bytes": 10})
        grant = self.lab.authorize(task["task"]["id"], task["control_key"], "fixture.a")
        with self.assertRaises(Rejected):
            run_fixture(self.lab, grant, "fixture.a", self.keys["fixture.a"], "check_completed")
        self.assertIsNone(self.lab.export()["grants"][-1]["receipt"])

    def test_scope_and_revision_unicode_fail_cleanly(self):
        with self.assertRaises(Rejected):
            self.lab.prepare(self.revision, "\ud800", "lean_check")

    def test_unsupported_kind_and_verifier(self):
        for kind in [[], None, "run_shell"]:
            with self.assertRaises(Rejected):
                self.lab.prepare(self.revision, "scope", kind)
        with self.assertRaises(Rejected):
            self.lab.authorize(self.task, self.control, "external.ai")

    def test_registry_does_not_accept_real_service_or_weak_key(self):
        for workers in [{"external.ai": b"x"*32}, {"fixture.x": b"weak"}]:
            with self.assertRaises(Rejected):
                Lab(":memory:", workers)

    def test_read_v1_and_refuse_ambiguous_v2(self):
        bundle = {"format":"eksmu.bundle.v1", "records":[self.revision], "sha256":sha([self.revision])}
        self.assertEqual(read_v1_revision(bundle, self.revision["id"]), self.revision)
        for bad in ["eksmu.bundle.v2", "eksmu.bundle.v3", AUDIT_SCHEMA]:
            bundle["format"] = bad
            with self.assertRaises(Rejected):
                read_v1_revision(bundle, self.revision["id"])

    def test_duplicate_revision_or_bad_bundle_hash_fails(self):
        rows = [self.revision, self.revision]
        with self.assertRaises(Rejected):
            read_v1_revision({"format":"eksmu.bundle.v1","records":rows,"sha256":sha(rows)},self.revision["id"])
        with self.assertRaises(Rejected):
            read_v1_revision({"format":"eksmu.bundle.v1","records":[self.revision],"sha256":"bad"},self.revision["id"])

    def test_demo_and_exclusive_cli_output(self):
        audit = demonstration()
        self.assertEqual(len(audit["grants"]), 3)
        path = Path(self.directory.name) / "demo.json"
        command = [sys.executable, "-m", "verification_lab", "--output", str(path)]
        self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
        raw = path.read_bytes()
        self.assertEqual(subprocess.run(command, capture_output=True).returncode, 1)
        self.assertEqual(raw, path.read_bytes())


    def test_community_database_is_refused_without_schema_changes(self):
        other = Path(self.directory.name) / "community.sqlite"
        connection = sqlite3.connect(other)
        connection.execute("CREATE TABLE records(id TEXT PRIMARY KEY, document TEXT)")
        connection.execute("INSERT INTO records VALUES('original','unchanged')")
        connection.commit(); connection.close()
        before = other.read_bytes()
        with self.assertRaises(Rejected):
            Lab(other, self.keys)
        self.assertEqual(before, other.read_bytes())

    def test_invalid_timestamp_is_rejected_even_after_rehash(self):
        for stamp in ["not a date", "2026-09-20T10:00:00", "2026-09-20T10:00:00+01:00"]:
            with self.subTest(stamp=stamp):
                revision = copy.deepcopy(self.revision)
                revision["created_at"] = stamp
                revision["sha256"] = sha({k:v for k,v in revision.items() if k != "sha256"})
                with self.assertRaises(Rejected):
                    revision_binding(revision)

    def test_concurrent_duplicate_completion_accepts_once(self):
        packet, results, errors = self.packet(), [], []
        barrier = threading.Barrier(2)
        def complete():
            connection = Lab(self.path, self.keys)
            try:
                barrier.wait(timeout=5)
                results.append(connection.finish(packet))
            except Exception as exc:
                errors.append(str(exc))
            finally:
                connection.close()
        threads = [threading.Thread(target=complete) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=10)
        self.assertFalse(errors)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], results[1])
        self.assertEqual(len([g for g in self.lab.export()["grants"] if g["receipt"]]), 1)


class BaselineIntegration(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("EKSMU_BASELINE_SOURCE"), "Set EKSMU_BASELINE_SOURCE to the delivered v0.2 source directory")
    def test_original_v02_publish_verify_cancel_and_publish_again(self):
        # Import only when an actual baseline source tree is supplied.
        sys.path.insert(0, os.environ["EKSMU_BASELINE_SOURCE"])
        from eksmu.store import Store
        with tempfile.TemporaryDirectory() as temp:
            community = Store(Path(temp)/"community.sqlite")
            lab = Lab(Path(temp)/"lab.sqlite", {"fixture.test":b"integration-test-key-32-bytes-long"})
            try:
                identity = community.register("No institution needed")
                published = community.publish(identity["token"], "An unproved question")
                before = community.export()
                selected = read_v1_revision(before, published["revision"]["id"])
                prepared = lab.prepare(selected,"Check one clause","lean_check")
                grant = lab.authorize(prepared["task"]["id"],prepared["control_key"],"fixture.test")
                run_fixture(lab,grant,"fixture.test",b"integration-test-key-32-bytes-long","check_failed")
                lab.cancel(prepared["task"]["id"],prepared["control_key"])
                self.assertEqual(before,community.export())
                community.comment(identity["token"],published["revision"]["id"],"I disagree")
                community.publish(identity["token"],"Continue exploring despite the failed fixture")
                self.assertEqual(len([r for r in community.rows() if r["type"]=="exploration"]),2)
                self.assertNotIn(identity["token"],canonical(lab.export()).decode())
            finally:
                lab.close(); community.close()


if __name__ == "__main__":
    unittest.main()
