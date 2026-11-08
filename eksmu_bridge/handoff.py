"""Explicit local-file handoff to a separately chosen finite-check worker.

No network transport, daemon, untrusted-code sandbox, or publication authority.
The service pins a key per dispatch; the worker receives no community database,
identity token, source article, or service signing key. Same-user OS isolation
is NOT claimed. Cancellation cannot recall a file already handed to a worker.
"""
from __future__ import annotations

from copy import deepcopy
import json
import time
import uuid

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from eksmu_receipts import core as receipts
from .service import BridgeError, Tasks

CAPSULE = "eksmu.local-worker-capsule.v1"
SUBMISSION = "eksmu.local-worker-submission.v1"
DOMAIN = b"EKSMU-LOCAL-WORKER-SUBMISSION-V1\x00"
MAX_HANDOFFS = 8
MAX_TTL = 86400


def validate_capsule(capsule: dict) -> None:
    receipts.canonical(capsule)
    receipts.exact(capsule, {"schema", "dispatch_id", "request", "expected_public_key",
                             "created_at", "expires_at", "transport"})
    if capsule["schema"] != CAPSULE or capsule["transport"] != "local_file_only":
        raise BridgeError("Only the local-file worker contract is supported")
    receipts.uid(capsule["dispatch_id"])
    receipts.hex_bytes(capsule["expected_public_key"], 32)
    receipts.request_check(capsule["request"])
    if capsule["request"]["execution_policy"] != "local_builtin_opt_in":
        raise BridgeError("A record-only request cannot be dispatched")
    start, end = capsule["created_at"], capsule["expires_at"]
    if type(start) is not int or type(end) is not int or start < 0 or not 60 <= end - start <= MAX_TTL:
        raise BridgeError("Invalid dispatch validity window")


def execute_capsule(capsule: dict, signer: Ed25519PrivateKey, *, allow_local: bool = False,
                    current_time: int | None = None) -> dict:
    """Run ONLY the built-in bounded arithmetic interpreter, never user commands."""
    capsule = deepcopy(capsule)
    validate_capsule(capsule)
    if allow_local is not True:
        raise BridgeError("Worker operator must explicitly allow local computation", 403)
    if receipts.public_hex(signer) != capsule["expected_public_key"]:
        raise BridgeError("Worker key differs from the requester's selected key", 403)
    timestamp = int(time.time()) if current_time is None else current_time
    if not capsule["created_at"] <= timestamp < capsule["expires_at"]:
        raise BridgeError("Dispatch is not currently valid", 409)
    receipt = receipts.PendingRun(capsule["request"]).execute(signer, allow_local_compute=True)
    body = {"schema": SUBMISSION, "dispatch_id": capsule["dispatch_id"],
            "capsule_sha256": receipts.sha(capsule), "receipt": receipt}
    return {"body": body, "public_key": receipts.public_hex(signer),
            "signature": signer.sign(DOMAIN + receipts.canonical(body)).hex()}


def verify_submission(capsule: dict, submission: dict, expected_public_key: str) -> dict:
    """Authenticate this dispatch and independently recompute its finite result.

The caller must obtain its expected key independently of an untrusted result.
The signature does not certify who consented or the source article's meaning.
"""
    capsule, submission = deepcopy(capsule), deepcopy(submission)
    validate_capsule(capsule)
    trusted = receipts.hex_bytes(expected_public_key, 32)
    receipts.canonical(submission)
    receipts.exact(submission, {"body", "public_key", "signature"})
    if capsule["expected_public_key"] != expected_public_key or submission["public_key"] != expected_public_key:
        raise BridgeError("Result key does not match the independently selected key", 403)
    signature = receipts.hex_bytes(submission["signature"], 64)
    try:
        Ed25519PublicKey.from_public_bytes(trusted).verify(signature, DOMAIN + receipts.canonical(submission["body"]))
    except InvalidSignature as exc:
        raise BridgeError("Invalid worker submission signature") from exc
    body = submission["body"]
    receipts.exact(body, {"schema", "dispatch_id", "capsule_sha256", "receipt"})
    if (body["schema"] != SUBMISSION or body["dispatch_id"] != capsule["dispatch_id"]
            or body["capsule_sha256"] != receipts.sha(capsule)):
        raise BridgeError("Result is bound to a different dispatch")
    checked = receipts.verify(body["receipt"], capsule["request"], expected_public_key)
    return {**checked, "dispatch_binding_valid": True, "requester_identity_certified": False}


