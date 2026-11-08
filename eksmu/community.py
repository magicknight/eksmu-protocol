"""Topic discovery and personal participation, independent of proof judgements.

A snapshot of existing records is read, never edited. Personal follows/bookmarks
are not public popularity scores. The sidecar can fail without blocking posts.
"""
from __future__ import annotations

from collections import Counter, OrderedDict, defaultdict
from contextlib import contextmanager
import json
from pathlib import Path
import re
import sqlite3
import threading
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone

from .research_render import excerpt
from .store import Problem, canonical, exact, identifier, text

PERSONAL_FORMAT = "eksmu.community-personal.v1"
VIEWS = {"all", "unanswered", "collaborate", "following", "bookmarks", "mine", "labeled"}
MAX_PREFERENCES = 512
BEHAVIOR_CATEGORIES = {"privacy", "harassment", "spam", "other_behavior"}
BEHAVIOR_OUTCOMES = {"no_action", "follow_up"}
BEHAVIOR_APPEAL_OUTCOMES = {"uphold", "reopen"}
MAX_BEHAVIOR_CASES_PER_ACTOR = 20
LABEL_KINDS = {"commercial_promotion", "pending_review"}
LABEL_STATES = {"active", "retracted"}
LABEL_SOURCES = {"maintainer", "ai"}
LABEL_APPEAL_DECISIONS = {"uphold", "retract"}
COOLING_OFF_HOURS = 72
PROMOTION_CONFIDENCE_THRESHOLD = 0.8
PROMOTION_OPERATOR = "ai_catalog_triage"
DEFAULT_AI_PROMOTION_REASON = "疑似以推广商品或服务为主要目的"


def _normalize_promotion_triage(raw: dict | None) -> dict | None:
    if not isinstance(raw, dict):
        return None
    likely = raw.get("likely")
    if not isinstance(likely, bool):
        return None
    conf = raw.get("confidence", 0.0)
    if isinstance(conf, bool) or not isinstance(conf, (int, float)):
        return None
    try:
        conf_val = float(conf)
    except (TypeError, ValueError):
        return None
    if conf_val != conf_val:  # NaN check
        return None
    conf_val = round(max(0.0, min(1.0, conf_val)), 4)
    reasons_raw = raw.get("reasons")
    reasons: list[str] = []
    if isinstance(reasons_raw, list):
        for item in reasons_raw:
            if isinstance(item, str):
                cleaned = " ".join(item.split()).strip()
                if cleaned:
                    cleaned = cleaned[:120]
                    if cleaned not in reasons:
                        reasons.append(cleaned)
            if len(reasons) >= 5:
                break
    evidence_raw = raw.get("evidence")
    evidence: list[str] = []
    if isinstance(evidence_raw, list):
        for item in evidence_raw:
            if isinstance(item, str):
                cleaned = " ".join(item.split()).strip()
                if cleaned:
                    cleaned = cleaned[:300]
                    if cleaned not in evidence:
                        evidence.append(cleaned)
            if len(evidence) >= 5:
                break
    return {
        "likely": likely,
        "confidence": conf_val,
        "reasons": reasons,
        "evidence": evidence,
    }


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def _parse_now(now=None) -> datetime:
    if now is None:
        now = utc_now()
    if isinstance(now, str):
        dt = datetime.fromisoformat(now)
    elif isinstance(now, datetime):
        dt = now
    else:
        raise ValueError("Invalid timestamp")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def operator_label(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{2,63}", value):
        raise Problem("需要3至64位非秘密操作者标识，仅使用英文字母、数字、下划线或连接号")
    return value


def topic_name(value: str) -> str:
    text(value, 80)
    value = " ".join(unicodedata.normalize("NFKC", value).casefold().split())
    text(value, 32)
    return value


def topics_list(values: list[str]) -> list[str]:
    if type(values) is not list or len(values) > 8:
        raise Problem("最多添加 8 个主题")
    return sorted({topic_name(v) for v in values})


class _DerivedIndex:
    __slots__ = ("rows_tuple", "index", "positions", "works", "work_numbers", "versions",
                 "activity", "comments", "reports", "responses", "search_body",
                 "comments_by_rev", "evidence_by_rev", "works_by_actor", "comments_by_actor")

    def __init__(self, records: list[dict]):
        self.rows_tuple = tuple(records)
        self.index = {r["id"]: r for r in records}
        self.positions = {r["id"]: n for n, r in enumerate(records)}
        self.works = {r["id"]: r for r in records if r["type"] == "exploration"}
        self.work_numbers = {wid: idx for idx, wid in enumerate(self.works, 1)}
        self.versions = defaultdict(list)
        self.activity = {}
        self.comments, self.reports, self.responses = Counter(), Counter(), Counter()
        self.comments_by_rev = defaultdict(list)
        self.evidence_by_rev = Counter()
        self.works_by_actor = defaultdict(list)
        self.comments_by_actor = Counter()
        for n, row in enumerate(records):
            p = row["payload"]
            rtype = row["type"]
            if rtype == "exploration":
                self.works_by_actor[row["actor_id"]].append((n, row))
            elif rtype == "revision":
                self.versions[p["exploration_id"]].append(row)
                self.activity[p["exploration_id"]] = n
            elif rtype in {"comment", "evidence"}:
                rev_id = p["revision_id"]
                if rtype == "comment":
                    self.comments_by_rev[rev_id].append(row)
                    self.comments_by_actor[row["actor_id"]] += 1
                else:
                    self.evidence_by_rev[rev_id] += 1
                rev_row = self.index.get(rev_id)
                if rev_row is not None:
                    work_id = rev_row["payload"]["exploration_id"]
                    (self.comments if rtype == "comment" else self.reports)[work_id] += 1
                    work_row = self.works.get(work_id)
                    if work_row is not None:
                        self.responses[work_id] += row["actor_id"] != work_row["actor_id"]
                    self.activity[work_id] = n
        self.search_body: dict[str, str] = {}


_DERIVED_CACHE_MAX = 16
_DERIVED_CACHE: OrderedDict[tuple, _DerivedIndex] = OrderedDict()
_DERIVED_LOCK = threading.RLock()


def _get_derived_index(records: list[dict]) -> _DerivedIndex:
    if not records:
        return _DerivedIndex(records)
    first, last = records[0], records[-1]
    key = (len(records), first.get("id"), last.get("id"), first.get("sha256"), last.get("sha256"))
    rows_tuple = tuple(records)
    with _DERIVED_LOCK:
        cached = _DERIVED_CACHE.get(key)
        if cached is not None and cached.rows_tuple == rows_tuple:
            _DERIVED_CACHE.move_to_end(key)
            return cached
    built = _DerivedIndex(records)
    with _DERIVED_LOCK:
        _DERIVED_CACHE[key] = built
        _DERIVED_CACHE.move_to_end(key)
        while len(_DERIVED_CACHE) > _DERIVED_CACHE_MAX:
            _DERIVED_CACHE.popitem(last=False)
    return built


_LABEL_FIELDS = ("id", "exploration_id", "label", "state", "reason", "source",
                 "operator", "created_at", "retracted_at", "retracted_by", "retract_reason")
_APPEAL_FIELDS = ("id", "label_id", "exploration_id", "author_id", "body", "state",
                  "decision", "decision_reason", "reviewer", "created_at", "reviewed_at")
_TRUSTED_AUTHOR_FIELDS = ("actor_id", "state", "reason", "operator", "created_at",
                          "revoked_at", "revoked_by", "revoke_reason")


def _read_trusted_authors_from_conn(conn: sqlite3.Connection) -> set[str]:
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='hub_trusted_authors'"
    ).fetchone()
    if not has_table:
        return set()
    rows = conn.execute(
        "SELECT actor_id FROM hub_trusted_authors WHERE state='active' ORDER BY rowid"
    ).fetchall()
    return {r[0] for r in rows}


