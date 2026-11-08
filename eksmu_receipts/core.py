"""Small exact-arithmetic checker with independently verifiable signed receipts.

Not a Lean runner, AI judge, distributed queue, or general-purpose code sandbox.
Requests use a separate namespace: no implicit migration of eksmu.bundle.v1/v2.
"""
from __future__ import annotations

import hashlib
import json
import platform
import re
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from fractions import Fraction
from typing import Any

import cryptography
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

REQUEST = "eksmu.receipt-request.v1"
RECEIPT = "eksmu.execution-receipt.v1"
DOMAIN = b"EKSMU-OFFLINE-RECEIPT-V1\x00"
MAX_BYTES = 256 * 1024
CAPS = {"nodes": 256, "depth": 24, "bits": 512}
RIGHTS = {"network": False, "user_code": False, "community_write": False, "funds": False}
SCOPE_NOTE = "Only the supplied closed rational equality is checked; no theorem, authorship, novelty or physical interpretation is certified."
HEX = re.compile(r"[0-9a-f]+\Z")
DECIMAL = re.compile(r"(?:0|-[1-9][0-9]*|[1-9][0-9]*)\Z")


class Rejected(ValueError):
    """A boundary or schema error; never a reason to restrict publication."""


class CheckFailure(Exception):
    def __init__(self, status: str, reason: str):
        self.status, self.reason = status, reason


def exact(obj: Any, fields: set[str]) -> None:
    if type(obj) is not dict or set(obj) != fields:
        raise Rejected("Missing or unexpected fields")


def text(value: Any, limit: int = 2048) -> None:
    if type(value) is not str or not value.strip() or len(value) > limit:
        raise Rejected("Invalid text")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise Rejected("Invalid UTF-8 text") from exc


def hex_bytes(value: Any, size: int) -> bytes:
    if type(value) is not str or len(value) != size * 2 or not HEX.fullmatch(value):
        raise Rejected("Invalid hex encoding")
    return bytes.fromhex(value)


def uid(value: Any) -> None:
    text(value, 36)
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError
    except ValueError as exc:
        raise Rejected("Invalid identifier") from exc


def canonical(obj: Any) -> bytes:
    # A deliberately limited Python/JSON encoding, not a claim to implement JCS.
    remaining = 8192
    def walk(value, depth=0):
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 64:
            raise Rejected("JSON structure limit exceeded")
        if type(value) is dict:
            for key, item in value.items():
                if type(key) is not str:
                    raise Rejected("JSON keys must be strings")
                walk(key, depth + 1); walk(item, depth + 1)
        elif type(value) is list:
            for item in value:
                walk(item, depth + 1)
        elif type(value) is str:
            if len(value) > MAX_BYTES:
                raise Rejected("JSON string limit exceeded")
        elif type(value) is int:
            if value.bit_length() > 63:
                raise Rejected("Use decimal strings for large integers")
        elif value is not None and type(value) is not bool:
            raise Rejected("Unsupported JSON value; floats are not accepted")
    try:
        walk(obj)
        raw = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise Rejected("Invalid canonical JSON") from exc
    if len(raw) > MAX_BYTES:
        raise Rejected("JSON byte limit exceeded")
    return raw


