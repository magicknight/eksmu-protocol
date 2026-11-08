"""Offline, separately stored verification requests and authenticated fixture receipts.

No network, shell, user-code execution or community-database write capability.
HMAC authenticates only within this configured local lab; it is not a public
signature, an identity system, or evidence that a mathematical claim is true.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TASK_SCHEMA = "eksmu.verification-task.fixture.v1"
RECEIPT_SCHEMA = "eksmu.verification-receipt.fixture.v1"
AUDIT_SCHEMA = "eksmu.verification-audit.fixture.v1"
OUTCOMES = {"check_completed", "check_failed", "incomplete", "counterexample",
            "unsupported", "resource_limited", "execution_error", "inconclusive"}
KINDS = {"lean_check", "ai_review", "computation_reproduction"}
POLICY = {"network": False, "external_processing": False, "user_code": False,
          "publication_write": False, "funds": False, "fixture_only": True}
BUDGET = {"wall_seconds": 60, "memory_mib": 256, "output_bytes": 65536}
LOG = "PROTOCOL FIXTURE ONLY: no AI, Lean, user program or research evaluation ran."


class Rejected(ValueError):
    """A request failed without a successful state transition."""


def canonical(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise Rejected("Invalid UTF-8 JSON") from exc


def sha(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def fields(value: Any, names: set[str]) -> None:
    if not isinstance(value, dict) or set(value) != names:
        raise Rejected("Missing or unexpected fields")


def text(value: Any, limit: int = 2000) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise Rejected("Invalid text")
    canonical(value)


def uid(value: Any) -> None:
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except ValueError as exc:
        raise Rejected("Invalid UUID") from exc


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def fingerprint(token: Any) -> str:
    text(token, 128)
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def revision_binding(revision: dict) -> dict:
    """Read an explicit revision (optionally with M3 metadata), without DB I/O."""
    fields(revision, {"id", "type", "actor_id", "created_at", "payload", "sha256"})
    if revision["type"] != "revision":
        raise Rejected("Expected an explicit revision record")
    uid(revision["id"])
    uid(revision["actor_id"])
    text(revision["created_at"], 64)
    try:
        stamp = datetime.fromisoformat(revision["created_at"])
        if stamp.tzinfo is None or stamp.utcoffset().total_seconds() != 0:
            raise ValueError
    except ValueError as exc:
        raise Rejected("Revision requires a UTC timestamp") from exc
    p = revision["payload"]
    extended = isinstance(p, dict) and 'paper' in p
    fields(p, {"exploration_id", "parent_revision_id", "title", "body", "note"} | ({'paper'} if extended else set()))
    if extended:
        from eksmu.papers import validate_paper
        try:
            validate_paper(p['paper'])
        except ValueError as exc:
            raise Rejected('Invalid paper metadata') from exc
    uid(p["exploration_id"])
    if p["parent_revision_id"] is not None:
        uid(p["parent_revision_id"])
    text(p["title"], 200)
    text(p["body"], 50000)
    if not isinstance(p["note"], str) or len(p["note"]) > 2000:
        raise Rejected("Invalid revision note")
    unsigned = {k: v for k, v in revision.items() if k != "sha256"}
    if revision["sha256"] != sha(unsigned):
        raise Rejected("Revision checksum mismatch")
    return {"revision_id": revision["id"], "revision_sha256": revision["sha256"]}


def read_v1_revision(bundle: dict, revision_id: str) -> dict:
    """Read a known v1 backup. Do not silently interpret ambiguous v2 variants."""
    fields(bundle, {"format", "records", "sha256"})
    if bundle["format"] != "eksmu.bundle.v1":
        raise Rejected("Unsupported community format; v2 candidate compatibility is not established")
    rows = bundle["records"]
    if not isinstance(rows, list) or len(rows) > 10000 or len(canonical(rows)) > 16 * 1024 * 1024:
        raise Rejected("Invalid or oversized backup")
    if sha(rows) != bundle["sha256"]:
        raise Rejected("Backup checksum mismatch")
    uid(revision_id)
    found = [r for r in rows if isinstance(r, dict) and r.get("id") == revision_id]
    if len(found) != 1:
        raise Rejected("Revision missing or duplicated")
    if isinstance(found[0].get('payload'), dict) and 'paper' in found[0]['payload']:
        raise Rejected('Paper metadata cannot be labeled as a legacy v1 bundle')
    revision_binding(found[0])
    return json.loads(canonical(found[0]))


def validate_budget(value: dict) -> None:
    fields(value, set(BUDGET))
    for key, cap in BUDGET.items():
        if type(value[key]) is not int or not 1 <= value[key] <= cap:
            raise Rejected("Invalid budget; these fields do not implement an execution sandbox")


def sign_fixture(ticket: dict, worker_key: bytes, outcome: str) -> dict:
    """A worker-side fixed fixture, not a scientific checker. Input text is unused."""
    if not isinstance(outcome, str) or outcome not in OUTCOMES:
        raise Rejected("Unknown fixture outcome")
    if not isinstance(worker_key, bytes) or len(worker_key) < 32:
        raise Rejected("A worker key of at least 32 bytes is required")
    body = {"schema": RECEIPT_SCHEMA, "ticket": ticket, "outcome": outcome,
            "simulation": True, "academic_verification": False,
            "tool": "eksmu-fixed-fixture/0.1.0", "log": LOG,
            "log_sha256": hashlib.sha256(LOG.encode()).hexdigest()}
    return {"body": body, "hmac_sha256": hmac.new(worker_key, canonical(body), "sha256").hexdigest()}


class Lab:
    """An explicitly local fixture coordinator with a separate SQLite database.

    Task control keys can authorize/cancel only their task. Worker keys are
    supplied by the local operator, never accepted from participant reports.
    A DB owner or HMAC-key holder remains trusted. This is not a secure sandbox.
    """
    def __init__(self, path: str | Path, workers: dict[str, bytes]):
        self.workers = dict(workers)
        for name, key in self.workers.items():
            text(name, 80)
            if not name.startswith("fixture.") or not isinstance(key, bytes) or len(key) < 32:
                raise Rejected("Only configured fixture workers are supported")
        self.db = sqlite3.connect(str(path), timeout=5, isolation_level=None)
        self.db.execute("PRAGMA foreign_keys=ON")
        existing = {r[0] for r in self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if existing - {"lab_tasks", "lab_grants"}:
            self.db.close()
            raise Rejected("Use a separate lab database, not a community or unrelated database")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS lab_tasks(
            id TEXT PRIMARY KEY, descriptor TEXT NOT NULL, control_hash TEXT NOT NULL,
            cancelled_at TEXT);
          CREATE TABLE IF NOT EXISTS lab_grants(
            id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES lab_tasks(id),
            verifier TEXT NOT NULL, ticket TEXT, receipt TEXT, acceptance TEXT);
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

    def _task(self, task_id: str):
        uid(task_id)
        row = self.db.execute("SELECT descriptor,control_hash,cancelled_at FROM lab_tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise Rejected("Unknown task")
        return json.loads(row[0]), row[1], row[2]

    def _owner(self, task_id: str, key: str):
        task, stored, cancelled = self._task(task_id)
        if not hmac.compare_digest(stored, fingerprint(key)):
            raise Rejected("Task control key is not authorized")
        return task, cancelled

    def _worker(self, name: str, key: bytes):
        if not isinstance(name, str) or not isinstance(key, bytes) or name not in self.workers:
            raise Rejected("Unknown verifier")
        if not hmac.compare_digest(self.workers[name], key):
            raise Rejected("Verifier authentication failed")

    def _grant(self, grant_id: str):
        uid(grant_id)
        row = self.db.execute("SELECT task_id,verifier,ticket,receipt,acceptance FROM lab_grants WHERE id=?", (grant_id,)).fetchone()
        if row is None:
            raise Rejected("Unknown grant")
        return row

    def prepare(self, revision: dict, scope: str, check_kind: str, budget: dict | None = None) -> dict:
        binding = revision_binding(revision)
        text(scope)
        if not isinstance(check_kind, str) or check_kind not in KINDS:
            raise Rejected("Unsupported requested check")
        budget = dict(BUDGET) if budget is None else budget
        validate_budget(budget)
        task_id, control = str(uuid.uuid4()), secrets.token_urlsafe(32)
        task = {"schema": TASK_SCHEMA, "id": task_id, **binding, "scope": scope,
                "check_kind": check_kind, "budget": budget, "policy": dict(POLICY)}
        task["sha256"] = sha(task)
        with self.transaction():
            self.db.execute("INSERT INTO lab_tasks VALUES(?,?,?,NULL)",
                            (task_id, canonical(task).decode(), fingerprint(control)))
        return {"task": task, "control_key": control}

    def authorize(self, task_id: str, control_key: str, verifier: str) -> str:
        if not isinstance(verifier, str) or verifier not in self.workers:
            raise Rejected("Unknown verifier")
        grant_id = str(uuid.uuid4())
        with self.transaction():
            _, cancelled = self._owner(task_id, control_key)
            if cancelled:
                raise Rejected("Cancelled tasks cannot receive new execution grants")
            self.db.execute("INSERT INTO lab_grants(id,task_id,verifier) VALUES(?,?,?)", (grant_id, task_id, verifier))
        return grant_id

    def begin(self, grant_id: str, verifier: str, worker_key: bytes) -> dict:
        with self.transaction():
            self._worker(verifier, worker_key)
            task_id, assigned, ticket, _, _ = self._grant(grant_id)
            if verifier != assigned:
                raise Rejected("Grant belongs to a different verifier")
            task, _, cancelled = self._task(task_id)
            if cancelled:
                raise Rejected("Task cancelled before this start or retry")
            if ticket is not None:
                return json.loads(ticket)
            result = {"attempt_id": str(uuid.uuid4()), "grant_id": grant_id,
                      "task_id": task_id, "task_sha256": task["sha256"],
                      "verifier": verifier, "revision_id": task["revision_id"],
                      "revision_sha256": task["revision_sha256"], "started_at": timestamp()}
            self.db.execute("UPDATE lab_grants SET ticket=? WHERE id=?", (canonical(result).decode(), grant_id))
            return result

    def finish(self, packet: dict) -> dict:
        fields(packet, {"body", "hmac_sha256"})
        body = packet["body"]
        fields(body, {"schema", "ticket", "outcome", "simulation", "academic_verification", "tool", "log", "log_sha256"})
        ticket = body["ticket"]
        fields(ticket, {"attempt_id", "grant_id", "task_id", "task_sha256", "verifier", "revision_id", "revision_sha256", "started_at"})
        name = ticket["verifier"]
        if not isinstance(name, str) or name not in self.workers:
            raise Rejected("Unknown verifier")
        tag = packet["hmac_sha256"]
        if not isinstance(tag, str) or len(tag) != 64 or any(c not in "0123456789abcdef" for c in tag):
            raise Rejected("Invalid receipt authentication tag")
        expected = hmac.new(self.workers[name], canonical(body), "sha256").hexdigest()
        if not hmac.compare_digest(expected, tag):
            raise Rejected("Receipt authentication failed")
        if (body["schema"] != RECEIPT_SCHEMA or body["simulation"] is not True or
                body["academic_verification"] is not False or body["tool"] != "eksmu-fixed-fixture/0.1.0" or
                body["log"] != LOG or body["log_sha256"] != hashlib.sha256(LOG.encode()).hexdigest() or
                not isinstance(body["outcome"], str) or body["outcome"] not in OUTCOMES):
            raise Rejected("Only explicit fixed fixture results are accepted")
        with self.transaction():
            task_id, assigned, stored_ticket, previous, acceptance = self._grant(ticket["grant_id"])
            if assigned != name or stored_ticket is None or json.loads(stored_ticket) != ticket:
                raise Rejected("Receipt does not match the issued execution ticket")
            if previous is not None:
                if json.loads(previous) != packet:
                    raise Rejected("A different result cannot overwrite an existing receipt")
                return json.loads(acceptance)
            task, _, cancelled = self._task(task_id)
            if len(canonical(packet)) > task["budget"]["output_bytes"]:
                raise Rejected("Receipt exceeds the declared output limit")
            result = {"receipt_sha256": sha(packet), "received_at": timestamp(),
                      "received_after_cancellation": cancelled is not None,
                      "authentication": "local_configured_hmac", "academic_verification": False}
            self.db.execute("UPDATE lab_grants SET receipt=?,acceptance=? WHERE id=?",
                            (canonical(packet).decode(), canonical(result).decode(), ticket["grant_id"]))
            return result

    def cancel(self, task_id: str, control_key: str) -> str:
        with self.transaction():
            _, cancelled = self._owner(task_id, control_key)
            if cancelled is None:
                cancelled = timestamp()
                self.db.execute("UPDATE lab_tasks SET cancelled_at=? WHERE id=?", (cancelled, task_id))
            return cancelled

    def export(self) -> dict:
        # A consistent read snapshot, without controls or worker secrets.
        with self.transaction():
            tasks = [{"task": json.loads(d), "cancelled_at": c} for d, c in
                     self.db.execute("SELECT descriptor,cancelled_at FROM lab_tasks ORDER BY rowid")]
            grants = [{"id": i, "task_id": t, "verifier": v,
                       "ticket": json.loads(k) if k else None,
                       "receipt": json.loads(r) if r else None,
                       "acceptance": json.loads(a) if a else None} for i,t,v,k,r,a in
                      self.db.execute("SELECT id,task_id,verifier,ticket,receipt,acceptance FROM lab_grants ORDER BY rowid")]
        result = {"schema": AUDIT_SCHEMA, "tasks": tasks, "grants": grants,
                  "warning": "Private audit only. Hashes are not public signatures or scientific validation."}
        result["sha256"] = sha(result)
        return result


def run_fixture(lab: Lab, grant_id: str, verifier: str, worker_key: bytes, outcome: str) -> dict:
    """Explicit fixture dispatch only; there is no user-specified command or URL."""
    ticket = lab.begin(grant_id, verifier, worker_key)
    return lab.finish(sign_fixture(ticket, worker_key, outcome))
