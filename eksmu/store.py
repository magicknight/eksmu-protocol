"""Append-only prototype records. Digests check integrity, NOT truth or signatures."""
from __future__ import annotations

from collections import OrderedDict
import copy
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FORMAT = "eksmu.bundle.v1"
_INHERIT = object()
MAX_RECORDS = 10_000
MAX_BUNDLE_BYTES = 16 * 1024 * 1024
KINDS = {"question", "conjecture", "finding", "experiment", "computation", "failed_attempt", "other"}
SOURCES = {"human", "ai", "lean", "computation"}
STATUSES = {"unreviewed", "supported", "challenged", "inconclusive", "resource_limited"}
FIELDS = {
    "actor": {"alias", "key_hash"},
    "exploration": {"kind"},
    "revision": {"exploration_id", "parent_revision_id", "title", "body", "note"},
    "comment": {"revision_id", "reply_to", "body"},
    "evidence": {"revision_id", "source", "tool", "scope", "method", "result", "limitations", "status", "service_verified"},
    "material": {"attachment_id", "revision_id", "size", "content_sha256", "origin", "derived_from_sha256"},
}


class Problem(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    try:
        return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise Problem("Value is not valid canonical UTF-8 JSON") from exc


def exact(value: Any, fields: set[str]) -> None:
    if not isinstance(value, dict) or set(value) != fields:
        raise Problem("Missing or unexpected fields")


def text(value: Any, limit: int = 50_000, empty: bool = False) -> None:
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise Problem("Invalid or oversized text")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise Problem("Invalid Unicode text") from exc


def display_name(value: Any, email: str | None = None) -> str:
    text(value, 80)
    cleaned = value.strip()
    if "@" in cleaned and "." in cleaned.split("@", 1)[1]:
        raise Problem("公开显示名称会永久公开，不能使用邮箱地址。请填写真实姓名或笔名。")
    if isinstance(email, str) and email.strip() and cleaned.casefold() == email.strip().casefold():
        raise Problem("公开显示名称会永久公开，不能使用邮箱地址。请填写真实姓名或笔名。")
    return cleaned


def identifier(value: Any) -> None:
    if not isinstance(value, str):
        raise Problem("Invalid record identifier")
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError
    except ValueError as exc:
        raise Problem("Invalid record identifier") from exc


def enum(value: Any, options: set[str]) -> None:
    if not isinstance(value, str) or value not in options:
        raise Problem("Unsupported option")


def record(kind: str, actor: str | None, payload: dict) -> dict:
    rid = str(uuid.uuid4())
    result = {"id": rid, "type": kind, "actor_id": actor or rid,
              "created_at": datetime.now(timezone.utc).isoformat(), "payload": payload}
    result["sha256"] = digest(result)
    return result


def validate(rows: list[dict]) -> None:
    """Validate schema, owner permissions and references without trusting the digest alone."""
    if not isinstance(rows, list) or len(rows) > MAX_RECORDS:
        raise Problem("Record limit exceeded")
    index: dict[str, dict] = {}
    heads: dict[str, str] = {}

    def ref(rid: Any, kind: str) -> dict:
        identifier(rid)
        found = index.get(rid)
        if found is None or found["type"] != kind:
            raise Problem("Missing or wrong-type reference")
        return found

    for row in rows:
        exact(row, {"id", "type", "actor_id", "created_at", "payload", "sha256"})
        identifier(row["id"])
        identifier(row["actor_id"])
        if row["id"] in index:
            raise Problem("Duplicate record identifier")
        enum(row["type"], set(FIELDS))
        text(row["created_at"], 64)
        try:
            stamp = datetime.fromisoformat(row["created_at"])
            if stamp.tzinfo is None or stamp.utcoffset().total_seconds() != 0:
                raise ValueError
        except ValueError as exc:
            raise Problem("A UTC timestamp is required") from exc
        unsigned = {k: v for k, v in row.items() if k != "sha256"}
        if row["sha256"] != digest(unsigned):
            raise Problem("Record digest mismatch")
        kind, p = row["type"], row["payload"]
        paper_fields = {'paper'} if kind == 'revision' and isinstance(p, dict) and 'paper' in p else set()
        exact(p, FIELDS[kind] | paper_fields)
        if kind == "actor":
            if row["actor_id"] != row["id"]:
                raise Problem("Actor identity mismatch")
            text(p["alias"], 80)
            text(p["key_hash"], 64)
            if len(p["key_hash"]) != 64 or any(c not in "0123456789abcdef" for c in p["key_hash"]):
                raise Problem("Invalid key fingerprint")
            if any(r["type"] == "actor" and r["payload"]["key_hash"] == p["key_hash"] for r in index.values()):
                raise Problem("Duplicate key fingerprint")
        else:
            ref(row["actor_id"], "actor")
        if kind == "exploration":
            enum(p["kind"], KINDS)
        elif kind == "revision":
            exp = ref(p["exploration_id"], "exploration")
            if exp["actor_id"] != row["actor_id"]:
                raise Problem("Only the author may revise this exploration", 403)
            if p["parent_revision_id"] != heads.get(exp["id"]):
                raise Problem("Stale or invalid parent revision", 409)
            for key, limit, empty in [("title", 200, False), ("body", 50_000, False), ("note", 2000, True)]:
                text(p[key], limit, empty)
            if paper_fields:
                from .papers import validate_paper
                validate_paper(p['paper'])
            heads[exp["id"]] = row["id"]
        elif kind == "material":
            revision = ref(p["revision_id"], "revision")
            if revision["actor_id"] != row["actor_id"]:
                raise Problem("Only the author may attach material", 403)
            identifier(p["attachment_id"])
            if type(p["size"]) is not int or p["size"] < 0:
                raise Problem("Invalid material size")
            for key in ("content_sha256", "derived_from_sha256"):
                value = p[key]
                if key == "derived_from_sha256" and value is None:
                    continue
                if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                    raise Problem("Invalid material digest")
            text(p["origin"], 80)
        elif kind in {"comment", "evidence"}:
            ref(p["revision_id"], "revision")
            if kind == "comment":
                text(p["body"])
                if p["reply_to"] is not None:
                    identifier(p["reply_to"])
                    target = index.get(p["reply_to"])
                    if not target or target["type"] not in {"comment", "evidence"} or target["payload"]["revision_id"] != p["revision_id"]:
                        raise Problem("Reply must target a comment or report on the same revision")
            else:
                enum(p["source"], SOURCES)
                enum(p["status"], STATUSES)
                for key in ("scope", "method", "result"):
                    text(p[key])
                text(p["tool"], 300, True)
                text(p["limitations"], 50_000, True)
                if p["service_verified"] is not False:
                    raise Problem("This prototype never certifies submitted reports")
        index[row["id"]] = row
    if any(r["type"] == "exploration" and r["id"] not in heads for r in rows):
        raise Problem("Exploration missing its first revision")


class _RowsSnapshot:
    __slots__ = ("version", "max_rowid", "chain_seq", "chain_head", "rows", "actors")

    def __init__(self, version: tuple, max_rowid: int, chain_seq: int, chain_head: str,
                 rows: tuple[dict, ...], actors: tuple[dict, ...]):
        self.version = version
        self.max_rowid = max_rowid
        self.chain_seq = chain_seq
        self.chain_head = chain_head
        self.rows = rows
        self.actors = actors


_ROWS_CACHE_MAX = 32
_ROWS_CACHE: OrderedDict[str, _RowsSnapshot] = OrderedDict()
_CACHE_LOCK = threading.RLock()


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._cache_key = None if str(path) == ":memory:" else str(self.path.resolve())
        self.db = sqlite3.connect(str(path), timeout=10)
        self.db.execute("CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, document TEXT NOT NULL)")
        from . import record_chain
        record_chain.ensure(self.db)
        self.db.commit()
        record_chain.backfill(self.db)

    def close(self) -> None:
        self.db.close()

    def _db_version(self) -> tuple:
        try:
            st = os.stat(self.path)
            inode = (st.st_dev, st.st_ino)
        except OSError:
            inode = (0, 0)
        rec_tip = self.db.execute("SELECT rowid, id FROM records ORDER BY rowid DESC LIMIT 1").fetchone()
        max_rowid = rec_tip[0] if rec_tip else 0
        last_id = rec_tip[1] if rec_tip else ""
        try:
            chain_tip = self.db.execute("SELECT seq, head FROM record_chain ORDER BY seq DESC LIMIT 1").fetchone()
            chain_seq = chain_tip[0] if chain_tip else 0
            chain_head = chain_tip[1] if chain_tip else ""
        except sqlite3.OperationalError:
            chain_seq, chain_head = 0, ""
        count = max_rowid if max_rowid == chain_seq else self.db.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        return (inode, max_rowid, last_id, chain_seq, chain_head, count)

    def _snapshot(self) -> _RowsSnapshot:
        key = self._cache_key
        if key is None:
            rows = tuple(json.loads(r[0]) for r in self.db.execute("SELECT document FROM records ORDER BY rowid"))
            actors = tuple(r for r in rows if r.get("type") == "actor")
            return _RowsSnapshot((), len(rows), 0, "", rows, actors)
        ver = self._db_version()
        _, max_rowid, _, chain_seq, chain_head, _ = ver
        with _CACHE_LOCK:
            cached = _ROWS_CACHE.get(key)
            if cached is not None and cached.version == ver:
                _ROWS_CACHE.move_to_end(key)
                return cached
        rows = None
        actors = None
        if (cached is not None and cached.version[0] == ver[0]
                and max_rowid > cached.max_rowid and chain_seq == max_rowid
                and cached.chain_seq == cached.max_rowid and cached.chain_seq > 0):
            try:
                prev = self.db.execute("SELECT head FROM record_chain WHERE seq = ?", (cached.chain_seq,)).fetchone()
            except sqlite3.OperationalError:
                prev = None
            if prev is not None and prev[0] == cached.chain_head:
                added = tuple(json.loads(r[0]) for r in self.db.execute(
                    "SELECT document FROM records WHERE rowid > ? ORDER BY rowid", (cached.max_rowid,)))
                if len(added) == max_rowid - cached.max_rowid:
                    rows = cached.rows + added
                    actors = cached.actors + tuple(r for r in added if r.get("type") == "actor")
        if rows is None:
            rows = tuple(json.loads(r[0]) for r in self.db.execute("SELECT document FROM records ORDER BY rowid"))
            actors = tuple(r for r in rows if r.get("type") == "actor")
        snap = _RowsSnapshot(ver, max_rowid, chain_seq, chain_head, rows, actors)
        if not self.db.in_transaction and self._db_version() == ver:
            with _CACHE_LOCK:
                cur = _ROWS_CACHE.get(key)
                if not (cur is not None and cur.version[0] == ver[0]
                        and cur.chain_seq == cur.max_rowid and cur.max_rowid > max_rowid):
                    _ROWS_CACHE[key] = snap
                    _ROWS_CACHE.move_to_end(key)
                    while len(_ROWS_CACHE) > _ROWS_CACHE_MAX:
                        _ROWS_CACHE.popitem(last=False)
        return snap

    def rows(self) -> list[dict]:
        return list(self._snapshot().rows)

    def _actors(self) -> tuple[dict, ...]:
        return self._snapshot().actors

    def authenticate(self, token: str) -> str:
        from .accounts import authenticate
        return authenticate(self, token)

    def _legacy_authenticate(self, token: str) -> str:
        text(token, 128)
        fingerprint = hashlib.sha256(token.encode()).hexdigest()
        for row in self._actors():
            if hmac.compare_digest(row["payload"]["key_hash"], fingerprint):
                return row["id"]
        raise Problem("Unknown identity key", 401)

    def _append(self, additions: list[dict], empty_only: bool = False, *, token=None) -> None:
        if not isinstance(additions, list):
            raise Problem("Records must be an array")
        from .accounts import transaction
        with transaction(self.db):
            if token is not None:
                actor = self.authenticate(token)
                if any(row['actor_id'] != actor for row in additions):
                    raise Problem('Author changed during authorization', 403)
            existing = self.rows()
            if empty_only and existing:
                raise Problem("Import requires an empty database; no data was changed", 409)
            combined = existing + additions
            validate(combined)
            if len(canonical(combined).encode("utf-8")) > MAX_BUNDLE_BYTES - 1024:
                raise Problem("Prototype storage limit reached", 413)
            self.db.executemany("INSERT INTO records(id,document) VALUES (?,?)", [(r["id"], canonical(r)) for r in additions])
            from . import record_chain
            record_chain.append(self.db, additions)

    def register(self, alias: str = "探索者") -> dict:
        display_name(alias)
        token = secrets.token_urlsafe(32)
        actor = record("actor", None, {"alias": alias, "key_hash": hashlib.sha256(token.encode()).hexdigest()})
        self._append([actor])
        return {"actor": actor, "token": token}

    def publish(self, token: str, body: str, title: str = "", kind: str = "question", paper=None) -> dict:
        actor = self.authenticate(token)
        if isinstance(title, str) and not title.strip() and isinstance(body, str):
            # The web form requires a title; older and scripted clients may
            # omit it, so name the work after its opening words, not "untitled".
            from .research_render import excerpt
            title = excerpt(body.strip().split("\n", 1)[0], 80) or excerpt(body, 80) or "未命名作品"
        exp = record("exploration", actor, {"kind": kind})
        payload = {"exploration_id": exp["id"], "parent_revision_id": None,
                   "title": title, "body": body, "note": "Initial version"}
        if paper is not None:
            payload['paper'] = paper
        rev = record("revision", actor, payload)
        self._append([exp, rev], token=token)
        return {"exploration": exp, "revision": rev}

    def revise(self, token: str, exploration_id: str, parent_revision_id: str, title: str, body: str, note: str = "", paper=_INHERIT) -> dict:
        actor = self.authenticate(token)
        # Older clients omit this field. Preserve metadata from their explicit
        # parent, never silently use metadata from a newer revision.
        if paper is _INHERIT:
            parent = next((r for r in self.rows() if r['id'] == parent_revision_id and r['type'] == 'revision'), None)
            paper = parent['payload'].get('paper') if parent else None
        payload = {"exploration_id": exploration_id, "parent_revision_id": parent_revision_id,
                   "title": title, "body": body, "note": note}
        if paper is not None:
            payload['paper'] = paper
        rev = record("revision", actor, payload)
        self._append([rev], token=token)
        return rev

    def comment(self, token: str, revision_id: str, body: str, reply_to: str | None = None) -> dict:
        row = record("comment", self.authenticate(token), {"revision_id": revision_id, "body": body, "reply_to": reply_to})
        self._append([row], token=token)
        return row

    def evidence(self, token: str, revision_id: str, source: str, scope: str, method: str, result: str,
                 status: str = "unreviewed", tool: str = "", limitations: str = "") -> dict:
        row = record("evidence", self.authenticate(token), {"revision_id": revision_id, "source": source,
            "scope": scope, "method": method, "result": result, "status": status, "tool": tool,
            "limitations": limitations, "service_verified": False})
        self._append([row], token=token)
        return row

    def export(self) -> dict:
        from .papers import BUNDLE_FORMAT
        rows = copy.deepcopy(self.rows())
        extended = any(r['type'] == 'revision' and 'paper' in r['payload'] for r in rows)
        return {"format": BUNDLE_FORMAT if extended else FORMAT, "records": rows, "sha256": digest(rows)}

    def import_bundle(self, bundle: dict) -> None:
        from .papers import BUNDLE_FORMAT
        exact(bundle, {"format", "records", "sha256"})
        if bundle["format"] not in (FORMAT, BUNDLE_FORMAT) or bundle["sha256"] != digest(bundle["records"]):
            raise Problem("Unsupported bundle or bundle digest mismatch")
        if not isinstance(bundle['records'], list):
            raise Problem('Records must be an array')
        if bundle['format'] == FORMAT and any(isinstance(r, dict) and isinstance(r.get('payload'), dict)
                and 'paper' in r['payload'] for r in bundle['records']):
            raise Problem('Paper metadata requires the publication bundle format')
        self._append(bundle["records"], empty_only=True)
