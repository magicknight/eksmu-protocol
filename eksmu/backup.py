"""Versioned OFFLINE instance snapshots, not public/untrusted content imports.

All writers must be stopped, including legacy programs without instance leases.
No scripts or SQL from a manifest are executed. Secrets use a separate directory.
"""
from __future__ import annotations

from contextlib import ExitStack, closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import time
import uuid

from . import __version__
from .blobstore import (
    create_blobstore,
    ensure_publication_blobs_schema,
    ensure_research_files_schema,
    is_gcs_configured,
)
from .maintenance import InstanceLock, RESTORING, local_path
from .store import canonical

FORMAT = 'eksmu.instance-backup.v2'
LEGACY_FORMAT = 'eksmu.instance-backup.v1'
SECRET_FORMAT = 'eksmu.instance-secrets.v1'
MAX_FILE = 512 * 1024 * 1024
MAX_MANIFEST = 128 * 1024
KEY_SUFFIX = '.receipt-signer.key'
KEY_FILE = 'receipt-signer.key'
# Every supported component has a recorded present/absent state.
COMPONENTS = {
    'records': ('', {'records'}),
    'community': ('.community.sqlite', {'hub_details', 'hub_changes', 'hub_marks', 'hub_reads',
                                       'hub_behavior_cases', 'hub_behavior_events',
                                       'hub_labels', 'hub_label_appeals', 'hub_trusted_authors'}),
    'research': ('.research.sqlite', {'research_files', 'research_tasks', 'research_events', 'research_claims'}),
    'receipts': ('.receipt-tasks.sqlite', {'receipt_tasks', 'receipt_attempts'}),
    'publications': ('.publications.sqlite', {'publication_snapshots', 'publication_blobs', 'publication_events'}),
    # AI analysis (RFC 0031). analysis_keys holds author API keys only in sealed
    # form and only until the worker runs; the worker's private key is never in a backup.
    'analysis': ('.analysis.sqlite', {'analysis_requests', 'analysis_reports', 'analysis_responses',
                                     'analysis_spend', 'analysis_worker', 'analysis_keys'}),
}
# Components added after a format was introduced: older backups may lack them.
LATE_COMPONENTS = {'analysis'}


def _sha(path: Path) -> str:
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def _regular(path: Path, maximum=MAX_FILE) -> None:
    local_path(path)
    if not path.is_file() or path.stat().st_size > maximum:
        raise ValueError('Missing, non-regular or oversized snapshot file')


def _info(path: Path) -> dict:
    _regular(path)
    return {'size': path.stat().st_size, 'sha256': _sha(path)}


def _check_info(path: Path, expected: dict):
    if (not isinstance(expected, dict) or set(expected) != {'size', 'sha256'}
            or type(expected['size']) is not int or not 0 <= expected['size'] <= MAX_FILE
            or not isinstance(expected['sha256'], str)
            or not re.fullmatch('[0-9a-f]{64}', expected['sha256'])):
        raise ValueError('Invalid snapshot file metadata')
    if _info(path) != expected:
        raise ValueError('Snapshot file size or digest mismatch')


def _write(path: Path, data: bytes):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


def _json_write(path: Path, value):
    _write(path, (canonical(value) + '\n').encode())


def _json_read(path: Path):
    _regular(path, MAX_MANIFEST)
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate manifest field')
            result[key] = value
        return result
    value = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=pairs)
    # Reject NaN, infinities and types outside the canonical JSON format.
    canonical(value)
    return value


