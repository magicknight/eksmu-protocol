"""Private account/session state, separate from immutable publication records."""
from contextlib import contextmanager
import base64
import hashlib
import hmac
import secrets
import time
import uuid

from .store import Problem, canonical, digest, display_name, exact, identifier, record, text

TABLES = {'auth_meta', 'auth_accounts', 'auth_sessions', 'auth_history', 'auth_operations'}
SESSION_SECONDS = 7 * 24 * 3600
MAX_SESSIONS = 20
MAX_OPERATIONS = 50000


def fingerprint(value):
    text(value, 128)
    return hashlib.sha256(value.encode()).hexdigest()


def secret(value, prefix):
    text(value, 128)
    if not value.startswith(prefix):
        raise Problem('凭证格式不正确')
    raw = value[len(prefix):]
    try:
        decoded = base64.urlsafe_b64decode(raw + '=')
    except (ValueError, TypeError):
        raise Problem('凭证格式不正确') from None
    if len(decoded) != 32 or base64.urlsafe_b64encode(decoded).decode().rstrip('=') != raw:
        raise Problem('凭证需要32字节随机值')
    return fingerprint(value)


@contextmanager
def transaction(db):
    owner = not db.in_transaction
    if owner:
        db.execute('BEGIN IMMEDIATE')
    try:
        yield
        if owner:
            db.commit()
    except BaseException:
        if owner:
            db.rollback()
        raise


def active_session(db, token):
    value = fingerprint(token)
    row = db.execute('''SELECT s.id,s.actor,s.created,s.expires,s.label FROM auth_sessions s
        JOIN auth_accounts a ON a.actor=s.actor
        WHERE s.token_hash=? AND s.revoked=0 AND s.expires>? AND s.generation=a.generation''',
        (value, int(time.time()))).fetchone()
    if row is None:
        raise Problem('会话已失效，请用恢复凭证重新登录', 401)
    return row


