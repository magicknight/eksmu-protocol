"""Bounded private authentication counters; public instances persist limits."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import threading
import time

from .store import Problem


# (source requests/window, identity requests/window, window in seconds)
LIMITS = {
    'register': (40, 5, 3600),
    'login': (120, 8, 900),
    'rotate': (80, 8, 900),
    'email-register': (40, 5, 3600),
    'confirm-registration': (80, 8, 900),
    'password-login': (120, 8, 900),
    'attach-email': (40, 5, 3600),
    'change-password': (80, 8, 900),
    'request-verify': (80, 6, 900),
    'verify-email': (80, 8, 900),
    'request-reset': (80, 5, 3600),
    'reset-password': (80, 8, 900),
}
MAX_COUNTERS = 4096
TABLES = {'auth_rate_meta', 'auth_rate_counters'}


class AuthLimiter:
    def __init__(self, clock=None, *, database=None):
        self.clock = clock or (time.time if database is not None else time.monotonic)
        self.database = str(database) if database is not None else None
        self.secret = secrets.token_bytes(32)
        self.lock = threading.Lock()
        self.counters = {}
        self.calls = 0
        if self.database is not None:
            with sqlite3.connect(self.database, timeout=5) as db:
                db.execute('BEGIN IMMEDIATE')
                existing = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if existing & TABLES and not TABLES <= existing:
                    raise Problem('认证限速存储不完整，请维护者恢复', 503)
                if not TABLES <= existing:
                    db.execute('''CREATE TABLE auth_rate_meta(
                        id INTEGER PRIMARY KEY CHECK(id=1), secret BLOB NOT NULL)''')
                    db.execute('''CREATE TABLE auth_rate_counters(
                        action TEXT NOT NULL, kind TEXT NOT NULL, key BLOB NOT NULL,
                        deadline REAL NOT NULL, count INTEGER NOT NULL,
                        PRIMARY KEY(action,kind,key))''')
                    db.execute('INSERT INTO auth_rate_meta VALUES(1,?)', (self.secret,))
                row = db.execute('SELECT secret FROM auth_rate_meta WHERE id=1').fetchone()
                if row is None or not isinstance(row[0], bytes) or len(row[0]) != 32:
                    raise Problem('认证限速存储不可用，请维护者恢复', 503)
                self.secret = row[0]

    def check(self, action, source, identity):
        """Count requests even when credentials are wrong or the account is absent."""
        source_limit, identity_limit, window = LIMITS[action]
        # HMAC avoids retaining addresses, session tokens, or client addresses.
        identity = identity.strip().casefold() if isinstance(identity, str) else ''
        hashed = lambda value: hmac.new(self.secret, value.encode('utf-8', 'replace'), hashlib.sha256).digest()
        keys = ((action, 'source', hashed(str(source))),
                (action, 'identity', hashed(identity)))
        with self.lock:
            now = self.clock()
            if self.database is not None:
                self._persistent_check(keys, (source_limit, identity_limit), window, now)
                return
            self.calls += 1
            if self.calls % 100 == 0 or len(self.counters) >= MAX_COUNTERS - 2:
                self.counters = {key: value for key, value in self.counters.items() if value[0] > now}
            entries = [self.counters.get(key, (now + window, 0)) for key in keys]
            entries = [(deadline, count) if deadline > now else (now + window, 0)
                       for deadline, count in entries]
            if (entries[0][1] >= source_limit or entries[1][1] >= identity_limit
                    or len(self.counters) >= MAX_COUNTERS and any(key not in self.counters for key in keys)):
                raise Problem('操作过于频繁，请稍后重试', 429)
            for key, (deadline, count) in zip(keys, entries):
                self.counters[key] = deadline, count + 1

    def _persistent_check(self, keys, limits, window, now):
        with sqlite3.connect(self.database, timeout=5) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM auth_rate_counters WHERE deadline<=?', (now,))
            size = db.execute('SELECT COUNT(*) FROM auth_rate_counters').fetchone()[0]
            entries = [db.execute('''SELECT deadline,count FROM auth_rate_counters
                WHERE action=? AND kind=? AND key=?''', key).fetchone() for key in keys]
            if size + sum(entry is None for entry in entries) > MAX_COUNTERS:
                raise Problem('操作过于频繁，请稍后重试', 429)
            if any(entry is not None and entry[1] >= limit for entry, limit in zip(entries, limits)):
                raise Problem('操作过于频繁，请稍后重试', 429)
            for key, entry in zip(keys, entries):
                deadline, count = entry or (now + window, 0)
                db.execute('''INSERT INTO auth_rate_counters VALUES(?,?,?,?,?)
                    ON CONFLICT(action,kind,key) DO UPDATE SET count=excluded.count''',
                    (*key, deadline, count + 1))