class Handoffs:
    """Persist local dispatches next to tasks, not in the publication database."""
    def __init__(self, tasks: Tasks, *, clock=time.time):
        self.tasks, self.db, self.clock = tasks, tasks.db, clock
        self.db.execute("""CREATE TABLE IF NOT EXISTS receipt_handoffs(
            id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES receipt_tasks(id),
            operation_id TEXT NOT NULL, document TEXT NOT NULL,
            submission TEXT, disposition TEXT, received_at INTEGER,
            UNIQUE(task_id, operation_id))""")

    def _row(self, dispatch_id: str):
        receipts.uid(dispatch_id)
        row = self.db.execute("SELECT document,submission,disposition,received_at FROM receipt_handoffs WHERE id=?",
                              (dispatch_id,)).fetchone()
        if row is None:
            raise BridgeError("Unknown worker dispatch", 404)
        return row

    def view(self, dispatch_id: str, actor_id: str) -> dict:
        raw, submission, disposition, received = self._row(dispatch_id)
        dispatch = json.loads(raw)
        task = self.tasks._owned(dispatch["task_id"], actor_id)
        if disposition is None:
            disposition = ("cancelled" if task["cancelled_at"] else
                           "expired" if int(self.clock()) >= dispatch["capsule"]["expires_at"] else "ready")
        return {"dispatch": dispatch, "submission": json.loads(submission) if submission else None,
                "disposition": disposition, "received_at": received,
                "task_cancelled_at": task["cancelled_at"]}

    def list(self, task_id: str, actor_id: str) -> list[dict]:
        self.tasks._owned(task_id, actor_id)
        ids = [r[0] for r in self.db.execute("SELECT id FROM receipt_handoffs WHERE task_id=? ORDER BY rowid", (task_id,))]
        return [self.view(rid, actor_id) for rid in ids]

    def dispatch(self, task_id: str, actor_id: str, operation_id: str,
                 expected_public_key: str, consent: bool, ttl_seconds: int = 3600) -> dict:
        if consent is not True:
            raise BridgeError("Explicit consent to local-file handoff is required", 403)
        receipts.uid(operation_id)
        receipts.hex_bytes(expected_public_key, 32)
        if type(ttl_seconds) is not int or not 60 <= ttl_seconds <= MAX_TTL:
            raise BridgeError("Dispatch lifetime must be 60 through 86400 seconds")
        with self.tasks.transaction():
            task = self.tasks._owned(task_id, actor_id)
            existing = self.db.execute("SELECT id,document FROM receipt_handoffs WHERE task_id=? AND operation_id=?",
                                       (task_id, operation_id)).fetchone()
            if existing:
                previous = json.loads(existing[1])["capsule"]
                if (previous["expected_public_key"] != expected_public_key or
                        previous["expires_at"] - previous["created_at"] != ttl_seconds):
                    raise BridgeError("An operation ID cannot change its selected worker or lifetime", 409)
                return self.view(existing[0], actor_id)
            if task["cancelled_at"]:
                raise BridgeError("Task cancelled; no new worker dispatch is permitted", 409)
            if self.db.execute("SELECT COUNT(*) FROM receipt_handoffs WHERE task_id=?", (task_id,)).fetchone()[0] >= MAX_HANDOFFS:
                raise BridgeError("Local per-task dispatch quota reached; publishing is unaffected", 413)
            request = deepcopy(task["prepared_request"])
            request["execution_policy"] = "local_builtin_opt_in"
            timestamp, dispatch_id = int(self.clock()), str(uuid.uuid4())
            capsule = {"schema": CAPSULE, "dispatch_id": dispatch_id, "request": request,
                       "expected_public_key": expected_public_key, "created_at": timestamp,
                       "expires_at": timestamp + ttl_seconds, "transport": "local_file_only"}
            validate_capsule(capsule)
            dispatch = {"id": dispatch_id, "task_id": task_id, "actor_id": actor_id,
                        "operation_id": operation_id, "capsule": capsule}
            self.db.execute("INSERT INTO receipt_handoffs(id,task_id,operation_id,document) VALUES(?,?,?,?)",
                            (dispatch_id, task_id, operation_id, json.dumps(dispatch, ensure_ascii=False)))
        return self.view(dispatch_id, actor_id)

    def collect(self, dispatch_id: str, actor_id: str, submission: dict) -> dict:
        submission = deepcopy(submission)
        initial = self.view(dispatch_id, actor_id)
        capsule = initial["dispatch"]["capsule"]
        # Verify outside the transaction: no long-running worker holds a DB lock.
        # The receiver deliberately recomputes the small finite result as well.
        verify_submission(capsule, submission, capsule["expected_public_key"])
        with self.tasks.transaction():
            current = self.view(dispatch_id, actor_id)
            if current["submission"] is not None:
                if receipts.canonical(current["submission"]) != receipts.canonical(submission):
                    raise BridgeError("Conflicting result cannot overwrite a recorded dispatch", 409)
                return current
            timestamp = int(self.clock())
            disposition = ("late_cancelled" if current["task_cancelled_at"] else
                           "late_expired" if timestamp >= capsule["expires_at"] else "recorded")
            self.db.execute("UPDATE receipt_handoffs SET submission=?,disposition=?,received_at=? WHERE id=?",
                            (json.dumps(submission, ensure_ascii=False), disposition, timestamp, dispatch_id))
        return self.view(dispatch_id, actor_id)