def load_trusted_authors(db_path: str | Path) -> set[str]:
    sidecar = Path(str(db_path)) if str(db_path).endswith(".community.sqlite") else Path(f"{db_path}.community.sqlite")
    if not sidecar.exists():
        return set()
    conn = sqlite3.connect(str(sidecar), timeout=5)
    try:
        return _read_trusted_authors_from_conn(conn)
    finally:
        conn.close()


def _public_label(row_dict: dict | None) -> dict | None:
    if not row_dict:
        return None
    return {
        "id": row_dict["id"],
        "exploration_id": row_dict["exploration_id"],
        "label": row_dict["label"],
        "state": row_dict["state"],
        "reason": row_dict["reason"],
        "source": row_dict["source"],
        "created_at": row_dict["created_at"],
    }


def _read_active_labels_from_conn(conn: sqlite3.Connection) -> dict[str, dict]:
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='hub_labels'"
    ).fetchone()
    if not has_table:
        return {}
    rows = conn.execute(
        "SELECT id,exploration_id,label,state,reason,source,operator,created_at,"
        "retracted_at,retracted_by,retract_reason FROM hub_labels WHERE state='active' ORDER BY rowid"
    ).fetchall()
    return {r[1]: dict(zip(_LABEL_FIELDS, r)) for r in rows}


def load_active_labels(db_path: str | Path) -> dict[str, dict]:
    sidecar = Path(str(db_path)) if str(db_path).endswith(".community.sqlite") else Path(f"{db_path}.community.sqlite")
    if not sidecar.exists():
        return {}
    conn = sqlite3.connect(str(sidecar), timeout=5)
    try:
        return _read_active_labels_from_conn(conn)
    finally:
        conn.close()


def load_author_label_status(db_path: str | Path, exploration_id: str, author_id: str) -> dict | None:
    sidecar = Path(str(db_path)) if str(db_path).endswith(".community.sqlite") else Path(f"{db_path}.community.sqlite")
    if not sidecar.exists():
        return None
    conn = sqlite3.connect(str(sidecar), timeout=5)
    try:
        return _read_author_label_status_from_conn(conn, exploration_id, author_id)
    finally:
        conn.close()


def _read_author_label_status_from_conn(conn: sqlite3.Connection, exploration_id: str, author_id: str) -> dict | None:
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='hub_labels'"
    ).fetchone()
    if not has_table:
        return None
    active_row = conn.execute(
        "SELECT id,exploration_id,label,state,reason,source,operator,created_at,"
        "retracted_at,retracted_by,retract_reason FROM hub_labels "
        "WHERE exploration_id=? AND state='active' ORDER BY rowid DESC LIMIT 1",
        (exploration_id,),
    ).fetchone()
    active_lbl = dict(zip(_LABEL_FIELDS, active_row)) if active_row else None
    appeal_row = None
    if active_lbl is not None:
        appeal_row = conn.execute(
            "SELECT id,label_id,exploration_id,author_id,body,state,decision,decision_reason,"
            "reviewer,created_at,reviewed_at FROM hub_label_appeals WHERE label_id=? AND author_id=? ORDER BY rowid DESC LIMIT 1",
            (active_lbl["id"], author_id),
        ).fetchone()
    if appeal_row is None:
        appeal_row = conn.execute(
            "SELECT id,label_id,exploration_id,author_id,body,state,decision,decision_reason,"
            "reviewer,created_at,reviewed_at FROM hub_label_appeals WHERE exploration_id=? AND author_id=? ORDER BY rowid DESC LIMIT 1",
            (exploration_id, author_id),
        ).fetchone()
    appeal_dict = None
    if appeal_row is not None:
        raw_appeal = dict(zip(_APPEAL_FIELDS, appeal_row))
        lbl_row = conn.execute(
            "SELECT label,state,reason FROM hub_labels WHERE id=?",
            (raw_appeal["label_id"],),
        ).fetchone()
        appeal_dict = {
            "id": raw_appeal["id"],
            "label_id": raw_appeal["label_id"],
            "exploration_id": raw_appeal["exploration_id"],
            "author_id": raw_appeal["author_id"],
            "label": lbl_row[0] if lbl_row else (active_lbl["label"] if active_lbl else ""),
            "label_state": lbl_row[1] if lbl_row else (active_lbl["state"] if active_lbl else ""),
            "label_reason": lbl_row[2] if lbl_row else (active_lbl["reason"] if active_lbl else ""),
            "body": raw_appeal["body"],
            "state": raw_appeal["state"],
            "decision": raw_appeal["decision"],
            "decision_reason": raw_appeal["decision_reason"],
            "created_at": raw_appeal["created_at"],
            "reviewed_at": raw_appeal["reviewed_at"],
        }
    if active_lbl is None and appeal_dict is None:
        return None
    return {
        "active_label": _public_label(active_lbl),
        "can_appeal": bool(active_lbl is not None and (appeal_dict is None or appeal_dict["label_id"] != active_lbl["id"])),
        "appeal": appeal_dict,
    }


def _direct_cooling_passed(work_id: str, derived: _DerivedIndex, now_dt: datetime) -> bool:
    work = derived.works.get(work_id)
    if work is None:
        return False
    if derived.responses[work_id] > 0:
        return True
    created_dt = _parse_now(work["created_at"])
    return (now_dt - created_dt) >= timedelta(hours=COOLING_OFF_HOURS)


def distribution(
    work_id: str,
    *,
    rows: list[dict] | None = None,
    derived: _DerivedIndex | None = None,
    community_db: sqlite3.Connection | None = None,
    db_path: str | Path | None = None,
    active_labels: dict[str, dict] | None = None,
    trusted_authors: set[str] | None = None,
    withdrawn: bool = False,
    withdrawn_ids: set[str] | None = None,
    now: str | datetime | None = None,
) -> dict:
    """Unified distribution judgement for a work across feed, search, sitemap, and syndication."""
    if derived is None:
        derived = _get_derived_index(rows or [])
    if active_labels is None:
        if community_db is not None:
            active_labels = _read_active_labels_from_conn(community_db)
        elif db_path is not None:
            active_labels = load_active_labels(db_path)
        else:
            active_labels = {}
    if trusted_authors is None:
        if community_db is not None:
            trusted_authors = _read_trusted_authors_from_conn(community_db)
        elif db_path is not None:
            trusted_authors = load_trusted_authors(db_path)
        else:
            trusted_authors = set()
    work = derived.works.get(work_id)
    if work is None:
        raise Problem("找不到这项作品", 404)
    now_dt = _parse_now(now)
    is_withdrawn = withdrawn or (withdrawn_ids is not None and work_id in withdrawn_ids)
    raw_label = active_labels.get(work_id)
    pub_label = _public_label(raw_label)

    work_pos = derived.positions[work_id]
    author_id = work["actor_id"]
    trusted_author = author_id in trusted_authors
    if not trusted_author:
        for pos, prior_work in derived.works_by_actor.get(author_id, ()):
            prior_id = prior_work["id"]
            if pos >= work_pos or prior_id == work_id:
                continue
            if prior_id in active_labels:
                continue
            if withdrawn_ids is not None and prior_id in withdrawn_ids:
                continue
            if _direct_cooling_passed(prior_id, derived, now_dt):
                trusted_author = True
                break

    cooling_off = not (trusted_author or _direct_cooling_passed(work_id, derived, now_dt))
    feed_distribute = pub_label is None
    if pub_label is not None:
        status = "labeled"
        reason = pub_label["reason"]
        distribute = False
    elif is_withdrawn:
        status = "withdrawn"
        reason = "withdrawn"
        distribute = False
    elif cooling_off:
        status = "cooling_off"
        reason = "cooling_off"
        distribute = False
    else:
        status = "ok"
        reason = ""
        distribute = True

    return {
        "work_id": work_id,
        "distribute": distribute,
        "feed_distribute": feed_distribute,
        "status": status,
        "label": pub_label,
        "reason": reason,
        "cooling_off": cooling_off,
        "trusted_author": trusted_author,
    }


