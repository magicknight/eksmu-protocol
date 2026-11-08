"""Local, bounded receipt tasks, separate from publication storage.

This is a synchronous finite checker, NOT a worker sandbox or an AI/Lean runner.
No community Store or identity secret is accepted by this layer. The HTTP layer
supplies the authenticated actor and an immutable revision snapshot.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import uuid

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from eksmu_receipts import core as receipts
from verification_lab.protocol import revision_binding

SCHEMA = "eksmu.linked-receipt-task.v1"
MAX_TASKS = 1000
MAX_ATTEMPTS = 8


class BridgeError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_key(path: str | Path) -> Ed25519PrivateKey:
    """Load or exclusively create a key; the caller decides if a new identity is allowed."""
    path = Path(path)
    try:
        # Exclusive creation avoids replacing an existing key. Start the service
        # once per local data directory; no shared-directory key orchestration.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raw = path.read_bytes()
    else:
        raw = Ed25519PrivateKey.generate().private_bytes_raw()
        with os.fdopen(fd, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
    if len(raw) != 32:
        raise BridgeError("Invalid local signing key; recover it rather than overwrite it", 503)
    return Ed25519PrivateKey.from_private_bytes(raw)


def check_export(bundle: dict, expected_public_key: str) -> list[dict]:
    """Recheck records with a reader-selected key, never a key trusted from a bundle."""
    receipts.hex_bytes(expected_public_key, 32)
    receipts.exact(bundle, {"schema", "task", "attempts", "trust_note"})
    if bundle["schema"] != SCHEMA:
        raise BridgeError("Unknown linked-task schema")
    task = bundle["task"]
    receipts.exact(task, {"id", "actor_id", "created_at", "revision", "prepared_request", "cancelled_at"})
    receipts.uid(task["id"])
    receipts.uid(task["actor_id"])
    binding = revision_binding(task["revision"])
    prepared = task["prepared_request"]
    receipts.request_check(prepared)
    if prepared["request_id"] != task["id"] or prepared["execution_policy"] != "record_only":
        raise BridgeError("Prepared request identity or policy mismatch")
    if any(prepared["target"][k] != v for k, v in binding.items()):
        raise BridgeError("Source revision binding mismatch")
    if type(bundle["attempts"]) is not list or len(bundle["attempts"]) > MAX_ATTEMPTS:
        raise BridgeError("Invalid attempt collection")
    seen_ops, seen_runs, results = set(), set(), []
    expected = deepcopy(prepared)
    expected["execution_policy"] = "local_builtin_opt_in"
    for attempt in bundle["attempts"]:
        receipts.exact(attempt, {"operation_id", "consented_at", "request", "receipt"})
        receipts.uid(attempt["operation_id"])
        receipts.text(attempt["consented_at"], 64)
        if receipts.canonical(attempt["request"]) != receipts.canonical(expected):
            raise BridgeError("Execution changed a prepared input or scope")
        result = receipts.verify(attempt["receipt"], attempt["request"], expected_public_key)
        run_id = attempt["receipt"]["body"]["run_id"]
        if attempt["operation_id"] in seen_ops or run_id in seen_runs:
            raise BridgeError("Duplicate operation or run")
        seen_ops.add(attempt["operation_id"])
        seen_runs.add(run_id)
        results.append(result)
    return results


class Tasks:
    def __init__(self, database: str | Path, signer: Ed25519PrivateKey):
        self.signer = signer
        self.db = sqlite3.connect(str(database), timeout=5, isolation_level=None)
        tables = {r[0] for r in self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if tables - {"receipt_tasks", "receipt_attempts", "receipt_handoffs"}:
            self.db.close()
            raise BridgeError("Use a separate receipt-task database, never the community database", 503)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS receipt_tasks(
              id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, revision_id TEXT NOT NULL,
              document TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS receipt_attempts(
              task_id TEXT NOT NULL REFERENCES receipt_tasks(id), operation_id TEXT NOT NULL,
              document TEXT NOT NULL, PRIMARY KEY(task_id,operation_id));
        """)

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def _task(self, task_id: str) -> dict:
        receipts.uid(task_id)
        row = self.db.execute("SELECT document FROM receipt_tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise BridgeError("Unknown verification task", 404)
        return json.loads(row[0])

    def _owned(self, task_id: str, actor_id: str) -> dict:
        task = self._task(task_id)
        if task["actor_id"] != actor_id:
            raise BridgeError("Only this request's creator can authorize or cancel it", 403)
        return task

    def prepare(self, actor_id: str, revision: dict, scope: str, problem: dict) -> dict:
        receipts.uid(actor_id)
        binding = revision_binding(revision)
        if len(receipts.canonical(problem)) > 32768:
            raise BridgeError("Input exceeds the 32 KiB local task limit", 413)
        request = receipts.make_request(**binding, scope=scope, problem=problem)
        task = {"id": request["request_id"], "actor_id": actor_id, "created_at": now(),
                "revision": deepcopy(revision), "prepared_request": request, "cancelled_at": None}
        with self.transaction():
            if self.db.execute("SELECT COUNT(*) FROM receipt_tasks").fetchone()[0] >= MAX_TASKS:
                raise BridgeError("Local verification task quota reached; publishing is unaffected", 413)
            self.db.execute("INSERT INTO receipt_tasks VALUES(?,?,?,?)",
                            (task["id"], actor_id, binding["revision_id"], json.dumps(task, ensure_ascii=False)))
        return self.view(task["id"])

    def view(self, task_id: str) -> dict:
        task = self._task(task_id)
        attempts = [json.loads(r[0]) for r in self.db.execute(
            "SELECT document FROM receipt_attempts WHERE task_id=? ORDER BY rowid", (task_id,))]
        return {"task": task, "attempts": attempts,
                "state": "cancelled" if task["cancelled_at"] else ("completed" if attempts else "prepared")}

    def list(self, revision_id: str) -> list[dict]:
        receipts.uid(revision_id)
        return [self.view(r[0]) for r in self.db.execute(
            "SELECT id FROM receipt_tasks WHERE revision_id=? ORDER BY rowid", (revision_id,))]

    def execute(self, task_id: str, actor_id: str, operation_id: str, consent: bool) -> dict:
        if consent is not True:
            raise BridgeError("Explicit local execution consent is required", 403)
        receipts.uid(operation_id)
        # Computation is bounded and synchronous. This transaction is the
        # linearization point: cancellation never pretends to interrupt a run.
        with self.transaction():
            task = self._owned(task_id, actor_id)
            existing = self.db.execute(
                "SELECT document FROM receipt_attempts WHERE task_id=? AND operation_id=?",
                (task_id, operation_id)).fetchone()
            if existing is not None:
                return json.loads(existing[0])  # retry delivery, never recompute
            if task["cancelled_at"]:
                raise BridgeError("Task cancelled; no further execution is allowed", 409)
            count = self.db.execute("SELECT COUNT(*) FROM receipt_attempts WHERE task_id=?", (task_id,)).fetchone()[0]
            if count >= MAX_ATTEMPTS:
                raise BridgeError("Local attempt quota reached", 413)
            request = deepcopy(task["prepared_request"])
            request["execution_policy"] = "local_builtin_opt_in"
            consented_at = now()
            receipt = receipts.PendingRun(request).execute(self.signer, allow_local_compute=True)
            receipts.verify(receipt, request, receipts.public_hex(self.signer))
            attempt = {"operation_id": operation_id, "consented_at": consented_at,
                       "request": request, "receipt": receipt}
            self.db.execute("INSERT INTO receipt_attempts VALUES(?,?,?)",
                            (task_id, operation_id, json.dumps(attempt, ensure_ascii=False)))
            return attempt

    def cancel(self, task_id: str, actor_id: str) -> dict:
        with self.transaction():
            task = self._owned(task_id, actor_id)
            if not task["cancelled_at"]:
                task["cancelled_at"] = now()
                self.db.execute("UPDATE receipt_tasks SET document=? WHERE id=?",
                                (json.dumps(task, ensure_ascii=False), task_id))
        return self.view(task_id)

    def export(self, task_id: str) -> dict:
        view = self.view(task_id)
        return {"schema": SCHEMA, "task": view["task"], "attempts": view["attempts"],
                "trust_note": "Pin a signer key separately. Signatures cover execution requests/results, not requester identity, cancellation, source meaning or originality."}

    def recheck(self, task_id: str, expected_public_key: str) -> dict:
        results = check_export(self.export(task_id), expected_public_key)
        return {"checks": results, "source_semantics_verified": False,
                "publication_authority": False, "requester_identity_certified": False}
