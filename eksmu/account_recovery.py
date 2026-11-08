"""Offline reconciliation after restoring a stale snapshot; never revive sessions."""
from contextlib import closing
from pathlib import Path
import sqlite3

from .accounts import Accounts, TABLES, transaction
from .modern_accounts import ModernAccounts, TABLES as MODERN_TABLES
from .maintenance import InstanceLock, local_path
from .store import Store, Problem


def reconcile_accounts(database, latest_database, *, offline=False):
    if offline is not True:
        raise ValueError('Stop both instances and explicitly select offline reconciliation')
    target, source = local_path(database), local_path(latest_database)
    if target == source or not target.is_file() or not source.is_file():
        raise ValueError('Use distinct existing restored and authoritative databases')
    with InstanceLock(target, exclusive=True), InstanceLock(source, exclusive=True):
        # Validate the target read-only before Store/Accounts can initialize tables.
        # An accidental path must not turn an unrelated database into an instance.
        with closing(sqlite3.connect(target.as_uri() + '?mode=ro', uri=True, timeout=0)) as check:
            target_tables = {r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not TABLES | {'records'} <= target_tables:
                raise ValueError('Target must be an upgraded restored instance awaiting account review')
            target_meta = check.execute('SELECT version,restore_gate FROM auth_meta WHERE id=1').fetchone()
            if target_meta != (1, 1):
                raise ValueError('Target is not awaiting restored-account review')
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=0)) as latest:
            latest.execute('BEGIN')
            tables = {r[0] for r in latest.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not TABLES <= tables:
                raise ValueError('Authoritative source must first be upgraded to the account version')
            if tables & MODERN_TABLES and not MODERN_TABLES <= tables:
                raise ValueError('Authoritative email account tables are incomplete')
            source_modern = MODERN_TABLES <= tables
            source_passwords = latest.execute('SELECT * FROM auth_passwords').fetchall() if source_modern else []
            source_profiles = latest.execute('SELECT * FROM auth_profiles').fetchall() if source_modern else []
            source_meta = latest.execute('SELECT version,instance,restore_gate FROM auth_meta WHERE id=1').fetchone()
            if source_meta is None or source_meta[0] != 1 or source_meta[2]:
                raise ValueError('Authoritative source is unavailable or itself awaiting recovery review')
            import json
            source_actors = {row['id']: row for raw, in latest.execute('SELECT document FROM records')
                             if (row := json.loads(raw))['type'] == 'actor'}
            store = Store(target)
            try:
                account = Accounts(store)
                meta = store.db.execute('SELECT instance,restore_gate FROM auth_meta WHERE id=1').fetchone()
                if not meta[1]:
                    raise ValueError('Target is not awaiting restored-account review')
                if meta[0] not in {source_meta[1], 'legacy-restore-unbound'}:
                    raise ValueError('Databases do not belong to the same account instance')
                actors = [r for r in store.rows() if r['type'] == 'actor']
                with transaction(store.db):
                    for actor in actors:
                        aid = actor['id']
                        if source_actors.get(aid) != actor:
                            raise ValueError('Restored author is missing or differs in the authoritative source')
                        row = latest.execute('SELECT recovery_hash,generation FROM auth_accounts WHERE actor=?', (aid,)).fetchone()
                        source_generation = row[1] if row else 0
                        value, generation = row or (actor['payload']['key_hash'], 1)
                        previous = store.db.execute('SELECT recovery_hash,generation FROM auth_accounts WHERE actor=?', (aid,)).fetchone()
                        if previous and (previous[1] > source_generation or
                                         (previous[1] == source_generation and previous[0] != value)):
                            raise ValueError('Selected source has older or conflicting recovery credentials')
                        store.db.execute('INSERT INTO auth_accounts VALUES(?,?,?) ON CONFLICT(actor) DO UPDATE SET recovery_hash=excluded.recovery_hash,generation=excluded.generation', (aid, value, generation))
                        history = [(value, aid)] + latest.execute('SELECT key_hash,actor FROM auth_history WHERE actor=?', (aid,)).fetchall()
                        for key_hash, owner in history:
                            other = store.db.execute('SELECT actor FROM auth_history WHERE key_hash=?', (key_hash,)).fetchone()
                            if other and other[0] != owner:
                                raise ValueError('Conflicting credential history')
                            store.db.execute('INSERT OR IGNORE INTO auth_history VALUES(?,?)', (key_hash, owner))
                        sessions = latest.execute('SELECT id,actor,token_hash,generation,created,expires,label FROM auth_sessions WHERE actor=?', (aid,)).fetchall()
                        for session in sessions:
                            old = store.db.execute('SELECT actor,token_hash FROM auth_sessions WHERE id=?', (session[0],)).fetchone()
                            if old and old != session[1:3]:
                                raise ValueError('Conflicting session history')
                            store.db.execute('INSERT OR IGNORE INTO auth_sessions VALUES(?,?,?,?,?,?,?,1)', session)
                    ModernAccounts(store)
                    restored_actor_ids = {actor['id'] for actor in actors}
                    source_password_by_actor = {row[0]: row for row in source_passwords if row[0] in restored_actor_ids}
                    source_profile_by_actor = {row[0]: row for row in source_profiles if row[0] in restored_actor_ids}
                    for table, source_by_actor in (('auth_passwords', source_password_by_actor),
                                                   ('auth_profiles', source_profile_by_actor)):
                        current = {row[0]: row for row in store.db.execute('SELECT * FROM ' + table)}
                        for aid, row in current.items():
                            newer = source_by_actor.get(aid)
                            if newer is None or row[-1] > newer[-1] or (row[-1] == newer[-1] and row != newer):
                                raise ValueError('Selected source has older or conflicting email/profile state')
                        store.db.execute('DELETE FROM ' + table)
                        if source_by_actor:
                            placeholders = ','.join('?' for _ in next(iter(source_by_actor.values())))
                            store.db.executemany('INSERT INTO ' + table + ' VALUES(' + placeholders + ')',
                                                 source_by_actor.values())
                    store.db.execute('DELETE FROM auth_email_challenges')
                    account.quarantine_restore()
                    store.db.execute('UPDATE auth_meta SET instance=?,restore_gate=0 WHERE id=1', (source_meta[1],))
                return {'accounts_reconciled': len(actors), 'all_sessions_revoked': True,
                        'recovery_review_required': False, 'service_started': False}
            finally:
                store.close()