class Community:
    def __init__(self, path: str | Path, records: list[dict], profiles=None, chain_db=None):
        self.rows = records
        self.profiles = profiles or {}
        self.chain_db = chain_db
        derived = _get_derived_index(records)
        self.index = derived.index
        self.positions = derived.positions
        self.works = derived.works
        self.versions = derived.versions
        self.activity = derived.activity
        self.comments = derived.comments
        self.reports = derived.reports
        self.responses = derived.responses
        self._search_body = derived.search_body
        self.db = sqlite3.connect(str(path), timeout=5, isolation_level=None)
        tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        allowed = {"hub_details", "hub_changes", "hub_marks", "hub_reads",
                   "hub_behavior_cases", "hub_behavior_events",
                   "hub_labels", "hub_label_appeals", "hub_trusted_authors"}
        if tables - allowed:
            self.db.close()
            raise Problem("Community data needs its own database", 503)
        if tables != allowed:
            self.db.executescript("""
              CREATE TABLE IF NOT EXISTS hub_details(id TEXT PRIMARY KEY, document TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS hub_changes(id INTEGER PRIMARY KEY, exploration_id TEXT NOT NULL, document TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS hub_marks(actor TEXT NOT NULL, exploration TEXT NOT NULL, kind TEXT NOT NULL,
                  since_record INTEGER NOT NULL, PRIMARY KEY(actor,exploration,kind));
              CREATE TABLE IF NOT EXISTS hub_reads(actor TEXT NOT NULL, record TEXT NOT NULL, PRIMARY KEY(actor,record));
              CREATE TABLE IF NOT EXISTS hub_behavior_cases(
                id TEXT PRIMARY KEY, reporter TEXT NOT NULL, target TEXT NOT NULL, target_author TEXT NOT NULL,
                category TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS hub_behavior_events(
                id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES hub_behavior_cases(id),
                actor TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS hub_labels(
                id TEXT PRIMARY KEY, exploration_id TEXT NOT NULL, label TEXT NOT NULL,
                state TEXT NOT NULL, reason TEXT NOT NULL, source TEXT NOT NULL,
                operator TEXT NOT NULL, created_at TEXT NOT NULL,
                retracted_at TEXT, retracted_by TEXT, retract_reason TEXT);
              CREATE TABLE IF NOT EXISTS hub_label_appeals(
                id TEXT PRIMARY KEY, label_id TEXT NOT NULL REFERENCES hub_labels(id),
                exploration_id TEXT NOT NULL, author_id TEXT NOT NULL, body TEXT NOT NULL,
                state TEXT NOT NULL, decision TEXT, decision_reason TEXT,
                reviewer TEXT, created_at TEXT NOT NULL, reviewed_at TEXT);
              CREATE TABLE IF NOT EXISTS hub_trusted_authors(
                actor_id TEXT PRIMARY KEY, state TEXT NOT NULL, reason TEXT NOT NULL,
                operator TEXT NOT NULL, created_at TEXT NOT NULL,
                revoked_at TEXT, revoked_by TEXT, revoke_reason TEXT);
            """)
        self.db.execute("PRAGMA foreign_keys=ON")

    def close(self):
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

    def actor(self, actor_id):
        identifier(actor_id)
        if self.index.get(actor_id, {}).get("type") != "actor":
            raise Problem("请先创建或恢复身份", 401)
        return actor_id

    def public_name(self, actor_id):
        profile = self.profiles.get(actor_id)
        return profile['display_name'] if profile else self.index[actor_id]['payload']['alias']

    def submit_behavior(self, actor_id, target_id, category, reason, research=None):
        self.actor(actor_id)
        identifier(target_id)
        target = self.index.get(target_id)
        if target is not None and target["type"] in {"revision", "comment", "evidence"}:
            target_author = target["actor_id"]
        elif target is None and research is not None:
            target_author = None
            for table in ("research_events", "research_files"):
                row = research.db.execute(f"SELECT actor_id FROM {table} WHERE id=?", (target_id,)).fetchone()
                if row is not None:
                    target_author = row[0]
                    break
            if target_author is None:
                raise Problem("请指向具体版本、评论、分析、协作事件或附件", 404)
            self.actor(target_author)
        else:
            raise Problem("请指向具体版本、评论、分析、协作事件或附件", 404)
        if not isinstance(category, str) or category not in BEHAVIOR_CATEGORIES:
            raise Problem("请选择行为问题类型")
        text(reason, 2000)
        with self.transaction():
            prior = self.db.execute("SELECT id FROM hub_behavior_cases WHERE reporter=? AND target=?",
                                    (actor_id, target_id)).fetchone()
            if prior:
                return self.behavior_case(actor_id, prior[0])
            count = self.db.execute("SELECT COUNT(*) FROM hub_behavior_cases WHERE reporter=?",
                                    (actor_id,)).fetchone()[0]
            if count >= MAX_BEHAVIOR_CASES_PER_ACTOR:
                raise Problem("当前身份的私下反馈名额已满，请联系实例维护者；发表和讨论不受影响", 413)
            case_id = str(uuid.uuid4())
            self.db.execute("INSERT INTO hub_behavior_cases VALUES(?,?,?,?,?,?,?)",
                            (case_id, actor_id, target_id, target_author, category, reason.strip(), utc_now()))
        return self.behavior_case(actor_id, case_id)

    def behavior_case(self, actor_id, case_id):
        self.actor(actor_id)
        identifier(case_id)
        row = self.db.execute("SELECT * FROM hub_behavior_cases WHERE id=?", (case_id,)).fetchone()
        if row is None:
            raise Problem("找不到反馈记录", 404)
        fields = ("id", "reporter", "target", "target_author", "category", "reason", "created_at")
        case = dict(zip(fields, row))
        if actor_id not in {case["reporter"], case["target_author"]}:
            raise Problem("这不是你可查看的反馈记录", 403)
        events = [dict(zip(("id", "case_id", "actor", "kind", "body", "created_at"), e)) for e in
                  self.db.execute("SELECT * FROM hub_behavior_events WHERE case_id=? ORDER BY rowid", (case_id,))]
        # A respondent should learn the concern and decision, but not the reporter's identity.
        if actor_id != case["reporter"]:
            for event in events:
                if event["actor"] == case["reporter"]:
                    event["actor"] = "reporter"
            case.pop("reporter")
        case["events"] = events
        appeals = sum(e["kind"].startswith("appeal_") and not e["kind"].startswith("appeal_review_") for e in events)
        decisions = sum(e["kind"].startswith("appeal_review_") for e in events)
        case["state"] = "appeal_pending" if appeals > decisions else "reviewed" if any(
            e["kind"].startswith("review_") for e in events) else "pending"
        return case

    def behavior_cases(self, actor_id):
        self.actor(actor_id)
        ids = [row[0] for row in self.db.execute(
            "SELECT id FROM hub_behavior_cases WHERE reporter=? OR target_author=? ORDER BY rowid DESC LIMIT 100",
            (actor_id, actor_id))]
        return {"items": [self.behavior_case(actor_id, case_id) for case_id in ids]}

    def respond_behavior(self, actor_id, case_id, body):
        case = self.behavior_case(actor_id, case_id)
        if actor_id != case["target_author"]:
            raise Problem("只有被指向记录的作者可以回应", 403)
        text(body, 2000)
        with self.transaction():
            count = self.db.execute("SELECT COUNT(*) FROM hub_behavior_events WHERE case_id=? AND kind='response'", (case_id,)).fetchone()[0]
            if count >= 3:
                raise Problem("回应次数已满，请联系实例维护者", 413)
            self.db.execute("INSERT INTO hub_behavior_events VALUES(?,?,?,?,?,?)",
                            (str(uuid.uuid4()), case_id, actor_id, "response", body.strip(), utc_now()))
        return self.behavior_case(actor_id, case_id)

    def appeal_behavior(self, actor_id, case_id, body):
        case = self.behavior_case(actor_id, case_id)
        role = "reporter" if case.get("reporter") == actor_id else "author"
        text(body, 2000)
        with self.transaction():
            events = [row[0] for row in self.db.execute(
                "SELECT kind FROM hub_behavior_events WHERE case_id=? ORDER BY rowid", (case_id,))]
            if not any(kind in {"review_no_action", "review_follow_up"} for kind in events):
                raise Problem("请等待首次本地复核后再申请复议", 409)
            if "appeal_" + role in events:
                raise Problem("本方已就此案件提交过复议", 409)
            if sum(kind in {"appeal_reporter", "appeal_author"} for kind in events) > sum(
                    kind.startswith("appeal_review_") for kind in events):
                raise Problem("本案另有待决复议，请待记录完成后再提交", 409)
            self.db.execute("INSERT INTO hub_behavior_events VALUES(?,?,?,?,?,?)",
                            (str(uuid.uuid4()), case_id, actor_id, "appeal_" + role, body.strip(), utc_now()))
        return self.behavior_case(actor_id, case_id)

    def review_behavior(self, case_id, outcome, reason, operator_id="local_operator"):
        """Offline operator only. No HTTP route or content restriction follows."""
        identifier(case_id)
        if not isinstance(outcome, str) or outcome not in BEHAVIOR_OUTCOMES:
            raise Problem("未知处理结果")
        text(reason, 2000)
        operator_label(operator_id)
        row = self.db.execute("SELECT id FROM hub_behavior_cases WHERE id=?", (case_id,)).fetchone()
        if not row:
            raise Problem("找不到反馈记录", 404)
        with self.transaction():
            if self.db.execute("SELECT 1 FROM hub_behavior_events WHERE case_id=? AND kind IN ('appeal_reporter','appeal_author') LIMIT 1",
                               (case_id,)).fetchone():
                raise Problem("已有复议记录，请通过复议流程继续核对", 409)
            self.db.execute("INSERT INTO hub_behavior_events VALUES(?,?,?,?,?,?)",
                            (str(uuid.uuid4()), case_id, operator_id, "review_" + outcome, reason.strip(), utc_now()))
        return {"case_id": case_id, "outcome": outcome, "recorded": True}

    def review_behavior_appeal(self, case_id, outcome, reason, operator_id):
        """Offline append-only appeal review; operator IDs are claims, not authentication."""
        identifier(case_id)
        if not isinstance(outcome, str) or outcome not in BEHAVIOR_APPEAL_OUTCOMES:
            raise Problem("未知复议结果")
        text(reason, 2000)
        operator_label(operator_id)
        with self.transaction():
            events = list(self.db.execute(
                "SELECT actor,kind FROM hub_behavior_events WHERE case_id=? ORDER BY rowid", (case_id,)))
            if not events:
                raise Problem("找不到反馈记录", 404)
            initial = [actor for actor, kind in events if kind in {"review_no_action", "review_follow_up"}]
            if not initial:
                raise Problem("本案尚无首次复核", 409)
            if operator_id == initial[-1]:
                raise Problem("复议复核不能使用首次复核的操作者标识", 403)
            appeals = sum(kind in {"appeal_reporter", "appeal_author"} for _, kind in events)
            decisions = sum(kind.startswith("appeal_review_") for _, kind in events)
            if appeals != decisions + 1:
                raise Problem("没有唯一待决复议", 409)
            self.db.execute("INSERT INTO hub_behavior_events VALUES(?,?,?,?,?,?)",
                            (str(uuid.uuid4()), case_id, operator_id,
                             "appeal_review_" + outcome, reason.strip(), utc_now()))
        return {"case_id": case_id, "outcome": outcome, "recorded": True,
                "reviewer_id": operator_id, "independence_verified": False}

    def work(self, work_id):
        identifier(work_id)
        if work_id not in self.works:
            raise Problem("找不到这项作品", 404)
        return self.works[work_id]

    def active_labels(self) -> dict[str, dict]:
        return _read_active_labels_from_conn(self.db)

    def trusted_author_ids(self) -> set[str]:
        return _read_trusted_authors_from_conn(self.db)

    def trust_author(self, actor_id: str, reason: str, operator_id: str) -> dict:
        """Offline maintainer setting to exempt an existing author from the new-author cooling-off period."""
        identifier(actor_id)
        if self.index.get(actor_id, {}).get("type") != "actor":
            raise Problem("找不到这位作者", 404)
        text(reason, 2000)
        operator_label(operator_id)
        now = utc_now()
        with self.transaction():
            self.db.execute(
                "INSERT INTO hub_trusted_authors VALUES(?,?,?,?,?,NULL,NULL,NULL) "
                "ON CONFLICT(actor_id) DO UPDATE SET state='active', reason=excluded.reason, "
                "operator=excluded.operator, created_at=excluded.created_at, "
                "revoked_at=NULL, revoked_by=NULL, revoke_reason=NULL",
                (actor_id, "active", reason.strip(), operator_id, now),
            )
        return {
            "actor_id": actor_id,
            "state": "active",
            "reason": reason.strip(),
            "operator": operator_id,
            "created_at": now,
        }

    def untrust_author(self, actor_id: str, reason: str, operator_id: str) -> dict:
        """Offline maintainer revocation of manual author trust."""
        identifier(actor_id)
        if self.index.get(actor_id, {}).get("type") != "actor":
            raise Problem("找不到这位作者", 404)
        text(reason, 2000)
        operator_label(operator_id)
        now = utc_now()
        with self.transaction():
            row = self.db.execute(
                "SELECT actor_id,state,reason,operator,created_at,revoked_at,revoked_by,revoke_reason "
                "FROM hub_trusted_authors WHERE actor_id=?",
                (actor_id,),
            ).fetchone()
            if row is None or row[1] != "active":
                raise Problem("该作者当前不在信任名单中", 404)
            self.db.execute(
                "UPDATE hub_trusted_authors SET state='revoked', revoked_at=?, revoked_by=?, revoke_reason=? "
                "WHERE actor_id=?",
                (now, operator_id, reason.strip(), actor_id),
            )
        return {
            "actor_id": actor_id,
            "state": "revoked",
            "revoked_by": operator_id,
            "reason": reason.strip(),
            "revoked_at": now,
        }

    def list_trusted(self) -> dict:
        """Offline maintainer listing of currently trusted authors."""
        items = []
        for row in self.db.execute(
            "SELECT actor_id,state,reason,operator,created_at FROM hub_trusted_authors "
            "WHERE state='active' ORDER BY rowid"
        ):
            aid, state, reason, operator, created_at = row
            alias = self.public_name(aid) if aid in self.index else ""
            items.append({
                "actor_id": aid,
                "display_name": alias,
                "state": state,
                "reason": reason,
                "operator": operator,
                "created_at": created_at,
            })
        return {"trusted_authors": items}

    def active_label(self, exploration_id: str) -> dict | None:
        self.work(exploration_id)
        row = self.db.execute(
            "SELECT id,exploration_id,label,state,reason,source,operator,created_at,"
            "retracted_at,retracted_by,retract_reason FROM hub_labels "
            "WHERE exploration_id=? AND state='active' ORDER BY rowid DESC LIMIT 1",
            (exploration_id,),
        ).fetchone()
        return _public_label(dict(zip(_LABEL_FIELDS, row))) if row else None

    def distribution(
        self,
        work_id: str,
        *,
        active_labels: dict[str, dict] | None = None,
        trusted_authors: set[str] | None = None,
        withdrawn: bool = False,
        withdrawn_ids: set[str] | None = None,
        now: str | datetime | None = None,
    ) -> dict:
        self.work(work_id)
        return distribution(
            work_id,
            derived=_get_derived_index(self.rows),
            community_db=self.db,
            active_labels=active_labels,
            trusted_authors=trusted_authors,
            withdrawn=withdrawn,
            withdrawn_ids=withdrawn_ids,
            now=now,
        )

    def label_work(self, exploration_id: str, label: str, reason: str, operator_id: str, *, source: str = "maintainer") -> dict:
        """Offline maintainer or AI triage label. Does not alter records or mirrors."""
        self.work(exploration_id)
        if not isinstance(label, str) or label not in LABEL_KINDS:
            raise Problem("未知的标签类型")
        if not isinstance(source, str) or source not in LABEL_SOURCES:
            raise Problem("未知的标签来源")
        text(reason, 2000)
        operator_label(operator_id)
        now = utc_now()
        label_id = str(uuid.uuid4())
        with self.transaction():
            self.db.execute(
                "UPDATE hub_labels SET state='retracted', retracted_at=?, retracted_by=?, retract_reason=? "
                "WHERE exploration_id=? AND state='active'",
                (now, operator_id, "superseded", exploration_id),
            )
            self.db.execute(
                "INSERT INTO hub_labels VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (label_id, exploration_id, label, "active", reason.strip(), source, operator_id, now, None, None, None),
            )
        return {
            "id": label_id,
            "exploration_id": exploration_id,
            "label": label,
            "state": "active",
            "reason": reason.strip(),
            "source": source,
            "operator": operator_id,
            "created_at": now,
        }

    def retract_label(self, label_id: str, reason: str, operator_id: str) -> dict:
        """Offline maintainer retraction of an active label."""
        identifier(label_id)
        text(reason, 2000)
        operator_label(operator_id)
        now = utc_now()
        with self.transaction():
            row = self.db.execute(
                "SELECT id,exploration_id,label,state,reason,source,operator,created_at,"
                "retracted_at,retracted_by,retract_reason FROM hub_labels WHERE id=?",
                (label_id,),
            ).fetchone()
            if row is None:
                raise Problem("找不到标签记录", 404)
            lbl = dict(zip(_LABEL_FIELDS, row))
            if lbl["state"] != "active":
                raise Problem("该标签已撤回", 409)
            self.db.execute(
                "UPDATE hub_labels SET state='retracted', retracted_at=?, retracted_by=?, retract_reason=? WHERE id=?",
                (now, operator_id, reason.strip(), label_id),
            )
            self.db.execute(
                "UPDATE hub_label_appeals SET state='reviewed', decision='retract', decision_reason=?, "
                "reviewer=?, reviewed_at=? WHERE label_id=? AND state='pending'",
                (reason.strip(), operator_id, now, label_id),
            )
        return {
            "label_id": label_id,
            "exploration_id": lbl["exploration_id"],
            "label": lbl["label"],
            "state": "retracted",
            "retracted_by": operator_id,
            "reason": reason.strip(),
            "retracted_at": now,
        }

    def appeal_label(self, actor_id: str, label_id: str | None = None, body: str = "", *, exploration_id: str | None = None) -> dict:
        """Author submits a private appeal (at most once per label, <=2000 chars)."""
        self.actor(actor_id)
        text(body, 2000)
        with self.transaction():
            if label_id:
                identifier(label_id)
                row = self.db.execute(
                    "SELECT id,exploration_id,label,state,reason,source,operator,created_at,"
                    "retracted_at,retracted_by,retract_reason FROM hub_labels WHERE id=?",
                    (label_id,),
                ).fetchone()
            elif exploration_id:
                self.work(exploration_id)
                row = self.db.execute(
                    "SELECT id,exploration_id,label,state,reason,source,operator,created_at,"
                    "retracted_at,retracted_by,retract_reason FROM hub_labels "
                    "WHERE exploration_id=? AND state='active' ORDER BY rowid DESC LIMIT 1",
                    (exploration_id,),
                ).fetchone()
            else:
                row = None
            if row is None:
                raise Problem("找不到标签记录", 404)
            lbl = dict(zip(_LABEL_FIELDS, row))
            work = self.work(lbl["exploration_id"])
            if work["actor_id"] != actor_id:
                raise Problem("只有作品作者可以提交申诉", 403)
            if lbl["state"] != "active":
                raise Problem("该标签已撤回，无需申诉", 409)
            existing = self.db.execute(
                "SELECT id FROM hub_label_appeals WHERE label_id=?",
                (lbl["id"],),
            ).fetchone()
            if existing is not None:
                raise Problem("已就该标签提交过申诉", 409)
            appeal_id = str(uuid.uuid4())
            now = utc_now()
            self.db.execute(
                "INSERT INTO hub_label_appeals VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (appeal_id, lbl["id"], lbl["exploration_id"], actor_id, body.strip(),
                 "pending", None, None, None, now, None),
            )
        return {
            "id": appeal_id,
            "label_id": lbl["id"],
            "exploration_id": lbl["exploration_id"],
            "author_id": actor_id,
            "label": lbl["label"],
            "body": body.strip(),
            "state": "pending",
            "decision": None,
            "decision_reason": None,
            "created_at": now,
            "reviewed_at": None,
        }

    def author_label_status(self, actor_id: str, exploration_id: str) -> dict:
        self.actor(actor_id)
        work = self.work(exploration_id)
        if work["actor_id"] != actor_id:
            raise Problem("只有作品作者可以查看申诉记录", 403)
        status = _read_author_label_status_from_conn(self.db, exploration_id, actor_id)
        return status or {"active_label": None, "can_appeal": False, "appeal": None}

    def review_label_appeal(self, appeal_id: str, decision: str, reason: str, operator_id: str) -> dict:
        """Offline maintainer review of a label appeal. Rejects same operator as original labeler."""
        identifier(appeal_id)
        if not isinstance(decision, str) or decision not in LABEL_APPEAL_DECISIONS:
            raise Problem("未知申诉处理结果")
        text(reason, 2000)
        operator_label(operator_id)
        now = utc_now()
        with self.transaction():
            appeal_row = self.db.execute(
                "SELECT id,label_id,exploration_id,author_id,body,state,decision,decision_reason,"
                "reviewer,created_at,reviewed_at FROM hub_label_appeals WHERE id=?",
                (appeal_id,),
            ).fetchone()
            if appeal_row is None:
                raise Problem("找不到申诉记录", 404)
            appeal = dict(zip(_APPEAL_FIELDS, appeal_row))
            if appeal["state"] != "pending":
                raise Problem("该申诉已处理", 409)
            lbl_row = self.db.execute(
                "SELECT id,exploration_id,label,state,reason,source,operator,created_at,"
                "retracted_at,retracted_by,retract_reason FROM hub_labels WHERE id=?",
                (appeal["label_id"],),
            ).fetchone()
            if lbl_row is None:
                raise Problem("找不到标签记录", 404)
            lbl = dict(zip(_LABEL_FIELDS, lbl_row))
            if operator_id == lbl["operator"]:
                raise Problem("申诉复核不能使用原标注的操作者标识", 403)
            self.db.execute(
                "UPDATE hub_label_appeals SET state='reviewed', decision=?, decision_reason=?, "
                "reviewer=?, reviewed_at=? WHERE id=?",
                (decision, reason.strip(), operator_id, now, appeal_id),
            )
            if decision == "retract":
                self.db.execute(
                    "UPDATE hub_labels SET state='retracted', retracted_at=?, retracted_by=?, retract_reason=? WHERE id=?",
                    (now, operator_id, reason.strip(), lbl["id"]),
                )
        return {
            "appeal_id": appeal_id,
            "label_id": lbl["id"],
            "exploration_id": lbl["exploration_id"],
            "decision": decision,
            "state": "retracted" if decision == "retract" else lbl["state"],
            "reviewer_id": operator_id,
            "recorded": True,
        }

    def label_queue(self) -> dict:
        """Offline maintainer queue listing active pending_review labels and pending appeals."""
        metadata = {r[0]: json.loads(r[1]) for r in self.db.execute("SELECT id,document FROM hub_details")}
        pending_review = []
        for row in self.db.execute(
            "SELECT id,exploration_id,label,state,reason,source,operator,created_at,"
            "retracted_at,retracted_by,retract_reason FROM hub_labels "
            "WHERE state='active' AND label='pending_review' ORDER BY rowid"
        ):
            lbl = dict(zip(_LABEL_FIELDS, row))
            wid = lbl["exploration_id"]
            title = self.versions[wid][-1]["payload"]["title"] if wid in self.works and self.versions[wid] else ""
            author_id = self.works[wid]["actor_id"] if wid in self.works else ""
            triage = metadata.get(wid, {}).get("promotion_triage")
            item = {
                "id": lbl["id"],
                "exploration_id": wid,
                "author_id": author_id,
                "title": title,
                "label": lbl["label"],
                "state": lbl["state"],
                "reason": lbl["reason"],
                "source": lbl["source"],
                "operator": lbl["operator"],
                "created_at": lbl["created_at"],
            }
            if isinstance(triage, dict):
                item["confidence"] = triage.get("confidence")
                item["reasons"] = list(triage.get("reasons", []))
                item["evidence"] = list(triage.get("evidence", []))
                item["triage_revision"] = triage.get("revision_id")
                item["promotion_triage"] = triage
            pending_review.append(item)
        pending_appeals = []
        for row in self.db.execute(
            "SELECT a.id,a.label_id,a.exploration_id,a.author_id,a.body,a.state,a.created_at,"
            "l.label,l.reason,l.operator,l.source "
            "FROM hub_label_appeals a JOIN hub_labels l ON a.label_id=l.id "
            "WHERE a.state='pending' ORDER BY a.rowid"
        ):
            aid, lid, wid, author_id, body, state, created_at, label, lreason, loperator, lsource = row
            title = self.versions[wid][-1]["payload"]["title"] if wid in self.works and self.versions[wid] else ""
            pending_appeals.append({
                "id": aid,
                "label_id": lid,
                "exploration_id": wid,
                "author_id": author_id,
                "title": title,
                "label": label,
                "label_reason": lreason,
                "label_operator": loperator,
                "label_source": lsource,
                "body": body,
                "state": state,
                "created_at": created_at,
            })
        return {
            "pending_review": pending_review,
            "pending_appeals": pending_appeals,
            "trusted_authors": self.list_trusted()["trusted_authors"],
        }

    def _raw_details(self, exploration_id):
        self.work(exploration_id)
        row = self.db.execute("SELECT document FROM hub_details WHERE id=?", (exploration_id,)).fetchone()
        return json.loads(row[0]) if row else {"topics": [], "help": "", "version": 0}

    def details(self, exploration_id, actor_id=None):
        doc = self._raw_details(exploration_id)
        res = {"topics": doc["topics"], "help": doc["help"], "version": doc["version"]}
        lbl = self.active_label(exploration_id)
        if lbl is not None:
            res["label"] = lbl
        if actor_id is not None and self.works[exploration_id]["actor_id"] == actor_id:
            status = _read_author_label_status_from_conn(self.db, exploration_id, actor_id)
            if status is not None:
                res["label_appeal"] = status
        return res

    def set_details(self, actor_id, exploration_id, topics, help, expected_version):
        self.actor(actor_id)
        if self.work(exploration_id)["actor_id"] != actor_id:
            raise Problem("可以评论和独立检查；只有作者可以修改这项作品的介绍", 403)
        normalized = topics_list(topics)
        text(help, 1000, True)
        if type(expected_version) is not int or expected_version < 0:
            raise Problem("需要有效的介绍版本")
        with self.transaction():
            previous = self._raw_details(exploration_id)
            if previous["version"] != expected_version:
                raise Problem("介绍已更新，请刷新后再保存", 409)
            public_value = {"topics": normalized, "help": help.strip(), "version": expected_version + 1}
            stored = dict(public_value)
            if previous.get("keywords"):
                stored["keywords"] = previous["keywords"]
            if previous.get("catalog_revision"):
                stored["catalog_revision"] = previous["catalog_revision"]
            if previous.get("promotion_triage"):
                stored["promotion_triage"] = previous["promotion_triage"]
            self.db.execute("INSERT INTO hub_details VALUES(?,?) ON CONFLICT(id) DO UPDATE SET document=excluded.document",
                            (exploration_id, canonical(stored)))
            self.db.execute("INSERT INTO hub_changes(exploration_id,document) VALUES(?,?)",
                            (exploration_id, canonical({"actor_id": actor_id, **public_value})))
        return public_value

    def uncataloged_revisions(self) -> list[dict]:
        """Latest revisions of works not yet indexed by AI cataloging."""
        metadata = {r[0]: json.loads(r[1]) for r in self.db.execute("SELECT id,document FROM hub_details")}
        pending = []
        for work_id in self.works:
            latest = self.versions[work_id][-1]
            if metadata.get(work_id, {}).get("catalog_revision") != latest["id"]:
                pending.append(latest)
        return pending

    def auto_catalog(
        self,
        exploration_id: str,
        revision_id: str,
        topics: list[str],
        keywords: list[str],
        *,
        promotion: dict | None = None,
        auto_label: bool = True,
        promotion_threshold: float = PROMOTION_CONFIDENCE_THRESHOLD,
        operator_id: str = PROMOTION_OPERATOR,
    ) -> dict:
        """Store AI-extracted topics, bilingual search terms, and optional promotion triage."""
        self.work(exploration_id)
        identifier(revision_id)
        if not any(r["id"] == revision_id for r in self.versions[exploration_id]):
            raise Problem("找不到对应的版本", 404)
        operator_label(operator_id)
        clean_topics = []
        for item in (topics or []):
            if isinstance(item, str):
                norm = " ".join(unicodedata.normalize("NFKC", item).casefold().split())
                if 1 <= len(norm) <= 32 and norm not in clean_topics:
                    clean_topics.append(norm)
            if len(clean_topics) >= 6:
                break
        clean_topics.sort()
        clean_keywords = []
        for item in (keywords or []):
            if isinstance(item, str):
                norm = " ".join(unicodedata.normalize("NFKC", item).casefold().split())
                if 1 <= len(norm) <= 64 and norm not in clean_keywords:
                    clean_keywords.append(norm)
            if len(clean_keywords) >= 16:
                break
        norm_promo = _normalize_promotion_triage(promotion)
        with self.transaction():
            previous = self._raw_details(exploration_id)
            prev_triage = previous.get("promotion_triage")
            already_triaged_same_rev = (
                isinstance(prev_triage, dict) and prev_triage.get("revision_id") == revision_id
            )
            use_ai_topics = (not previous["topics"]) or bool(previous.get("ai_topics"))
            stored = {
                "topics": clean_topics if use_ai_topics else previous["topics"],
                "help": previous["help"],
                "version": previous["version"],
                "keywords": clean_keywords,
                "catalog_revision": revision_id,
            }
            if use_ai_topics and clean_topics:
                stored["ai_topics"] = True
            if norm_promo is not None:
                if already_triaged_same_rev:
                    stored["promotion_triage"] = prev_triage
                else:
                    stored["promotion_triage"] = {
                        "revision_id": revision_id,
                        **norm_promo,
                    }
            elif already_triaged_same_rev:
                stored["promotion_triage"] = prev_triage
            self.db.execute("INSERT INTO hub_details VALUES(?,?) ON CONFLICT(id) DO UPDATE SET document=excluded.document",
                            (exploration_id, canonical(stored)))

            if norm_promo is not None and auto_label and not already_triaged_same_rev:
                active_row = self.db.execute(
                    "SELECT id,exploration_id,label,state,reason,source,operator,created_at,"
                    "retracted_at,retracted_by,retract_reason FROM hub_labels "
                    "WHERE exploration_id=? AND state='active' ORDER BY rowid DESC LIMIT 1",
                    (exploration_id,),
                ).fetchone()
                active_lbl = dict(zip(_LABEL_FIELDS, active_row)) if active_row else None
                flagged = norm_promo["likely"] and norm_promo["confidence"] >= float(promotion_threshold)
                now = utc_now()
                if flagged:
                    if active_lbl is None:
                        short_reasons = [r[:80] for r in norm_promo["reasons"][:2] if r]
                        reason_text = "；".join(short_reasons)[:200] if short_reasons else DEFAULT_AI_PROMOTION_REASON
                        label_id = str(uuid.uuid4())
                        self.db.execute(
                            "INSERT INTO hub_labels VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (label_id, exploration_id, "pending_review", "active",
                             reason_text, "ai", operator_id, now, None, None, None),
                        )
                elif not norm_promo["likely"]:
                    # Revised work judged not to be promotion: retract ONLY AI pending_review, never maintainer labels.
                    if (active_lbl is not None
                            and active_lbl["source"] == "ai"
                            and active_lbl["label"] == "pending_review"):
                        retract_note = "新版本 AI 编目分诊未发现推广倾向，自动撤回初筛待复核标签"
                        self.db.execute(
                            "UPDATE hub_labels SET state='retracted', retracted_at=?, retracted_by=?, retract_reason=? "
                            "WHERE id=? AND source='ai' AND state='active'",
                            (now, operator_id, retract_note, active_lbl["id"]),
                        )
                        self.db.execute(
                            "UPDATE hub_label_appeals SET state='reviewed', decision='retract', decision_reason=?, "
                            "reviewer=?, reviewed_at=? WHERE label_id=? AND state='pending'",
                            (retract_note, operator_id, now, active_lbl["id"]),
                        )
        return stored

    def mark(self, actor_id, exploration_id, kind, enabled):
        self.actor(actor_id); self.work(exploration_id)
        if kind not in {"follow", "bookmark"} or type(enabled) is not bool:
            raise Problem("需要明确的关注或收藏操作")
        with self.transaction():
            if enabled:
                existing = self.db.execute("SELECT 1 FROM hub_marks WHERE actor=? AND exploration=? AND kind=?",
                                           (actor_id, exploration_id, kind)).fetchone()
                count = self.db.execute("SELECT COUNT(*) FROM hub_marks WHERE actor=? AND kind=?", (actor_id, kind)).fetchone()[0]
                if not existing and count >= MAX_PREFERENCES:
                    raise Problem("本地个人列表已满；发表和讨论不受影响", 413)
                self.db.execute("INSERT OR IGNORE INTO hub_marks VALUES(?,?,?,?)",
                                (actor_id, exploration_id, kind, len(self.rows)))
            else:
                self.db.execute("DELETE FROM hub_marks WHERE actor=? AND exploration=? AND kind=?", (actor_id, exploration_id, kind))
        return {"exploration_id": exploration_id, "kind": kind, "enabled": enabled}

    def _marks(self, actor_id, kind):
        return {row[0]: row[1] for row in self.db.execute(
            "SELECT exploration,since_record FROM hub_marks WHERE actor=? AND kind=?", (actor_id, kind))}

    def feed(self, actor_id=None, *, q="", topic="", view="all", order="latest", offset=0, limit=20, analysis=None):
        if actor_id is not None:
            self.actor(actor_id)
        text(q, 200, True); text(topic, 80, True)
        if view not in VIEWS or order not in {"latest", "active"}:
            raise Problem("未知的浏览方式")
        if type(offset) is not int or not 0 <= offset <= 10000 or type(limit) is not int or not 1 <= limit <= 50:
            raise Problem("分页参数不正确")
        if view in {"following", "bookmarks", "mine"} and actor_id is None:
            raise Problem("请先创建或恢复身份", 401)
        follow = self._marks(actor_id, "follow") if actor_id else {}
        saved = self._marks(actor_id, "bookmark") if actor_id else {}
        active_labels = self.active_labels()
        trusted_authors = self.trusted_author_ids()
        terms = unicodedata.normalize("NFKC", q).casefold().split()
        selected_topic = topic_name(topic) if topic.strip() else ""
        default_meta = {"topics": [], "help": "", "version": 0}
        need_all_meta = (view == "collaborate") or bool(selected_topic) or bool(terms)
        metadata = {r[0]: json.loads(r[1]) for r in self.db.execute("SELECT id,document FROM hub_details")} if need_all_meta else {}
        search_body = self._search_body
        matched_ids = []
        for work_id, work in self.works.items():
            dist = self.distribution(work_id, active_labels=active_labels, trusted_authors=trusted_authors)
            if view in {"all", "unanswered", "collaborate"} and not dist["feed_distribute"]:
                continue
            if view == "labeled" and dist["feed_distribute"]:
                continue
            if view == "unanswered" and self.responses[work_id]:
                continue
            if (view == "following" and work_id not in follow) or (view == "bookmarks" and work_id not in saved):
                continue
            if view == "mine" and work["actor_id"] != actor_id:
                continue
            if need_all_meta:
                meta = metadata.get(work_id, default_meta)
                if view == "collaborate" and not meta["help"]:
                    continue
                if selected_topic and selected_topic not in meta["topics"]:
                    continue
                if terms:
                    latest = self.versions[work_id][-1]
                    rev_id = latest["id"]
                    body_norm = search_body.get(rev_id)
                    if body_norm is None:
                        paper_abs = (latest["payload"].get("paper") or {}).get("abstract") or ""
                        body_norm = unicodedata.normalize(
                            "NFKC", latest["payload"]["title"] + " " + paper_abs + " " + latest["payload"]["body"]
                        ).casefold()
                        search_body[rev_id] = body_norm
                    meta_norm = None
                    matched = True
                    for t in terms:
                        if t in body_norm:
                            continue
                        if meta_norm is None:
                            meta_norm = unicodedata.normalize(
                                "NFKC", " ".join([meta["help"], *meta["topics"], *meta.get("keywords", [])])
                            ).casefold()
                        if t not in meta_norm:
                            matched = False
                            break
                    if not matched:
                        continue
            matched_ids.append(work_id)
        if order == "latest":
            matched_ids.sort(key=lambda wid: (self.positions[wid], wid), reverse=True)
        else:
            matched_ids.sort(key=lambda wid: (self.activity[wid], self.positions[wid], wid), reverse=True)
        total = len(matched_ids)
        page_ids = matched_ids[offset:offset + limit]
        if page_ids and not need_all_meta:
            placeholders = ",".join("?" for _ in page_ids)
            metadata = {
                r[0]: json.loads(r[1])
                for r in self.db.execute(f"SELECT id,document FROM hub_details WHERE id IN ({placeholders})", page_ids)
            }
        ai_by_rev = {}
        if page_ids and analysis is not None:
            rev_ids = [v["id"] for wid in page_ids for v in self.versions[wid]]
            if rev_ids:
                placeholders = ",".join("?" for _ in rev_ids)
                ai_by_rev = dict(analysis.db.execute(
                    f"SELECT revision_id, COUNT(*) FROM analysis_reports WHERE revision_id IN ({placeholders}) GROUP BY revision_id",
                    rev_ids,
                ))
        if self.chain_db is not None and page_ids:
            from . import record_chain
        result = []
        for work_id in page_ids:
            work = self.works[work_id]
            versions = self.versions[work_id]
            latest = versions[-1]
            meta = metadata.get(work_id, default_meta)
            dist = self.distribution(work_id, active_labels=active_labels, trusted_authors=trusted_authors)
            pub_lbl = dist["label"]
            paper = latest["payload"].get("paper") or {}
            item = {"id": work_id, "revision_id": latest["id"], "author_id": work["actor_id"],
                    "author": self.public_name(work["actor_id"]),
                    "affiliation": self.profiles.get(work["actor_id"], {}).get('affiliation', ''),
                    "kind": work["payload"]["kind"],
                    "title": latest["payload"]["title"],
                    "excerpt": excerpt(paper.get("abstract") or latest["payload"]["body"], 220),
                    "created_at": work["created_at"], "updated_at": self.rows[self.activity[work_id]]["created_at"],
                    "versions": len(versions), "comments": self.comments[work_id], "reports": self.reports[work_id],
                    "ai_reports": 0 if pub_lbl else sum(ai_by_rev.get(v["id"], 0) for v in versions),
                    "topics": meta["topics"], "help": meta["help"], "details_version": meta["version"],
                    "following": work_id in follow, "bookmarked": work_id in saved}
            if pub_lbl is not None:
                item["label"] = pub_lbl
                item["moderation_label"] = pub_lbl["label"]
                item["moderation_reason"] = pub_lbl["reason"]
            if self.chain_db is not None:
                if pub_lbl is not None:
                    item["chain_seq"] = None
                    item["bitcoin_height"] = None
                else:
                    chain = record_chain.summary(self.chain_db, latest["id"])
                    item["chain_seq"] = chain["seq"] if chain else None
                    anchor = chain.get("anchor") if chain else None
                    item["bitcoin_height"] = anchor["bitcoin"]["height"] if (anchor and anchor.get("state") == "confirmed" and anchor.get("bitcoin")) else None
            result.append(item)
        return {"items": result, "total": total, "offset": offset, "limit": limit,
                "next_offset": offset + limit if offset + limit < total else None}

    def topics(self):
        counts = Counter()
        active_labels = self.active_labels()
        trusted_authors = self.trusted_author_ids()
        for work_id, raw in self.db.execute("SELECT id,document FROM hub_details"):
            if work_id in self.works and self.distribution(work_id, active_labels=active_labels, trusted_authors=trusted_authors)["feed_distribute"]:
                counts.update(json.loads(raw)["topics"])
        return {"items": [{"name": name, "explorations": counts[name]} for name in sorted(counts)]}

    def notifications(self, actor_id, *, limit=50, research=None, analysis=None):
        self.actor(actor_id)
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise Problem("提醒数量不正确")
        following = self._marks(actor_id, "follow")
        read = {r[0] for r in self.db.execute("SELECT record FROM hub_reads WHERE actor=?", (actor_id,))}
        items = []
        for n, row in enumerate(self.rows):
            kind = row["type"]
            if kind not in {"revision", "comment", "evidence"} or row["actor_id"] == actor_id:
                continue
            revision = row if kind == "revision" else self.index[row["payload"]["revision_id"]]
            work_id = revision["payload"]["exploration_id"]
            target = self.index.get(row["payload"].get("reply_to"))
            reason = None
            if kind in {"comment", "evidence"} and self.works[work_id]["actor_id"] == actor_id:
                reason = "your_exploration"
            elif target and target["actor_id"] == actor_id:
                reason = "reply_to_you"
            elif work_id in following and n >= following[work_id]:
                reason = "following"
            if reason:
                row_paper = row["payload"].get("paper") or {}
                raw_text = row_paper.get("abstract") or row["payload"].get("body", row["payload"].get("result", ""))
                items.append({"id": row["id"], "exploration_id": work_id, "revision_id": revision["id"],
                              "type": kind, "reason": reason, "from": self.public_name(row["actor_id"]),
                              "title": revision["payload"]["title"], "created_at": row["created_at"],
                              "excerpt": excerpt(raw_text, 160),
                              "read": row["id"] in read})
        items.reverse()
        extra = []
        if research is not None:
            extra += research.collaboration_notifications(actor_id)
        if analysis is not None:
            extra += analysis.notices(actor_id)
        if extra:
            items.extend({**notice, 'read': notice['id'] in read} for notice in extra)
            items.sort(key=lambda notice: notice['created_at'], reverse=True)
        return {"items": items[:limit], "unread": sum(not x["read"] for x in items), "total": len(items)}

    def mark_read(self, actor_id, record_ids, *, research=None, analysis=None):
        self.actor(actor_id)
        if type(record_ids) is not list or len(record_ids) > 100:
            raise Problem("一次最多标记100条提醒")
        available = {x["id"] for x in self.notifications(actor_id, limit=10000, research=research, analysis=analysis)["items"]}
        for rid in record_ids:
            identifier(rid)
            if rid not in available:
                raise Problem("这不是你的提醒", 403)
        with self.transaction():
            self.db.executemany("INSERT OR IGNORE INTO hub_reads VALUES(?,?)", [(actor_id, rid) for rid in record_ids])
        return {"marked": len(set(record_ids))}

    def personal_export(self, actor_id):
        self.actor(actor_id)
        return {"format": PERSONAL_FORMAT, "actor_id": actor_id,
                "following": sorted(self._marks(actor_id, "follow")),
                "bookmarks": sorted(self._marks(actor_id, "bookmark")),
                "read": sorted(r[0] for r in self.db.execute("SELECT record FROM hub_reads WHERE actor=?", (actor_id,))),
                "details": [{"exploration_id": wid, "topics": self.details(wid)["topics"], "help": self.details(wid)["help"]}
                            for wid, work in self.works.items() if work["actor_id"] == actor_id and self.details(wid)["version"]]}

    def personal_import(self, actor_id, bundle, *, research=None, analysis=None):
        self.actor(actor_id)
        exact(bundle, {"format", "actor_id", "following", "bookmarks", "read", "details"})
        if bundle["format"] != PERSONAL_FORMAT or bundle["actor_id"] != actor_id:
            raise Problem("请用备份原本所属的身份恢复", 403)
        for name in ("following", "bookmarks", "read", "details"):
            if type(bundle[name]) is not list or len(bundle[name]) > 10000:
                raise Problem("备份列表不正确")
        for name in ("following", "bookmarks"):
            if len(bundle[name]) > MAX_PREFERENCES:
                raise Problem("个人列表超出本地配额", 413)
            for wid in bundle[name]:
                self.work(wid)
        collaboration_ids = ({notice['id'] for notice in research.collaboration_notifications(actor_id)}
                             if research is not None else set())
        if analysis is not None:
            collaboration_ids |= {notice['id'] for notice in analysis.notices(actor_id)}
        for rid in bundle["read"]:
            identifier(rid)
            if self.index.get(rid, {}).get("type") not in {"revision", "comment", "evidence"} and rid not in collaboration_ids:
                raise Problem("请先导入对应的作品记录")
        cleaned, seen = [], set()
        for detail in bundle["details"]:
            exact(detail, {"exploration_id", "topics", "help"})
            wid = detail["exploration_id"]
            if self.work(wid)["actor_id"] != actor_id or wid in seen:
                raise Problem("备份包含不属于你的或重复的作品介绍", 403)
            seen.add(wid); text(detail["help"], 1000, True)
            cleaned.append((wid, {"topics": topics_list(detail["topics"]), "help": detail["help"].strip()}))
        # Replace only this person's preferences, not other people or article content.
        with self.transaction():
            self.db.execute("DELETE FROM hub_marks WHERE actor=?", (actor_id,))
            for name, kind in (("following", "follow"), ("bookmarks", "bookmark")):
                self.db.executemany("INSERT OR IGNORE INTO hub_marks VALUES(?,?,?,?)",
                                    [(actor_id, wid, kind, len(self.rows)) for wid in bundle[name]])
            self.db.execute("DELETE FROM hub_reads WHERE actor=?", (actor_id,))
            self.db.executemany("INSERT OR IGNORE INTO hub_reads VALUES(?,?)", [(actor_id, rid) for rid in bundle["read"]])
            for wid, detail in cleaned:
                prior = self._raw_details(wid)
                if all(prior[k] == detail[k] for k in detail):
                    continue
                public_value = {**detail, "version": prior["version"] + 1}
                stored = dict(public_value)
                for keep in ("keywords", "catalog_revision", "promotion_triage"):
                    if prior.get(keep):
                        stored[keep] = prior[keep]
                self.db.execute("INSERT INTO hub_details VALUES(?,?) ON CONFLICT(id) DO UPDATE SET document=excluded.document", (wid, canonical(stored)))
                self.db.execute("INSERT INTO hub_changes(exploration_id,document) VALUES(?,?)", (wid, canonical({"actor_id": actor_id, **public_value})))
        return {"restored": True}