def _sync_dir(path: Path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def application() -> dict:
    root = Path(__file__).resolve().parent.parent
    files = {}
    for name in ('eksmu', 'eksmu_bridge', 'eksmu_receipts', 'verification_lab'):
        for path in sorted((root / name).rglob('*')):
            if path.suffix in {'.py', '.js', '.css', '.html', '.svg'} or (path.suffix == '.json' and path.parent.name == 'locales'):
                _regular(path)
                files[path.relative_to(root).as_posix()] = _sha(path)
    for name in ('requirements-research.txt', 'requirements-receipts.txt'):
        path = root / name
        if path.exists():
            _regular(path)
            files[name] = _sha(path)
    return {'version': __version__, 'python': sys.version.split()[0],
            'sqlite': sqlite3.sqlite_version, 'files': files}


def _connect(path: Path, mode='ro'):
    connection = sqlite3.connect(path.as_uri() + '?mode=' + mode, uri=True, timeout=0)
    connection.execute('PRAGMA trusted_schema=OFF')
    return connection


def _check_database(path: Path, component: str):
    _regular(path)
    with closing(_connect(path)) as db:
        deadline = time.monotonic() + 15
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
        if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('SQLite integrity check failed')
        if db.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise ValueError('SQLite foreign-key check failed')
        objects = db.execute("SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
        tables = {name for kind, name in objects if kind == 'table'}
        required = COMPONENTS[component][1]
        optional = {'receipt_handoffs'} if component == 'receipts' else set()
        if component == 'research':
            lean_tables = {'research_lean_enrollments', 'research_lean_runs'}
            if tables & lean_tables and not lean_tables <= tables:
                raise ValueError('Lean activity tables are incomplete')
            optional = lean_tables | {'blobs', 'compile_jobs', 'research_redactions'}
        if component == 'publications':
            optional = {'blobs'}
        if component == 'records':
            from .accounts import TABLES
            from .modern_accounts import TABLES as MODERN_TABLES
            from .modern_accounts import REGISTRATION_TABLES
            from .auth_limits import TABLES as RATE_TABLES
            from .record_chain import TABLES as CHAIN_TABLES
            optional = TABLES | MODERN_TABLES | REGISTRATION_TABLES | RATE_TABLES | CHAIN_TABLES
            if tables & CHAIN_TABLES and not CHAIN_TABLES <= tables:
                raise ValueError('Record chain tables are incomplete')
            if tables & TABLES and not TABLES <= tables:
                raise ValueError('Account tables are incomplete')
            if tables & MODERN_TABLES and not MODERN_TABLES <= tables:
                raise ValueError('Email account tables are incomplete')
            if tables & RATE_TABLES and not RATE_TABLES <= tables:
                raise ValueError('Authentication rate-limit tables are incomplete')
        if not required <= tables or tables - required - optional:
            raise ValueError('Unsupported database tables; snapshot scope needs review')
        if any(kind not in {'table', 'index'} for kind, _ in objects):
            raise ValueError('Unsupported database object in instance snapshot')
        if component == 'records' and 'record_chain' in tables:
            from .record_chain import verify as verify_chain
            try:
                verify_chain(db)
            except Exception as exc:
                raise ValueError('Record chain check failed: ' + str(exc)) from exc


def _inventory(database: Path) -> dict[str, Path | None]:
    result = {}
    allowed = {database.name, database.name + '.maintenance.lock', database.name + '.requests.lock', database.name + KEY_SUFFIX}
    for component, (suffix, _) in COMPONENTS.items():
        path = Path(str(database) + suffix)
        allowed.update({path.name, path.name + '-wal', path.name + '-shm', path.name + '-journal'})
        for end in ('-wal', '-shm', '-journal'):
            sidecar = Path(str(path) + end)
            if sidecar.exists() or sidecar.is_symlink():
                _regular(sidecar)
        if path.exists() or path.is_symlink():
            _regular(path)
            result[component] = path
        else:
            result[component] = None
            if any(Path(str(path) + end).exists() for end in ('-wal', '-shm', '-journal')):
                raise ValueError('Orphaned SQLite sidecar; recover the source before backup')
    if result['records'] is None:
        raise ValueError('The instance records database is missing')
    # Refuse unknown sidecar components instead of silently omitting future data.
    for path in database.parent.iterdir():
        if path.name.startswith(database.name + '.') and path.name not in allowed:
            raise ValueError('Unknown instance sidecar; review the backup scope first')
    return result


def _separate(paths: list[Path]):
    for i, path in enumerate(paths):
        for other in paths[i + 1:]:
            if path.is_relative_to(other) or other.is_relative_to(path):
                raise ValueError('Data, backup and secret directories must be separate')


def _collect_referenced_blobs(directory: Path, parts: dict, *, strict=False) -> list[str]:
    referenced: set[str] = set()
    if parts.get('research') is not None:
        rpath = directory / 'research.sqlite'
        if rpath.is_file():
            with closing(_connect(rpath)) as rdb:
                tables = {r[0] for r in rdb.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                redacted_filter = ' WHERE id NOT IN (SELECT attachment_id FROM research_redactions)' if 'research_redactions' in tables else ''
                cols = {r[1] for r in rdb.execute('PRAGMA table_info(research_files)')}
                for (digest,) in rdb.execute(f'SELECT sha256 FROM research_files{redacted_filter}'):
                    if isinstance(digest, str) and re.fullmatch('[0-9a-f]{64}', digest):
                        referenced.add(digest)
                    elif strict:
                        raise ValueError('Invalid research attachment digest')
                if 'preview_sha256' in cols:
                    prev_where = (
                        f'{redacted_filter} AND preview_sha256 IS NOT NULL'
                        if redacted_filter else ' WHERE preview_sha256 IS NOT NULL'
                    )
                    for (pdigest,) in rdb.execute(f'SELECT preview_sha256 FROM research_files{prev_where}'):
                        if isinstance(pdigest, str) and re.fullmatch('[0-9a-f]{64}', pdigest):
                            referenced.add(pdigest)
                        elif strict:
                            raise ValueError('Invalid preview digest')
    if parts.get('publications') is not None:
        ppath = directory / 'publications.sqlite'
        if ppath.is_file():
            with closing(_connect(ppath)) as pdb:
                for (digest,) in pdb.execute('SELECT sha256 FROM publication_blobs'):
                    if isinstance(digest, str) and re.fullmatch('[0-9a-f]{64}', digest):
                        referenced.add(digest)
                    elif strict:
                        raise ValueError('Invalid publication blob digest')
                for (doc,) in pdb.execute('SELECT document FROM publication_snapshots'):
                    try:
                        snap = json.loads(doc)
                        for item in snap.get('materials', ()):
                            digest = item.get('sha256') if isinstance(item, dict) else None
                            if isinstance(digest, str) and re.fullmatch('[0-9a-f]{64}', digest):
                                referenced.add(digest)
                            elif strict:
                                raise ValueError('Invalid snapshot material digest')
                    except Exception:
                        if strict:
                            raise ValueError('Invalid publication snapshot material list')
    return sorted(referenced)


def create_backup(database: str | Path, destination: str | Path, *, offline=False,
                  secrets_dir: str | Path | None = None, blobstore=None,
                  portable_blobs=False) -> dict:
    if offline is not True:
        raise ValueError('Stop all instance writers and explicitly select offline backup')
    database, destination = local_path(database), local_path(destination)
    secret_root = local_path(secrets_dir) if secrets_dir is not None else None
    _separate([database.parent, destination] + ([secret_root] if secret_root else []))
    if destination.exists() or (secret_root and secret_root.exists()):
        raise ValueError('Backup destinations must be new directories')
    gcs_mode = (blobstore is not None and getattr(blobstore, 'kind', None) == 'gcs') or is_gcs_configured()
    if portable_blobs and not gcs_mode:
        raise ValueError('Portable blob copying requires a configured GCS source')
    created = []
    with InstanceLock(database, exclusive=True), ExitStack() as stack:
        inventory = _inventory(database)
        key = Path(str(database) + KEY_SUFFIX)
        has_key = key.exists() or key.is_symlink()
        if inventory['receipts'] is not None and not has_key:
            raise ValueError('Receipt database has no signing key; recover it before full backup')
        if has_key:
            _regular(key, 32)
            if key.stat().st_size != 32 or secret_root is None:
                raise ValueError('Signing key requires a separate --secrets-dir and valid 32-byte key')
        elif secret_root is not None:
            raise ValueError('This instance has no service signing key to back up')
        # Reserve writes on all existing DBs before taking any snapshot. A separate
        # connection performs SQLite backup so the write guard cannot self-deadlock.
        for path in inventory.values():
            if path is not None:
                guard = stack.enter_context(closing(_connect(path, 'rw')))
                guard.execute('BEGIN IMMEDIATE')
        try:
            destination.mkdir(mode=0o700)
            created.append(destination)
            _write(destination / '.incomplete', b'offline backup in progress\n')
            manifest = {'format': FORMAT, 'id': str(uuid.uuid4()),
                        'created_at': datetime.now(timezone.utc).isoformat(),
                        'consistency': 'offline-instance-lease-and-sqlite-write-guards',
                        'application': application(), 'components': {}, 'signer': None}
            for component, path in inventory.items():
                if path is None:
                    manifest['components'][component] = None
                    continue
                target = destination / (component + '.sqlite')
                _write(target, b'')
                deadline = time.monotonic() + 60
                def progress(*_):
                    if time.monotonic() > deadline:
                        raise ValueError('SQLite backup exceeded its time limit')
                with closing(_connect(path)) as source, closing(sqlite3.connect(target, isolation_level=None)) as output:
                    source.backup(output, pages=256, progress=progress, sleep=.02)
                    if gcs_mode and component == 'research':
                        ensure_research_files_schema(output)
                        output.execute('BEGIN IMMEDIATE')
                        output.execute('UPDATE research_files SET data=NULL, preview=NULL')
                        output.execute('DROP TABLE IF EXISTS blobs')
                        output.commit()
                        output.execute('VACUUM')
                    elif gcs_mode and component == 'publications':
                        ensure_publication_blobs_schema(output)
                        output.execute('BEGIN IMMEDIATE')
                        output.execute('UPDATE publication_blobs SET data=NULL')
                        output.execute('DROP TABLE IF EXISTS blobs')
                        output.commit()
                        output.execute('VACUUM')
                    output.execute('PRAGMA journal_mode=DELETE')
                _check_database(target, component)
                with target.open('rb') as source:
                    os.fsync(source.fileno())
                manifest['components'][component] = _info(target)
            if gcs_mode:
                manifest['blob_backend'] = 'embedded-sqlite' if portable_blobs else 'gcs'
                manifest['referenced_blobs'] = _collect_referenced_blobs(destination, manifest['components'], strict=portable_blobs)
                if portable_blobs:
                    source_store = create_blobstore(blobstore=blobstore)
                    for component in ('research', 'publications'):
                        if manifest['components'].get(component) is None:
                            continue
                        target = destination / (component + '.sqlite')
                        refs = _collect_referenced_blobs(destination, {component: manifest['components'][component]}, strict=True)
                        with closing(sqlite3.connect(target)) as output:
                            output.execute('CREATE TABLE IF NOT EXISTS blobs(sha256 TEXT PRIMARY KEY, data BLOB NOT NULL)')
                            for digest in refs:
                                raw = source_store.get(digest)
                                if hashlib.sha256(raw).hexdigest() != digest:
                                    raise ValueError('Downloaded blob digest mismatch')
                                output.execute('INSERT INTO blobs(sha256,data) VALUES (?,?)', (digest, raw))
                            output.commit()
                        _check_database(target, component)
                        with target.open('rb') as source:
                            os.fsync(source.fileno())
                        manifest['components'][component] = _info(target)
            if has_key:
                secret_root.mkdir(mode=0o700)
                created.append(secret_root)
                raw = key.read_bytes()
                _write(secret_root / KEY_FILE, raw)
                manifest['signer'] = _info(secret_root / KEY_FILE)
                _json_write(secret_root / 'secrets.json', {'format': SECRET_FORMAT,
                            'backup_id': manifest['id'], 'signer': manifest['signer']})
                _sync_dir(secret_root)
                _sync_dir(secret_root.parent)
            if _inventory(database) != inventory:
                raise ValueError('Instance components changed during backup')
            _json_write(destination / 'manifest.json', manifest)
            (destination / '.incomplete').unlink()
            _sync_dir(destination)
            _sync_dir(destination.parent)
            return verify_backup(destination, secrets_dir=secret_root, blobstore=blobstore)
        except BaseException:
            for path in reversed(created):
                shutil.rmtree(path)
            raise


def verify_backup(directory: str | Path, *, secrets_dir: str | Path | None = None, blobstore=None) -> dict:
    directory = local_path(directory)
    if not directory.is_dir():
        raise ValueError('Backup directory is missing')
    manifest = _json_read(directory / 'manifest.json')
    fields = {'format', 'id', 'created_at', 'consistency', 'application', 'components', 'signer'}
    gcs_fields = fields | {'blob_backend', 'referenced_blobs'}
    if (not isinstance(manifest, dict) or set(manifest) not in (fields, gcs_fields)
            or manifest['format'] not in (FORMAT, LEGACY_FORMAT)):
        raise ValueError('Unsupported instance backup manifest')
    if not isinstance(manifest['id'], str) or str(uuid.UUID(manifest['id'])) != manifest['id']:
        raise ValueError('Invalid backup ID')
    if (not isinstance(manifest['created_at'], str)
            or datetime.fromisoformat(manifest['created_at']).utcoffset() is None):
        raise ValueError('Backup creation time must include a timezone')
    if manifest['consistency'] != 'offline-instance-lease-and-sqlite-write-guards':
        raise ValueError('Unsupported snapshot consistency mode')
    app = manifest['application']
    if (not isinstance(app, dict) or set(app) != {'version', 'python', 'sqlite', 'files'}
            or any(not isinstance(app[k], str) for k in ('version', 'python', 'sqlite'))
            or not isinstance(app['files'], dict) or not app['files']):
        raise ValueError('Invalid source version record')
    for name, value in app['files'].items():
        if not isinstance(name, str) or not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
            raise ValueError('Invalid source digest record')
    parts = manifest['components']
    expected_parts = set(COMPONENTS) - ({'publications'} if manifest['format'] == LEGACY_FORMAT else set())
    if isinstance(parts, dict):
        expected_parts -= LATE_COMPONENTS - set(parts)
    if not isinstance(parts, dict) or set(parts) != expected_parts or parts['records'] is None:
        raise ValueError('Backup component inventory is incomplete')
    names = {'manifest.json'}
    for component, expected in parts.items():
        if expected is not None:
            path = directory / (component + '.sqlite')
            names.add(path.name)
            _check_info(path, expected)
            _check_database(path, component)
    if {p.name for p in directory.iterdir()} != names:
        raise ValueError('Backup directory contains extra files or an incomplete marker')
    actual_referenced = _collect_referenced_blobs(directory, parts, strict=manifest.get('blob_backend') == 'embedded-sqlite')
    if 'blob_backend' in manifest:
        if manifest.get('blob_backend') not in ('gcs', 'embedded-sqlite') or not isinstance(manifest.get('referenced_blobs'), list):
            raise ValueError('Invalid backup blob storage metadata')
        ref_list = manifest['referenced_blobs']
        if (any(not isinstance(d, str) or not re.fullmatch('[0-9a-f]{64}', d) for d in ref_list)
                or ref_list != sorted(set(ref_list))
                or ref_list != actual_referenced):
            raise ValueError('Backup referenced_blobs list is invalid or out of sync with SQLite metadata')
    portable = manifest.get('blob_backend') == 'embedded-sqlite'
    if portable:
        for component in ('research', 'publications'):
            if parts.get(component) is None:
                continue
            refs = _collect_referenced_blobs(directory, {component: parts[component]}, strict=True)
            with closing(_connect(directory / (component + '.sqlite'))) as db:
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if 'blobs' not in tables:
                    raise ValueError('Portable backup missing blob table')
                for digest in refs:
                    row = db.execute('SELECT data FROM blobs WHERE sha256=?', (digest,)).fetchone()
                    if row is None or hashlib.sha256(row[0]).hexdigest() != digest:
                        raise ValueError('Portable backup missing or corrupted blob: ' + digest)
    gcs_mode = not portable and (('blob_backend' in manifest) or (blobstore is not None and getattr(blobstore, 'kind', None) == 'gcs') or is_gcs_configured())
    if gcs_mode:
        referenced = manifest.get('referenced_blobs', actual_referenced)
        store = create_blobstore(blobstore=blobstore)
        for digest in referenced:
            try:
                present = store.exists(digest)
            except Exception as exc:
                raise ValueError(f'Failed to verify referenced blob in object storage: {digest}') from exc
            if not present:
                raise ValueError(f'Missing referenced blob in object storage: {digest}')
    if parts['receipts'] is not None and manifest['signer'] is None:
        raise ValueError('Receipt records require a separately backed up signer')
    secret_root = local_path(secrets_dir) if secrets_dir is not None else None
    if manifest['signer'] is not None:
        info = manifest['signer']
        if (not isinstance(info, dict) or set(info) != {'size', 'sha256'} or info['size'] != 32
                or not isinstance(info['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', info['sha256'])):
            raise ValueError('Invalid signing key metadata')
        if secret_root is not None:
            _separate([directory, secret_root])
            if {p.name for p in secret_root.iterdir()} != {'secrets.json', KEY_FILE}:
                raise ValueError('Invalid separate secret directory')
            if _json_read(secret_root / 'secrets.json') != {
                    'format': SECRET_FORMAT, 'backup_id': manifest['id'], 'signer': info}:
                raise ValueError('Signing key package belongs to another snapshot')
            _check_info(secret_root / KEY_FILE, info)
    elif secret_root is not None:
        raise ValueError('Unexpected secret package for a snapshot without a signer')
    return {'manifest': manifest, 'data_verified': True,
            'independent_blob_bytes_verified': portable,
            'secrets_required': manifest['signer'] is not None,
            'secrets_verified': secret_root is not None,
            'runtime_matches': app['files'] == application()['files']}


def restore_backup(directory: str | Path, destination: str | Path, *,
                   secrets_dir: str | Path | None = None, blobstore=None) -> Path:
    directory, destination = local_path(directory), local_path(destination)
    secret_root = local_path(secrets_dir) if secrets_dir is not None else None
    _separate([directory, destination] + ([secret_root] if secret_root else []))
    checked = verify_backup(directory, secrets_dir=secret_root, blobstore=blobstore)
    manifest = checked['manifest']
    if not checked['runtime_matches']:
        raise ValueError('Restore with the recorded runtime source; automatic migration is not supported')
    if checked['secrets_required'] and not checked['secrets_verified']:
        raise ValueError('Full restore requires the separate signing key package')
    # Exclusive mkdir reserves the destination; existing even empty directories
    # are never replaced. Interrupted restores retain a marker that servers reject.
    destination.mkdir(mode=0o700)
    try:
        _write(destination / RESTORING, b'restore incomplete; do not serve\n')
        database = destination / 'community.sqlite'
        for component, info in manifest['components'].items():
            if info is None:
                continue
            source = directory / (component + '.sqlite')
            target = Path(str(database) + COMPONENTS[component][0])
            _copy(source, target)
            _check_info(target, info)
            _check_database(target, component)
        if manifest['signer'] is not None:
            target = Path(str(database) + KEY_SUFFIX)
            _copy(secret_root / KEY_FILE, target)
            _check_info(target, manifest['signer'])
        # Byte checks happen before this deliberate authentication-state change.
        # A restored database cannot know about later revocations on its own.
        from .store import Store
        from .accounts import Accounts, TABLES
        with closing(_connect(database)) as check:
            managed = bool({r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")} & TABLES)
        store = Store(database)
        try:
            account = Accounts(store)
            if not managed:
                # A pre-account snapshot has no instance ID; reconciliation must
                # match every immutable author against the selected newer source.
                with store.db:
                    store.db.execute("UPDATE auth_meta SET instance='legacy-restore-unbound' WHERE id=1")
            account.quarantine_restore()
            tables = {r[0] for r in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'auth_pending_registrations' in tables:
                with store.db:
                    # A restored pending code may have been consumed on the
                    # authoritative instance. Never restore registration grants.
                    store.db.execute('DELETE FROM auth_pending_registrations')
        finally:
            store.close()
        _json_write(destination / 'restore-record.json', {
            'format': 'eksmu.instance-restore.v1', 'backup_id': manifest['id'],
            'restored_at': datetime.now(timezone.utc).isoformat(),
            'application': manifest['application'], 'account_review_required': True})
        (destination / RESTORING).unlink()
        _sync_dir(destination)
        _sync_dir(destination.parent)
        return database
    except BaseException:
        shutil.rmtree(destination)
        raise


def _copy(source: Path, destination: Path):
    _regular(source)
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with source.open('rb') as incoming, os.fdopen(fd, 'wb') as output:
        shutil.copyfileobj(incoming, output, 1024 * 1024)
        output.flush()
        os.fsync(output.fileno())
