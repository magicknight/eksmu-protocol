"""Author publication snapshots and append-only notices in a private sidecar.

HTTP callers hold the instance request lock through authorization and writes.
Material capture additionally locks Research while reading the original bytes.
"""
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import sqlite3

from .blobstore import (
    DEFAULT_CHUNK_SIZE,
    MAX_STORAGE_SQLITE,
    create_blobstore,
    ensure_publication_blobs_schema,
    iter_bytes,
)
from .store import Problem, canonical, digest, exact, identifier, text
from .publication_contract import (SNAPSHOT, PACKAGE, EVENT, MATERIAL_FIELDS, MAX_FILES,
    optional_id, seal, sha, validate_snapshot, validate_event, verify_package, citation)

TABLES = {'publication_snapshots', 'publication_blobs', 'publication_events'}
MAX_SNAPSHOTS = 512
MAX_SNAPSHOTS_WORK = 64
MAX_EVENTS_WORK = 200
MAX_DOCUMENTS_BYTES = 96 * 1024 * 1024
MAX_BLOBS_BYTES = MAX_STORAGE_SQLITE


class Publications:
    def __init__(self, path, records, *, blobstore=None):
        self.index = {r['id']: r for r in records}
        self.db = sqlite3.connect(str(path), timeout=5, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if tables and (not TABLES.issubset(tables) or (tables - (TABLES | {'blobs'}))):
            self.db.close(); raise Problem('发表记录需要完整的独立数据库', 503)
        if not tables:
            self.db.executescript('''BEGIN IMMEDIATE;
              CREATE TABLE IF NOT EXISTS publication_snapshots(id TEXT PRIMARY KEY, exploration_id TEXT NOT NULL,
                revision_id TEXT NOT NULL, document TEXT NOT NULL, request_sha256 TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS publication_blobs(sha256 TEXT PRIMARY KEY, data BLOB, size INTEGER);
              CREATE TABLE IF NOT EXISTS publication_events(id TEXT PRIMARY KEY, exploration_id TEXT NOT NULL,
                document TEXT NOT NULL, request_sha256 TEXT NOT NULL);
              COMMIT;''')
        ensure_publication_blobs_schema(self.db)
        self.blobstore = create_blobstore(self.db, blobstore=blobstore)

    def close(self): self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback(); raise

    def record(self, rid, kind):
        identifier(rid)
        found = self.index.get(rid)
        if found is None or found['type'] != kind: raise Problem('找不到对应的发表版本或作者', 404)
        return found

    def owner(self, actor_id, revision_id):
        self.record(actor_id, 'actor'); rev = self.record(revision_id, 'revision')
        if rev['actor_id'] != actor_id: raise Problem('只有原发表账户能固定版本或追加作者声明', 403)
        return rev

    def publisher(self, revision):
        actor = self.record(revision['actor_id'], 'actor')
        return {'id': actor['id'], 'alias': actor['payload']['alias']}

    def _room(self, document):
        used = sum(self.db.execute('SELECT COALESCE(SUM(length(CAST(document AS BLOB))),0) FROM ' + table).fetchone()[0]
                   for table in ('publication_snapshots', 'publication_events'))
        if used + len(canonical(document).encode()) > MAX_DOCUMENTS_BYTES:
            raise Problem('固定版本/声明的本机配额已满，普通发表仍可继续', 413)

    def snapshot(self, snapshot_id):
        identifier(snapshot_id)
        row = self.db.execute('SELECT * FROM publication_snapshots WHERE id=?', (snapshot_id,)).fetchone()
        if row is None: raise Problem('固定版本不存在', 404)
        value = validate_snapshot(json.loads(row['document']))
        if value['id'] != snapshot_id or row['exploration_id'] != value['exploration']['id'] or row['revision_id'] != value['revision']['id']:
            raise Problem('Snapshot storage reference mismatch')
        return value

    def _replay(self, table, operation_id, request):
        row = self.db.execute('SELECT document,request_sha256 FROM ' + table + ' WHERE id=?', (operation_id,)).fetchone()
        if row is None: return None
        if row['request_sha256'] != request: raise Problem('操作标识已用于不同内容，请刷新后重新确认', 409)
        return json.loads(row['document'])

    def create_snapshot(self, research, actor_id, revision_id, attachment_ids, expected_previous_id, operation_id):
        rev = self.owner(actor_id, revision_id); identifier(operation_id); optional_id(expected_previous_id)
        if not isinstance(attachment_ids, list) or len(attachment_ids) > MAX_FILES:
            raise Problem('固定版本最多包含16份材料')
        for fid in attachment_ids: identifier(fid)
        if len(set(attachment_ids)) != len(attachment_ids): raise Problem('材料标识不能重复')
        ids = sorted(attachment_ids); wid = rev['payload']['exploration_id']
        request = digest({'actor_id': actor_id, 'revision_id': revision_id, 'attachment_ids': ids,
                          'expected_previous_id': expected_previous_id})
        with self.transaction():
            if self._replay('publication_snapshots', operation_id, request) is not None:
                return self.snapshot(operation_id)
            rows = self.db.execute('SELECT id FROM publication_snapshots WHERE exploration_id=? ORDER BY rowid', (wid,)).fetchall()
            if expected_previous_id != (rows[-1]['id'] if rows else None):
                raise Problem('已有新的固定版本，请刷新后重新选择材料', 409)
            if len(rows) >= MAX_SNAPSHOTS_WORK or self.db.execute('SELECT COUNT(*) FROM publication_snapshots').fetchone()[0] >= MAX_SNAPSHOTS:
                raise Problem('本机固定版本名额已满，普通发表仍可继续', 413)
            with research.transaction():
                available = {m['id']: m for m in research.files(revision_id)['items']}
                if any(fid not in available for fid in ids): raise Problem('所选材料不属于此正文版本', 400)
                materials = [{k: available[fid][k] for k in MATERIAL_FIELDS} for fid in ids]
                snap = seal({'format': SNAPSHOT, 'id': operation_id, 'created_at': datetime.now(timezone.utc).isoformat(),
                    'previous_snapshot_id': expected_previous_id, 'publisher': self.publisher(rev),
                    'exploration': self.record(wid, 'exploration'), 'revision': rev, 'materials': materials})
                validate_snapshot(snap); self._room(snap)
                used = self.db.execute('SELECT COALESCE(SUM(COALESCE(size, length(data), 0)),0) FROM publication_blobs').fetchone()[0]
                for item in materials:
                    try:
                        raw, _, _ = research.download(item['id'])
                    except Problem as exc:
                        raise Problem('源材料字节与摘要不一致，未创建固定版本') from exc
                    if sha(raw) != item['sha256'] or len(raw) != item['size']:
                        raise Problem('源材料字节与摘要不一致，未创建固定版本')
                    prior = self.db.execute('SELECT data, size FROM publication_blobs WHERE sha256=?', (item['sha256'],)).fetchone()
                    if prior is not None:
                        if prior['data'] is not None:
                            prior_raw = bytes(prior['data'])
                        else:
                            try:
                                prior_raw = self.blobstore.get(item['sha256'])
                            except Problem as exc:
                                raise Problem('已有固定版本材料损坏，未覆盖') from exc
                        if prior_raw != raw:
                            raise Problem('已有固定版本材料损坏，未覆盖')
                    else:
                        used += len(raw)
                        if used > MAX_BLOBS_BYTES:
                            raise Problem('固定版本材料存储配额已满，普通发表仍可继续', 413)
                        self.blobstore.put(raw)
                        self.db.execute('INSERT INTO publication_blobs(sha256, data, size) VALUES(?,?,?)', (item['sha256'], None, len(raw)))
                self.db.execute('INSERT INTO publication_snapshots VALUES(?,?,?,?,?)',
                                (operation_id, wid, revision_id, canonical(snap), request))
                return snap

    def file_stream(self, snapshot_id, attachment_id, chunk_size=DEFAULT_CHUNK_SIZE):
        snap = self.snapshot(snapshot_id); identifier(attachment_id)
        item = next((m for m in snap['materials'] if m['id'] == attachment_id), None)
        if item is None: raise Problem('此固定版本没有包含该材料', 404)
        row = self.db.execute('SELECT data, size FROM publication_blobs WHERE sha256=?', (item['sha256'],)).fetchone()
        if row is None:
            raise Problem('固定版本材料缺失或摘要不一致', 409)
        if row['data'] is not None:
            raw = bytes(row['data'])
            if sha(raw) != item['sha256'] or len(raw) != item['size']:
                raise Problem('固定版本材料缺失或摘要不一致', 409)
            return len(raw), iter_bytes(raw, chunk_size), 'application/octet-stream', item['filename']
        try:
            total_size, chunks = self.blobstore.open_stream(item['sha256'], chunk_size=chunk_size)
        except Problem as exc:
            raise Problem('固定版本材料缺失或摘要不一致', 409) from exc
        if total_size != item['size']:
            raise Problem('固定版本材料缺失或摘要不一致', 409)
        return total_size, chunks, 'application/octet-stream', item['filename']

    def file(self, snapshot_id, attachment_id):
        _, chunks, media, filename = self.file_stream(snapshot_id, attachment_id)
        return b''.join(chunks), media, filename

    def export(self, snapshot_id):
        snap = self.snapshot(snapshot_id); blobs = {}
        for item in snap['materials']:
            if item['sha256'] not in blobs:
                raw, _, _ = self.file(snapshot_id, item['id'])
                blobs[item['sha256']] = {'sha256': item['sha256'], 'data_b64': base64.b64encode(raw).decode()}
        package = {'format': PACKAGE, 'snapshot': snap, 'blobs': [blobs[key] for key in sorted(blobs)]}
        verify_package(package)
        return package

    def status(self, exploration_id):
        work = self.record(exploration_id, 'exploration')
        events = []; previous = None; withdrawn = False
        for row in self.db.execute('SELECT id,document FROM publication_events WHERE exploration_id=? ORDER BY rowid', (exploration_id,)):
            event = json.loads(row['document']); validate_event(event)
            rev = self.record(event['revision_id'], 'revision')
            if (event['id'] != row['id'] or event['exploration_id'] != exploration_id or event['actor_id'] != work['actor_id']
                    or rev['payload']['exploration_id'] != exploration_id or event['previous_event_id'] != previous):
                raise Problem('Publication event history mismatch')
            if event['kind'] == 'withdraw':
                if withdrawn: raise Problem('Invalid repeated withdrawal')
                withdrawn = True
            elif event['kind'] == 'reinstate':
                if not withdrawn: raise Problem('Invalid reinstatement')
                withdrawn = False
            previous = event['id']; events.append(event)
        return {'exploration_id': exploration_id, 'withdrawn': withdrawn, 'events': events, 'head': previous}

    def event(self, actor_id, revision_id, kind, body, expected_previous_id, operation_id):
        rev = self.owner(actor_id, revision_id); identifier(operation_id); optional_id(expected_previous_id)
        wid = rev['payload']['exploration_id']
        event = seal({'format': EVENT, 'id': operation_id, 'exploration_id': wid, 'revision_id': revision_id,
                      'actor_id': actor_id, 'previous_event_id': expected_previous_id,
                      'created_at': datetime.now(timezone.utc).isoformat(), 'kind': kind, 'body': body})
        validate_event(event)
        request = digest({k: v for k, v in event.items() if k not in {'created_at', 'sha256'}})
        with self.transaction():
            replay = self._replay('publication_events', operation_id, request)
            if replay is not None:
                validate_event(replay); return replay
            status = self.status(wid)
            if status['head'] != expected_previous_id: raise Problem('作者声明已有更新，请刷新后重试', 409)
            if (kind == 'withdraw' and status['withdrawn']) or (kind == 'reinstate' and not status['withdrawn']):
                raise Problem('撤回状态已改变，请刷新后选择相应操作', 409)
            if len(status['events']) >= MAX_EVENTS_WORK: raise Problem('此作品的作者声明配额已满', 413)
            self._room(event)
            self.db.execute('INSERT INTO publication_events VALUES(?,?,?,?)', (operation_id, wid, canonical(event), request))
            return event

    def workspace(self, revision_id):
        rev = self.record(revision_id, 'revision'); wid = rev['payload']['exploration_id']
        snapshots = []
        for row in self.db.execute('SELECT id FROM publication_snapshots WHERE exploration_id=? ORDER BY rowid', (wid,)):
            snap = self.snapshot(row['id'])
            snapshots.append({'id': snap['id'], 'revision_id': snap['revision']['id'], 'created_at': snap['created_at'],
                              'sha256': snap['sha256'], 'materials': len(snap['materials'])})
        return {'revision_id': revision_id, 'snapshots': snapshots, 'status': self.status(wid),
                'snapshot_head': snapshots[-1]['id'] if snapshots else None,
                'citation': citation(rev, self.publisher(rev))}