def load(raw: bytes) -> dict:
    if len(raw) > MAX_BYTES:
        raise Rejected("JSON byte limit exceeded")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise Rejected("Duplicate JSON key")
            result[key] = value
        return result
    def no_constant(_):
        raise Rejected("Non-finite number")
    try:
        obj = json.loads(raw, object_pairs_hook=pairs, parse_constant=no_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise Rejected("Invalid JSON document") from exc
    if type(obj) is not dict:
        raise Rejected("Expected an object")
    canonical(obj)
    return obj


def sha(obj: Any) -> str:
    return hashlib.sha256(canonical(obj)).hexdigest()


def request_check(req: dict) -> None:
    canonical(req)
    exact(req, {"schema", "request_id", "target", "scope", "checker", "input", "limits", "execution_policy", "rights"})
    if req["schema"] != REQUEST:
        raise Rejected("Unknown request schema; implicit conversion is forbidden")
    uid(req["request_id"])
    exact(req["target"], {"revision_id", "revision_sha256", "binding"})
    uid(req["target"]["revision_id"])
    hex_bytes(req["target"]["revision_sha256"], 32)
    if req["target"]["binding"] != "caller_supplied_reference":
        raise Rejected("Only explicit caller-supplied revision references are supported")
    text(req["scope"])
    text(req["checker"], 80)
    exact(req["rights"], set(RIGHTS))
    if any(req["rights"][key] is not False for key in RIGHTS):
        raise Rejected("Network, user-code, community-write and funds permissions are unavailable")
    if req["execution_policy"] not in ("record_only", "local_builtin_opt_in"):
        raise Rejected("Unsupported execution policy")
    exact(req["limits"], set(CAPS))
    for key, cap in CAPS.items():
        value = req["limits"][key]
        if type(value) is not int or not 1 <= value <= cap:
            raise Rejected("Resource limit outside supported range")
    if type(req["input"]) is not dict:
        raise Rejected("Checker input must be a JSON object")


def make_request(revision_id: str, revision_sha256: str, scope: str, problem: dict,
                 *, allow_local: bool = False, limits: dict | None = None,
                 checker: str = "rational-equality/v1") -> dict:
    if type(allow_local) is not bool:
        raise Rejected("Consent must be explicit")
    req = {"schema": REQUEST, "request_id": str(uuid.uuid4()),
           "target": {"revision_id": revision_id, "revision_sha256": revision_sha256,
                      "binding": "caller_supplied_reference"},
           "scope": scope, "checker": checker, "input": deepcopy(problem),
           "limits": deepcopy(CAPS if limits is None else limits),
           "execution_policy": "local_builtin_opt_in" if allow_local else "record_only",
           "rights": dict(RIGHTS)}
    request_check(req)
    return req


def calculate(req: dict) -> dict:
    """Bounded data interpreter: never eval/exec, imports, file paths or network input."""
    request_check(req)
    nodes = 0
    limits = req["limits"]
    def bounded(value: Fraction) -> Fraction:
        if max(abs(value.numerator).bit_length(), value.denominator.bit_length()) > limits["bits"]:
            raise CheckFailure("resource_limited", "integer_bit_budget")
        return value
    def number(raw: Any) -> int:
        if type(raw) is not str or len(raw) > 160 or not DECIMAL.fullmatch(raw):
            raise CheckFailure("invalid_input", "canonical_decimal_required")
        value = int(raw)
        if abs(value).bit_length() > limits["bits"]:
            raise CheckFailure("resource_limited", "integer_bit_budget")
        return value
    def visit(node: Any, depth=1) -> Fraction:
        nonlocal nodes
        nodes += 1
        if nodes > limits["nodes"] or depth > limits["depth"]:
            raise CheckFailure("resource_limited", "expression_budget")
        if type(node) is not dict or type(node.get("op")) is not str:
            raise CheckFailure("invalid_input", "expression_object_required")
        op = node["op"]
        if op == "rational":
            if set(node) != {"op", "numerator", "denominator"}:
                raise CheckFailure("invalid_input", "unexpected_expression_field")
            n, d = number(node["numerator"]), number(node["denominator"])
            if not d:
                raise CheckFailure("invalid_input", "zero_denominator")
            return bounded(Fraction(n, d))
        if op not in {"add", "sub", "mul", "div"}:
            raise CheckFailure("unsupported", "operation_not_supported")
        if set(node) != {"op", "left", "right"}:
            raise CheckFailure("invalid_input", "unexpected_expression_field")
        a, b = visit(node["left"], depth + 1), visit(node["right"], depth + 1)
        if op == "add": value = a + b
        elif op == "sub": value = a - b
        elif op == "mul": value = a * b
        elif not b: raise CheckFailure("invalid_input", "division_by_zero")
        else: value = a / b
        return bounded(value)
    try:
        if req["checker"] != "rational-equality/v1":
            raise CheckFailure("unsupported", "checker_not_installed")
        if set(req["input"]) != {"left", "right"}:
            raise CheckFailure("invalid_input", "equality_sides_required")
        left, right = visit(req["input"]["left"]), visit(req["input"]["right"])
        output = {"status": "passed" if left == right else "failed", "reason": "exact_comparison",
                  "left": [str(left.numerator), str(left.denominator)],
                  "right": [str(right.numerator), str(right.denominator)]}
    except CheckFailure as exc:
        output = {"status": exc.status, "reason": exc.reason, "left": None, "right": None}
    return {**output, "visited_nodes": nodes, "scope_note": SCOPE_NOTE}


def public_hex(key: Ed25519PrivateKey) -> str:
    return key.public_key().public_bytes_raw().hex()


def sign_body(body: dict, key: Ed25519PrivateKey) -> dict:
    return {"body": deepcopy(body), "public_key": public_hex(key),
            "signature": key.sign(DOMAIN + canonical(body)).hex()}


class PendingRun:
    """One in-memory attempt. State is NOT a durable distributed queue.

    Cancellation wins only before start. No caller receives community capabilities.
    """
    def __init__(self, req: dict):
        self._request = deepcopy(req)
        request_check(self._request)
        self._lock = threading.Lock()
        self._state = "pending"
        self._receipt = None

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def cancel(self) -> bool:
        with self._lock:
            if self._state != "pending":
                return False
            self._state = "cancelled"
            return True

    def execute(self, key: Ed25519PrivateKey, *, allow_local_compute: bool = False) -> dict:
        with self._lock:
            if self._state != "pending":
                raise Rejected("Attempt is not pending; retry requires a new attempt")
            if self._request["execution_policy"] != "local_builtin_opt_in" or allow_local_compute is not True:
                raise Rejected("Both request opt-in and operator opt-in are required")
            self._state = "running"
        try:
            started = datetime.now(timezone.utc).isoformat()
            result = calculate(self._request)
            body = {"schema": RECEIPT, "run_id": str(uuid.uuid4()),
                    "request_sha256": sha(self._request), "request_id": self._request["request_id"],
                    "target": deepcopy(self._request["target"]), "scope": self._request["scope"],
                    "checker": self._request["checker"], "input_sha256": sha(self._request["input"]),
                    "limits": deepcopy(self._request["limits"]), "rights": dict(RIGHTS),
                    "started_at": started, "finished_at": datetime.now(timezone.utc).isoformat(),
                    "engine": {"name": "eksmu-offline-rational", "version": "0.1.0",
                               "python": platform.python_version(), "cryptography": cryptography.__version__},
                    "result": result}
            receipt = sign_body(body, key)
        except Exception:
            with self._lock:
                self._state = "error"
            raise
        with self._lock:
            self._state, self._receipt = "completed", deepcopy(receipt)
        return receipt


def verify(receipt: dict, req: dict, expected_public_key: str) -> dict:
    """Pin a key chosen by the reader, verify bytes, binding AND rerun the finite check.

    This does not establish that the supplied source reference or author's text
    means the supplied equality. A valid signature authenticates a key, not truth.
    """
    receipt, req = deepcopy(receipt), deepcopy(req)
    request_check(req)
    canonical(receipt)
    exact(receipt, {"body", "public_key", "signature"})
    trusted = hex_bytes(expected_public_key, 32)
    if receipt["public_key"] != expected_public_key:
        raise Rejected("Signer differs from the independently selected key")
    signature = hex_bytes(receipt["signature"], 64)
    try:
        Ed25519PublicKey.from_public_bytes(trusted).verify(signature, DOMAIN + canonical(receipt["body"]))
    except InvalidSignature as exc:
        raise Rejected("Invalid receipt signature") from exc
    body = receipt["body"]
    exact(body, {"schema", "run_id", "request_sha256", "request_id", "target", "scope", "checker",
                 "input_sha256", "limits", "rights", "started_at", "finished_at", "engine", "result"})
    if body["schema"] != RECEIPT or body["request_sha256"] != sha(req):
        raise Rejected("Receipt schema or request binding mismatch")
    uid(body["run_id"])
    if req["execution_policy"] != "local_builtin_opt_in":
        raise Rejected("Receipt cannot elevate record-only permission")
    for field in ("request_id", "target", "scope", "checker", "limits", "rights"):
        if canonical(body[field]) != canonical(req[field]):
            raise Rejected("Receipt target or permissions mismatch")
    if body["input_sha256"] != sha(req["input"]):
        raise Rejected("Input digest mismatch")
    exact(body["engine"], {"name", "version", "python", "cryptography"})
    if body["engine"]["name"] != "eksmu-offline-rational" or body["engine"]["version"] != "0.1.0":
        raise Rejected("Unsupported engine contract")
    for field in ("python", "cryptography"):
        text(body["engine"][field], 80)
    stamps = []
    for field in ("started_at", "finished_at"):
        text(body[field], 64)
        try:
            value = datetime.fromisoformat(body[field])
            if value.tzinfo is None or value.utcoffset().total_seconds() != 0:
                raise ValueError
        except ValueError as exc:
            raise Rejected("UTC execution times required") from exc
        stamps.append(value)
    if stamps[1] < stamps[0]:
        raise Rejected("Invalid execution time order")
    if canonical(body["result"]) != canonical(calculate(req)):
        raise Rejected("Signed result disagrees with deterministic replay")
    return {"signature_valid": True, "request_binding_valid": True, "replay_matches": True,
            "status": body["result"]["status"], "signer_sha256": hashlib.sha256(trusted).hexdigest(),
            "source_semantics_verified": False, "publication_authority": False}


class ReceiptBook:
    """In-memory reader-side deduplication. Independent signers can coexist."""
    def __init__(self):
        self._entries: dict[tuple[str, str], dict] = {}
        self._lock = threading.Lock()

    def add(self, receipt: dict, req: dict, expected_public_key: str) -> bool:
        # Freeze caller inputs before verification and storage.
        frozen, frozen_req = deepcopy(receipt), deepcopy(req)
        verify(frozen, frozen_req, expected_public_key)
        key = (expected_public_key, frozen["body"]["run_id"])
        with self._lock:
            if key in self._entries:
                if canonical(self._entries[key]) != canonical(frozen):
                    raise Rejected("Conflicting receipt for the same signer/run")
                return False
            self._entries[key] = frozen
            return True

    def all(self) -> list[dict]:
        with self._lock:
            return deepcopy(list(self._entries.values()))