def authenticate(store, token):
    """Missing tables in a managed database fail closed; never fall back on errors."""
    tables = {r[0] for r in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not tables & TABLES:
        return store._legacy_authenticate(token)
    if not TABLES <= tables:
        raise Problem('账户存储不完整，请维护者恢复', 503)
    meta = store.db.execute('SELECT version,restore_gate FROM auth_meta WHERE id=1').fetchone()
    if meta is None or meta[0] != 1:
        raise Problem('账户存储版本不可用', 503)
    if meta[1]:
        raise Problem('实例恢复后须先核对较新的凭证状态，当前登录已停用', 401)
    text(token, 128)
    if token.startswith('ois1_'):
        return active_session(store.db, token)[1]
    actor = store._legacy_authenticate(token)
    if store.db.execute('SELECT 1 FROM auth_accounts WHERE actor=?', (actor,)).fetchone():
        raise Problem('旧身份已迁移，请用恢复凭证登录取得会话', 401)
    return actor


class Accounts:
    def __init__(self, store):
        self.store, self.db = store, store.db
        existing = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if existing & TABLES:
            if not TABLES <= existing:
                raise Problem('账户存储不完整，请维护者恢复', 503)
            row = self.db.execute('SELECT version FROM auth_meta WHERE id=1').fetchone()
            if row != (1,):
                raise Problem('账户存储版本不可用', 503)
            return
        # Initialization is atomic; no partly created tables can enable a legacy fallback.
        with transaction(self.db):
            self.db.execute('CREATE TABLE auth_meta(id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER NOT NULL,instance TEXT NOT NULL,restore_gate INTEGER NOT NULL)')
            self.db.execute('CREATE TABLE auth_accounts(actor TEXT PRIMARY KEY REFERENCES records(id),recovery_hash TEXT UNIQUE NOT NULL,generation INTEGER NOT NULL)')
            self.db.execute('CREATE TABLE auth_sessions(id TEXT PRIMARY KEY,actor TEXT NOT NULL REFERENCES auth_accounts(actor),token_hash TEXT UNIQUE NOT NULL,generation INTEGER NOT NULL,created INTEGER NOT NULL,expires INTEGER NOT NULL,label TEXT NOT NULL,revoked INTEGER NOT NULL)')
            self.db.execute('CREATE TABLE auth_history(key_hash TEXT PRIMARY KEY,actor TEXT NOT NULL REFERENCES auth_accounts(actor))')
            self.db.execute('CREATE TABLE auth_operations(id TEXT PRIMARY KEY,request_hash TEXT NOT NULL,actor TEXT NOT NULL REFERENCES auth_accounts(actor),session TEXT NOT NULL REFERENCES auth_sessions(id),kind TEXT NOT NULL)')
            gate = int((store.path.parent / 'restore-record.json').exists())
            self.db.execute('INSERT INTO auth_meta VALUES(1,1,?,?)',
                            ('legacy-restore-unbound' if gate else str(uuid.uuid4()), gate))

    def info(self):
        gate = bool(self.db.execute('SELECT restore_gate FROM auth_meta WHERE id=1').fetchone()[0])
        return {'format': 'eksmu.accounts.v1', 'recovery_review_required': gate,
                'session_lifetime_seconds': SESSION_SECONDS}

    def _ready(self):
        if self.info()['recovery_review_required']:
            raise Problem('实例恢复后须先由维护者核对较新的凭证状态', 409)

    def _actor(self, actor):
        actors = self.store._actors() if hasattr(self.store, '_actors') else (r for r in self.store.rows() if r['type'] == 'actor')
        return next(r for r in actors if r['id'] == actor)

    def _result(self, actor, session):
        row = self.db.execute('SELECT expires FROM auth_sessions WHERE id=?', (session,)).fetchone()
        return {'actor': self._actor(actor), 'session_id': session, 'expires_at': row[0]}

    def _replay(self, kind, payload):
        identifier(payload['operation_id'])
        request_hash = digest({'action': kind, **payload})
        row = self.db.execute('SELECT request_hash,actor,session FROM auth_operations WHERE id=?',
                              (payload['operation_id'],)).fetchone()
        if row:
            if not hmac.compare_digest(row[0], request_hash):
                raise Problem('此操作编号已用于不同请求', 409)
            try:
                active = active_session(self.db, payload['session_token'])
            except Problem:
                raise Problem('已完成请求对应的会话已撤销或过期，请重新登录', 409) from None
            if active[0] != row[2] or active[1] != row[1]:
                raise Problem('操作状态不一致', 409)
            return self._result(row[1], row[2])
        if self.db.execute('SELECT COUNT(*) FROM auth_operations').fetchone()[0] >= MAX_OPERATIONS:
            raise Problem('本地账户操作记录已满，请联系维护者', 503)
        return None

    def _unused_recovery(self, key_hash):
        if self.db.execute('SELECT 1 FROM auth_history WHERE key_hash=?', (key_hash,)).fetchone():
            raise Problem('恢复凭证不能重复使用', 409)
        actors = self.store._actors() if hasattr(self.store, '_actors') else (r for r in self.store.rows() if r['type'] == 'actor')
        if any(r['payload']['key_hash'] == key_hash for r in actors):
            raise Problem('恢复凭证不能重复使用', 409)

    def _credential(self, credential):
        value = fingerprint(credential)
        row = self.db.execute('SELECT actor,generation FROM auth_accounts WHERE recovery_hash=?', (value,)).fetchone()
        if row:
            return row
        try:
            actor = self.store._legacy_authenticate(credential)
        except Problem:
            raise Problem('恢复凭证无效，请检查保存的文件', 401) from None
        if self.db.execute('SELECT 1 FROM auth_accounts WHERE actor=?', (actor,)).fetchone():
            raise Problem('恢复凭证无效，请使用最新凭证', 401)
        self.db.execute('INSERT INTO auth_accounts VALUES(?,?,1)', (actor, value))
        self.db.execute('INSERT INTO auth_history VALUES(?,?)', (value, actor))
        return actor, 1

    def _new_session(self, actor, generation, kind, payload):
        value = secret(payload['session_token'], 'ois1_')
        text(payload['label'], 64)
        if self.db.execute('SELECT 1 FROM auth_sessions WHERE token_hash=?', (value,)).fetchone():
            raise Problem('会话凭证不能重复使用', 409)
        now = int(time.time())
        count = self.db.execute('SELECT COUNT(*) FROM auth_sessions WHERE actor=? AND revoked=0 AND expires>?', (actor, now)).fetchone()[0]
        if count >= MAX_SESSIONS:
            raise Problem('有效会话已达20个，请先退出其他设备', 409)
        sid = str(uuid.uuid4())
        self.db.execute('INSERT INTO auth_sessions VALUES(?,?,?,?,?,?,?,0)',
                        (sid, actor, value, generation, now, now + SESSION_SECONDS, payload['label']))
        self.db.execute('INSERT INTO auth_operations VALUES(?,?,?,?,?)',
                        (payload['operation_id'], digest({'action': kind, **payload}), actor, sid, kind))
        return self._result(actor, sid)

    def issue(self, kind, payload):
        fields = {'operation_id', 'session_token', 'label'}
        fields |= {'alias', 'recovery'} if kind == 'register' else {'credential'}
        if kind == 'rotate':
            fields |= {'new_recovery', 'actor_id'}
        if kind not in {'register', 'login', 'rotate'}:
            raise Problem('未知账户操作', 404)
        exact(payload, fields)
        secret(payload['session_token'], 'ois1_')
        with transaction(self.db):
            self._ready()
            previous = self._replay(kind, payload)
            if previous:
                return previous
            if kind == 'register':
                display_name(payload['alias'])
                value = secret(payload['recovery'], 'oir1_')
                self._unused_recovery(value)
                # Archival actor fingerprint is NOT a live recovery credential.
                actor_row = record('actor', None, {'alias': payload['alias'],
                    'key_hash': fingerprint(secrets.token_urlsafe(32))})
                self.store._append([actor_row])
                actor, generation = actor_row['id'], 1
                self.db.execute('INSERT INTO auth_accounts VALUES(?,?,?)', (actor, value, generation))
                self.db.execute('INSERT INTO auth_history VALUES(?,?)', (value, actor))
            else:
                actor, generation = self._credential(payload['credential'])
                if kind == 'rotate':
                    if payload['actor_id'] != actor:
                        raise Problem('恢复凭证不属于当前身份', 403)
                    value = secret(payload['new_recovery'], 'oir1_')
                    self._unused_recovery(value)
                    generation += 1
                    self.db.execute('UPDATE auth_accounts SET recovery_hash=?,generation=? WHERE actor=?',
                                    (value, generation, actor))
                    self.db.execute('INSERT INTO auth_history VALUES(?,?)', (value, actor))
                    self.db.execute('UPDATE auth_sessions SET revoked=1 WHERE actor=?', (actor,))
            return self._new_session(actor, generation, kind, payload)

    def status(self, token):
        result = self.info()
        try:
            self._ready()
            row = active_session(self.db, token)
            result.update(self._result(row[1], row[0]))
        except Problem as exc:
            if exc.status not in {400, 401, 409}:
                raise
            result.update({'actor': None, 'session_id': None})
        return result

    def sessions(self, token):
        self._ready()
        current = active_session(self.db, token)
        rows = self.db.execute('SELECT id,created,expires,label FROM auth_sessions WHERE actor=? AND revoked=0 AND expires>? ORDER BY created,id',
                               (current[1], int(time.time()))).fetchall()
        return {'sessions': [{'id': r[0], 'created_at': r[1], 'expires_at': r[2], 'label': r[3], 'current': r[0] == current[0]} for r in rows]}

    def revoke(self, token, *, session_id=None, others=False):
        with transaction(self.db):
            self._ready()
            current = active_session(self.db, token)
            if others:
                self.db.execute('UPDATE auth_sessions SET revoked=1 WHERE actor=? AND id<>?', (current[1], current[0]))
            else:
                identifier(session_id)
                row = self.db.execute('SELECT actor FROM auth_sessions WHERE id=?', (session_id,)).fetchone()
                if row is None or row[0] != current[1]:
                    raise Problem('不能管理此会话', 403)
                self.db.execute('UPDATE auth_sessions SET revoked=1 WHERE id=?', (session_id,))
        return {'revoked': True}

    def logout(self, token):
        # Idempotent even if the first successful response was lost.
        with transaction(self.db):
            self.db.execute('UPDATE auth_sessions SET revoked=1 WHERE token_hash=?', (fingerprint(token),))
        return {'logged_out': True}

    def quarantine_restore(self):
        with transaction(self.db):
            self.db.execute('UPDATE auth_sessions SET revoked=1')
            self.db.execute('UPDATE auth_meta SET restore_gate=1 WHERE id=1')
