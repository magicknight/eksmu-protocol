import json
import socket
import tempfile
import threading
import unittest
import uuid
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from eksmu_receipts import core as c


def rational(n, d=1):
    return {"op": "rational", "numerator": str(n), "denominator": str(d)}


def op(name, a, b):
    return {"op": name, "left": a, "right": b}


class Receipts(unittest.TestCase):
    def setUp(self):
        self.key = Ed25519PrivateKey.generate()
        self.public = c.public_hex(self.key)
        self.req = c.make_request(str(uuid.uuid4()), "ab" * 32, "演示：只检查这一个等式，不判断论文价值。",
            {"left": op("add", rational(1, 3), rational(1, 6)), "right": rational(1, 2)}, allow_local=True)

    def run_receipt(self, req=None, key=None):
        return c.PendingRun(self.req if req is None else req).execute(key or self.key, allow_local_compute=True)

    def test_exact_fraction_equality(self):
        result = c.calculate(self.req)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["left"], ["1", "2"])

    def test_wrong_equality_has_values(self):
        self.req["input"]["right"] = rational(2, 3)
        result = c.calculate(self.req)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["right"], ["2", "3"])

    def test_fraction_sign_reduction(self):
        self.req["input"] = {"left": rational(2, -4), "right": rational(-1, 2)}
        self.assertEqual(c.calculate(self.req)["status"], "passed")

    def test_subtraction(self):
        self.req["input"] = {"left": op("sub", rational(3, 2), rational(1)), "right": rational(1, 2)}
        self.assertEqual(c.calculate(self.req)["status"], "passed")

    def test_multiplication(self):
        self.req["input"] = {"left": op("mul", rational(2, 3), rational(3, 4)), "right": rational(1, 2)}
        self.assertEqual(c.calculate(self.req)["status"], "passed")

    def test_division(self):
        self.req["input"] = {"left": op("div", rational(1, 3), rational(2, 3)), "right": rational(1, 2)}
        self.assertEqual(c.calculate(self.req)["status"], "passed")

    def test_zero_denominator(self):
        self.req["input"]["left"] = rational(1, 0)
        self.assertEqual(c.calculate(self.req)["reason"], "zero_denominator")

    def test_division_by_zero(self):
        self.req["input"]["left"] = op("div", rational(1), rational(0))
        self.assertEqual(c.calculate(self.req)["reason"], "division_by_zero")

    def test_nodes_budget(self):
        self.req["limits"]["nodes"] = 1
        self.assertEqual(c.calculate(self.req)["status"], "resource_limited")

    def test_depth_budget(self):
        self.req["limits"]["depth"] = 1
        self.assertEqual(c.calculate(self.req)["status"], "resource_limited")

    def test_operand_bit_budget(self):
        self.req["limits"]["bits"] = 4
        self.req["input"]["left"] = rational(16)
        self.assertEqual(c.calculate(self.req)["reason"], "integer_bit_budget")

    def test_result_bit_budget(self):
        self.req["limits"]["bits"] = 4
        self.req["input"]["left"] = op("mul", rational(8), rational(8))
        self.assertEqual(c.calculate(self.req)["reason"], "integer_bit_budget")

    def test_unknown_checker_not_a_fake_lean_success(self):
        self.req["checker"] = "lean"
        self.assertEqual(c.calculate(self.req)["reason"], "checker_not_installed")

    def test_unknown_operation(self):
        self.req["input"]["left"] = {"op": "exec", "command": "untrusted text, not a command"}
        self.assertEqual(c.calculate(self.req)["status"], "unsupported")

    def test_extra_expression_fields_rejected(self):
        self.req["input"]["right"]["url"] = "https://example.invalid"
        self.assertEqual(c.calculate(self.req)["status"], "invalid_input")

    def test_boolean_not_a_number(self):
        self.req["input"]["right"]["numerator"] = True
        self.assertEqual(c.calculate(self.req)["status"], "invalid_input")

    def test_noncanonical_decimal(self):
        for raw in ["01", "-0", "+1", "1.0", "1e30", " 1"]:
            with self.subTest(raw=raw):
                self.req["input"]["right"]["numerator"] = raw
                self.assertEqual(c.calculate(self.req)["status"], "invalid_input")

    def test_valid_signature_binding_replay(self):
        result = c.verify(self.run_receipt(), self.req, self.public)
        self.assertTrue(result["signature_valid"])
        self.assertTrue(result["replay_matches"])
        self.assertFalse(result["source_semantics_verified"])
        self.assertFalse(result["publication_authority"])

    def test_changed_body_invalidates_signature(self):
        receipt = self.run_receipt()
        receipt["body"]["result"]["status"] = "failed"
        with self.assertRaisesRegex(c.Rejected, "signature"):
            c.verify(receipt, self.req, self.public)

    def test_self_signed_not_automatically_trusted(self):
        stranger = Ed25519PrivateKey.generate()
        with self.assertRaisesRegex(c.Rejected, "independently selected"):
            c.verify(self.run_receipt(key=stranger), self.req, self.public)

    def test_signed_false_result_still_fails_replay(self):
        receipt = self.run_receipt()
        body = receipt["body"]; body["result"]["status"] = "failed"
        with self.assertRaisesRegex(c.Rejected, "replay"):
            c.verify(c.sign_body(body, self.key), self.req, self.public)

    def test_changed_revision_rejected(self):
        receipt = self.run_receipt()
        self.req["target"]["revision_sha256"] = "cd" * 32
        with self.assertRaises(c.Rejected): c.verify(receipt, self.req, self.public)

    def test_changed_scope_rejected(self):
        receipt = self.run_receipt(); self.req["scope"] = "A different claim"
        with self.assertRaises(c.Rejected): c.verify(receipt, self.req, self.public)

    def test_changed_input_rejected(self):
        receipt = self.run_receipt(); self.req["input"]["right"] = rational(3)
        with self.assertRaises(c.Rejected): c.verify(receipt, self.req, self.public)

    def test_changed_request_id_rejected(self):
        receipt = self.run_receipt(); self.req["request_id"] = str(uuid.uuid4())
        with self.assertRaises(c.Rejected): c.verify(receipt, self.req, self.public)

    def test_signed_privilege_escalation_rejected(self):
        body = self.run_receipt()["body"]; body["rights"]["community_write"] = True
        with self.assertRaises(c.Rejected): c.verify(c.sign_body(body, self.key), self.req, self.public)

    def test_signed_engine_impersonation_rejected(self):
        body = self.run_receipt()["body"]; body["engine"]["name"] = "Lean verified"
        with self.assertRaises(c.Rejected): c.verify(c.sign_body(body, self.key), self.req, self.public)

    def test_negative_elapsed_time_rejected(self):
        body = self.run_receipt()["body"]; body["finished_at"] = "1970-01-01T00:00:00+00:00"
        with self.assertRaises(c.Rejected): c.verify(c.sign_body(body, self.key), self.req, self.public)

    def test_extra_receipt_field_rejected(self):
        receipt = self.run_receipt(); receipt["verified_theorem"] = True
        with self.assertRaises(c.Rejected): c.verify(receipt, self.req, self.public)

    def test_default_request_is_record_only(self):
        req = c.make_request(str(uuid.uuid4()), "aa" * 32, "scope", self.req["input"])
        self.assertEqual(req["execution_policy"], "record_only")
        with self.assertRaises(c.Rejected): self.run_receipt(req)

    def test_operator_opt_in_also_required(self):
        job = c.PendingRun(self.req)
        with self.assertRaises(c.Rejected): job.execute(self.key)
        self.assertEqual(job.state, "pending")

    def test_request_rights_escalation_rejected(self):
        for key in c.RIGHTS:
            with self.subTest(key=key):
                req = deepcopy(self.req); req["rights"][key] = True
                with self.assertRaises(c.Rejected): c.PendingRun(req)

    def test_limits_reject_bool_zero_and_oversize(self):
        for value in [True, 0, -1, 257]:
            self.req["limits"]["nodes"] = value
            with self.assertRaises(c.Rejected): c.request_check(self.req)

    def test_cancel_pending_never_runs(self):
        job = c.PendingRun(self.req)
        self.assertTrue(job.cancel()); self.assertFalse(job.cancel())
        with patch.object(c, "calculate", side_effect=AssertionError("must not run")):
            with self.assertRaises(c.Rejected): job.execute(self.key, allow_local_compute=True)
        self.assertEqual(job.state, "cancelled")

    def test_cancel_running_does_not_claim_rollback(self):
        job = c.PendingRun(self.req)
        started, release = threading.Event(), threading.Event()
        original = c.calculate
        results = []
        def pause(req):
            started.set()
            if not release.wait(3): raise AssertionError("test timeout")
            return original(req)
        with patch.object(c, "calculate", side_effect=pause):
            thread = threading.Thread(target=lambda: results.append(job.execute(self.key, allow_local_compute=True)))
            thread.start()
            try:
                self.assertTrue(started.wait(3))
                self.assertFalse(job.cancel())
            finally:
                release.set(); thread.join(3)
        self.assertEqual(job.state, "completed"); self.assertEqual(len(results), 1)

    def test_double_execute_rejected(self):
        job = c.PendingRun(self.req); job.execute(self.key, allow_local_compute=True)
        with self.assertRaises(c.Rejected): job.execute(self.key, allow_local_compute=True)

    def test_retry_new_id_preserves_old_receipt(self):
        first, second = self.run_receipt(), self.run_receipt()
        self.assertNotEqual(first["body"]["run_id"], second["body"]["run_id"])
        self.assertEqual(first["body"]["request_sha256"], second["body"]["request_sha256"])

    def test_frozen_request_after_construction(self):
        job = c.PendingRun(self.req); original = deepcopy(self.req)
        self.req["input"]["right"] = rational(10)
        receipt = job.execute(self.key, allow_local_compute=True)
        self.assertEqual(c.verify(receipt, original, self.public)["status"], "passed")

    def test_runtime_error_not_reported_as_success(self):
        job = c.PendingRun(self.req)
        with patch.object(c, "calculate", side_effect=RuntimeError("test fault")):
            with self.assertRaises(RuntimeError): job.execute(self.key, allow_local_compute=True)
        self.assertEqual(job.state, "error")

    def test_duplicate_receipt_idempotent(self):
        book, receipt = c.ReceiptBook(), self.run_receipt()
        self.assertTrue(book.add(receipt, self.req, self.public))
        self.assertFalse(book.add(receipt, self.req, self.public))
        self.assertEqual(len(book.all()), 1)

    def test_conflicting_same_run_rejected(self):
        book, receipt = c.ReceiptBook(), self.run_receipt()
        book.add(receipt, self.req, self.public)
        body = receipt["body"]; body["engine"]["python"] = "different-runtime"
        with self.assertRaisesRegex(c.Rejected, "Conflicting"):
            book.add(c.sign_body(body, self.key), self.req, self.public)

    def test_independent_signers_coexist(self):
        book, other = c.ReceiptBook(), Ed25519PrivateKey.generate()
        book.add(self.run_receipt(), self.req, self.public)
        book.add(self.run_receipt(key=other), self.req, c.public_hex(other))
        self.assertEqual(len(book.all()), 2)

    def test_reader_output_mutation_does_not_edit_book(self):
        book = c.ReceiptBook(); book.add(self.run_receipt(), self.req, self.public)
        book.all()[0]["body"]["scope"] = "mutated"
        self.assertEqual(book.all()[0]["body"]["scope"], self.req["scope"])

    def test_no_network_or_subprocess(self):
        with patch("socket.socket", side_effect=AssertionError("network disabled")), patch("subprocess.Popen", side_effect=AssertionError("process disabled")):
            self.assertEqual(c.verify(self.run_receipt(), self.req, self.public)["status"], "passed")

    def test_no_identity_or_private_key_in_receipt(self):
        raw = c.canonical(self.run_receipt())
        self.assertNotIn(self.key.private_bytes_raw().hex().encode(), raw)
        self.assertNotIn(b"token", raw)

    def test_valid_json_roundtrip(self):
        self.assertEqual(c.load(c.canonical(self.req)), self.req)

    def test_duplicate_json_keys_rejected(self):
        with self.assertRaises(c.Rejected): c.load(b'{"x":1,"x":2}')

    def test_float_json_rejected(self):
        for raw in [b'{"x":0.5}', b'{"x":NaN}', b'{"x":Infinity}']:
            with self.assertRaises(c.Rejected): c.load(raw)

    def test_unicode_surrogate_rejected(self):
        with self.assertRaises(c.Rejected): c.canonical({"x": "\ud800"})

    def test_json_depth_limit(self):
        value = {}; current = value
        for _ in range(70): current["x"] = {}; current = current["x"]
        with self.assertRaises(c.Rejected): c.canonical(value)

    def test_byte_limit(self):
        with self.assertRaises(c.Rejected): c.load(b" " * (c.MAX_BYTES + 1))

    def test_unknown_fields_and_bundle_not_migrated(self):
        self.req["schema"] = "eksmu.bundle.v2"
        with self.assertRaises(c.Rejected): c.request_check(self.req)

    def test_record_only_cannot_receive_signed_receipt(self):
        self.req["execution_policy"] = "record_only"
        body = {"schema": c.RECEIPT}
        with self.assertRaises(c.Rejected): c.verify(c.sign_body(body, self.key), self.req, self.public)


if __name__ == "__main__": unittest.main()
