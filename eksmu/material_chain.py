"""Attachment commitments in the public record chain (RFC 0032).

Offline or lock-coordinated restartable attachment commitments; never submits
external timestamps directly.

Every backfilled attachment only acquires an external upper-bound time when a
later independent timestamp covers its chain position.
"""
from __future__ import annotations

from contextlib import nullcontext
import hashlib
from pathlib import Path
import uuid

from .accounts import transaction
from . import record_chain
from .maintenance import InstanceLock, RequestLock, local_path
from .store import MAX_BUNDLE_BYTES, Problem, Store, canonical, digest as json_digest, record, validate

_NAMESPACE = uuid.UUID('8cbda154-3c17-48bf-ad0c-5c6c5b735fea')


def material_record_id(attachment_id: str) -> str:
    """Deterministic UUIDv5 record ID for an attachment's material record."""
    return str(uuid.uuid5(_NAMESPACE, str(attachment_id)))


def commit_materials(store, research, *, backfilled: bool = True, request_lock_db: str | Path | None = None) -> dict:
    """Verify stored attachment bytes and idempotently append material records to record_chain."""
    files = research.db.execute('SELECT * FROM research_files ORDER BY rowid').fetchall()
    redacted_ids = {
        r[0] for r in research.db.execute(
            "SELECT attachment_id FROM research_redactions"
        )
    }
    added = existing = 0
    for item in files:
        rid = material_record_id(item['id'])
        payload = {'attachment_id': item['id'], 'revision_id': item['revision_id'],
                   'size': item['size'],
                   'content_sha256': item['sha256'], 'origin': item['origin'],
                   'derived_from_sha256': item['derived_from_sha256']}
        if item['id'] in redacted_ids:
            prior = next((row for row in store.rows() if row['id'] == rid), None)
            if prior:
                if prior['type'] != 'material' or prior['actor_id'] != item['actor_id'] or prior['payload'] != payload:
                    raise Problem('Previously committed attachment metadata changed', 409)
                existing += 1
            continue
        # Older databases can have NULL size with in-database bytes.
        total, chunks, _, _ = research.download_stream(item['id'])
        digest = hashlib.sha256()
        length = 0
        for chunk in chunks:
            length += len(chunk)
            digest.update(chunk)
        if length != total or digest.hexdigest() != item['sha256'] or (item['size'] is not None and length != item['size']):
            raise Problem('Attachment bytes or size do not match metadata', 409)
        payload['size'] = length
        write_guard = RequestLock(request_lock_db) if request_lock_db is not None else nullcontext()
        with write_guard, transaction(store.db):
            rows = store.rows()
            prior = next((row for row in rows if row['id'] == rid), None)
            if prior:
                if prior['type'] != 'material' or prior['actor_id'] != item['actor_id'] or prior['payload'] != payload:
                    raise Problem('Previously committed attachment metadata changed', 409)
                existing += 1
                continue
            row = record('material', item['actor_id'], payload)
            row['id'] = rid
            row['sha256'] = json_digest({k: v for k, v in row.items() if k != 'sha256'})
            combined = rows + [row]
            validate(combined)
            if len(canonical(combined).encode('utf-8')) > MAX_BUNDLE_BYTES - 1024:
                raise Problem('Prototype storage limit reached', 413)
            store.db.execute('INSERT INTO records(id,document) VALUES (?,?)', (rid, canonical(row)))
            record_chain.append(store.db, [row], backfilled=bool(backfilled))
            added += 1
    return {'added': added, 'already_committed': existing, 'backfilled': bool(backfilled),
            'external_time_verified': False, 'note': 'A later confirmed external anchor is required.'}


def reconcile(store, research, *, experimental=False):
    """Test-compatible entry point requiring explicit experimental=True flag.

    Each attachment is verified against the stored bytes, then atomically appended
    with its chain entry. The deterministic ID makes a retry after a crash safe.
    """
    if experimental is not True:
        raise Problem('Experimental local-test feature is disabled', 409)
    return commit_materials(store, research, backfilled=True)


def reconcile_instance(database: str | Path, *, offline: bool = False, backfilled: bool = True) -> dict:
    """Reconcile all research attachments of an instance into its record chain."""
    db_path = local_path(database)
    if not db_path.is_file():
        raise Problem('Instance database is missing')
    sidecar = Path(str(db_path) + '.research.sqlite')
    if not sidecar.is_file():
        return {'added': 0, 'already_committed': 0, 'backfilled': bool(backfilled),
                'external_time_verified': False, 'note': 'A later confirmed external anchor is required.'}
    from .research import Research
    with InstanceLock(db_path, exclusive=bool(offline)):
        store = Store(db_path)
        try:
            research = Research(sidecar, store.rows())
            try:
                return commit_materials(
                    store,
                    research,
                    backfilled=backfilled,
                    request_lock_db=None if offline else db_path,
                )
            finally:
                research.close()
        finally:
            store.close()


def materials_status(chain_db, rows: list[dict], revision_id: str) -> dict[str, dict]:
    """Return chain and Bitcoin anchor coverage for each committed attachment of revision_id."""
    result: dict[str, dict] = {}
    if chain_db is None:
        return result
    for row in rows:
        if row.get('type') != 'material':
            continue
        payload = row.get('payload') or {}
        if payload.get('revision_id') != revision_id:
            continue
        aid = payload.get('attachment_id')
        rid = row.get('id')
        if not aid or not rid:
            continue
        found = chain_db.execute(
            'SELECT seq, head, chained_at, backfilled FROM record_chain WHERE record_id = ?',
            (rid,),
        ).fetchone()
        if found is None:
            continue
        seq, head, chained_at, backfilled = found
        result[str(aid)] = {
            'record_id': rid,
            'record_sha256': row['sha256'],
            'content_sha256': payload.get('content_sha256'),
            'size': payload.get('size'),
            'origin': payload.get('origin'),
            'derived_from_sha256': payload.get('derived_from_sha256'),
            'seq': seq,
            'head': head,
            'chained_at': chained_at,
            'backfilled': bool(backfilled),
            'anchor': record_chain.anchor_for(chain_db, seq),
        }
    return result
