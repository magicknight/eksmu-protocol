"""Content-addressed blob storage backends (SQLite and Google Cloud Storage JSON API)."""
from __future__ import annotations

import base64
from collections.abc import Iterator
from contextlib import closing
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import time
import urllib.error
from urllib.parse import quote
import urllib.request

from .maintenance import InstanceLock, local_path
from .store import Problem

MAX_FILE = 32 * 1024 * 1024
MAX_PER_REVISION = 16
MAX_AUTHOR_STORAGE = 256 * 1024 * 1024
MAX_STORAGE_SQLITE = 2 * 1024 * 1024 * 1024
MAX_STORAGE_GCS = 200 * 1024 * 1024 * 1024
UPLOAD_REQUEST_LIMIT = 46 * 1024 * 1024
IMPORT_MATERIALS_LIMIT = 128 * 1024 * 1024
MAX_UPLOAD_REQUEST = UPLOAD_REQUEST_LIMIT
MAX_IMPORT_MATERIALS_REQUEST = IMPORT_MATERIALS_LIMIT
MAX_CONCURRENT_LARGE_REQUESTS = 2
DEFAULT_CHUNK_SIZE = 64 * 1024

METADATA_TOKEN_URL = 'http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token'
GCS_API_ENDPOINT = 'https://storage.googleapis.com'
_RETRYABLE_HTTP_CODES = {408, 429, 500, 502, 503, 504}


def _make_crc32c_table() -> tuple[int, ...]:
    table = []
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = (crc >> 1) ^ 0x82F63B78 if (crc & 1) else (crc >> 1)
        table.append(crc)
    return tuple(table)


_CRC32C_TABLE = _make_crc32c_table()


def crc32c_b64(data: bytes) -> str:
    crc = 0xFFFFFFFF
    table = _CRC32C_TABLE
    for b in data:
        crc = table[(crc ^ b) & 0xFF] ^ (crc >> 8)
    crc ^= 0xFFFFFFFF
    return base64.b64encode(crc.to_bytes(4, 'big')).decode('ascii')


def md5_b64(data: bytes) -> str:
    return base64.b64encode(hashlib.md5(data).digest()).decode('ascii')


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_sha256(digest: str) -> str:
    if not isinstance(digest, str) or re.fullmatch(r'[0-9a-f]{64}', digest) is None:
        raise Problem('Invalid SHA-256')
    return digest


def iter_bytes(raw: bytes, chunk_size: int = DEFAULT_CHUNK_SIZE) -> Iterator[bytes]:
    view = memoryview(raw)
    for offset in range(0, len(view), chunk_size):
        yield bytes(view[offset:offset + chunk_size])


def is_gcs_configured() -> bool:
    return bool(os.environ.get('EKSMU_BLOB_BUCKET', '').strip())


class SqliteBlobStore:
    """Default local content-addressed store backed by a `blobs` table in SQLite."""

    kind = 'sqlite'

    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.db.execute(
            'CREATE TABLE IF NOT EXISTS blobs(sha256 TEXT PRIMARY KEY, data BLOB NOT NULL)'
        )

    def exists(self, digest: str) -> bool:
        validate_sha256(digest)
        row = self.db.execute('SELECT 1 FROM blobs WHERE sha256=?', (digest,)).fetchone()
        return row is not None

    def put(self, data: bytes) -> str:
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise Problem('文件编码不正确')
        raw = bytes(data)
        digest = sha256_hex(raw)
        row = self.db.execute('SELECT data FROM blobs WHERE sha256=?', (digest,)).fetchone()
        if row is not None:
            existing = bytes(row[0])
            if sha256_hex(existing) != digest:
                raise Problem('附件内容校验失败，请联系维护者', 503)
            return digest
        self.db.execute('INSERT INTO blobs(sha256, data) VALUES(?, ?)', (digest, raw))
        return digest

    def open_stream(self, digest: str, chunk_size: int = DEFAULT_CHUNK_SIZE) -> tuple[int, Iterator[bytes]]:
        validate_sha256(digest)
        row = self.db.execute('SELECT rowid, length(data) FROM blobs WHERE sha256=?', (digest,)).fetchone()
        if row is None:
            raise Problem('附件不存在', 404)
        rowid, total_size = int(row[0]), int(row[1])
        if hasattr(self.db, 'blobopen'):
            hasher = hashlib.sha256()
            with closing(self.db.blobopen('blobs', 'data', rowid, readonly=True)) as blob:
                offset = 0
                while offset < total_size:
                    blob.seek(offset)
                    chunk = blob.read(min(chunk_size, total_size - offset))
                    if not chunk:
                        break
                    hasher.update(chunk)
                    offset += len(chunk)
            if hasher.hexdigest() != digest:
                raise Problem('附件内容校验失败，请联系维护者', 503)

            def _gen() -> Iterator[bytes]:
                with closing(self.db.blobopen('blobs', 'data', rowid, readonly=True)) as blob:
                    pos = 0
                    while pos < total_size:
                        blob.seek(pos)
                        part = blob.read(min(chunk_size, total_size - pos))
                        if not part:
                            break
                        pos += len(part)
                        yield part

            return total_size, _gen()

        raw = self.get(digest)
        return len(raw), iter_bytes(raw, chunk_size)

    def stream(self, digest: str, chunk_size: int = DEFAULT_CHUNK_SIZE) -> Iterator[bytes]:
        _, chunks = self.open_stream(digest, chunk_size=chunk_size)
        return chunks

    def get(self, digest: str) -> bytes:
        validate_sha256(digest)
        row = self.db.execute('SELECT data FROM blobs WHERE sha256=?', (digest,)).fetchone()
        if row is None:
            raise Problem('附件不存在', 404)
        raw = bytes(row[0])
        if sha256_hex(raw) != digest:
            raise Problem('附件内容校验失败，请联系维护者', 503)
        return raw

    def delete(self, digest: str) -> None:
        validate_sha256(digest)
        self.db.execute('DELETE FROM blobs WHERE sha256=?', (digest,))


_TOKEN_CACHE_LOCK = threading.Lock()
_TOKEN_CACHE: dict[tuple[str, int], tuple[str, float]] = {}


def clear_token_cache() -> None:
    with _TOKEN_CACHE_LOCK:
        _TOKEN_CACHE.clear()


class GcsBlobStore:
    """Content-addressed store using Google Cloud Storage JSON API and GCE metadata tokens."""

    kind = 'gcs'

    def __init__(
        self,
        bucket: str,
        prefix: str = '',
        *,
        api_endpoint: str = GCS_API_ENDPOINT,
        token_url: str = METADATA_TOKEN_URL,
        urlopen=None,
        max_retries: int = 3,
        retry_delay: float = 0.05,
        timeout: float = 30.0,
    ):
        bucket = (bucket or '').strip()
        if not bucket or any(c in bucket for c in '/\\?#'):
            raise ValueError('A valid GCS bucket name is required')
        self.bucket = bucket
        self.prefix = (prefix or '').strip().strip('/')
        self.api_endpoint = (api_endpoint or GCS_API_ENDPOINT).rstrip('/')
        self.token_url = token_url or METADATA_TOKEN_URL
        self._urlopen = urlopen or urllib.request.urlopen
        self.max_retries = max(1, int(max_retries))
        self.retry_delay = max(0.0, float(retry_delay))
        self.timeout = float(timeout)
        self._lock = threading.Lock()
        self._cached_token: str | None = None
        self._token_expires_at: float = 0.0

    def object_name(self, digest: str) -> str:
        validate_sha256(digest)
        rel = f'sha256/{digest[:2]}/{digest}'
        return f'{self.prefix}/{rel}' if self.prefix else rel

    def _get_token(self, *, force_refresh: bool = False) -> str:
        cache_key = (self.token_url, id(self._urlopen))
        now_mono = time.monotonic()
        with self._lock:
            if not force_refresh and self._cached_token and now_mono < self._token_expires_at:
                return self._cached_token
        with _TOKEN_CACHE_LOCK:
            cached = _TOKEN_CACHE.get(cache_key)
            if not force_refresh and cached and now_mono < cached[1]:
                with self._lock:
                    self._cached_token, self._token_expires_at = cached
                return cached[0]

        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            req = urllib.request.Request(
                self.token_url,
                headers={'Metadata-Flavor': 'Google'},
                method='GET',
            )
            try:
                with self._urlopen(req, timeout=self.timeout) as resp:
                    status = getattr(resp, 'status', 200)
                    body = resp.read()
                if status != 200:
                    raise urllib.error.HTTPError(self.token_url, status, 'token error', {}, None)
                doc = json.loads(body.decode('utf-8'))
                token = doc.get('access_token')
                if not isinstance(token, str) or not token.strip():
                    raise ValueError('Missing access_token in metadata response')
                expires_in = float(doc.get('expires_in', 3600))
                margin = min(60.0, max(1.0, expires_in * 0.2)) if expires_in > 2.0 else 0.0
                expires_at = time.monotonic() + max(0.0, expires_in - margin)
                with self._lock:
                    self._cached_token = token
                    self._token_expires_at = expires_at
                with _TOKEN_CACHE_LOCK:
                    _TOKEN_CACHE[cache_key] = (token, expires_at)
                return token
            except urllib.error.HTTPError as exc:
                exc.close()
                last_exc = exc
                if exc.code not in _RETRYABLE_HTTP_CODES or attempt + 1 >= self.max_retries:
                    break
            except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException, OSError, ValueError) as exc:
                last_exc = exc
                if isinstance(exc, ValueError) or attempt + 1 >= self.max_retries:
                    break
            if self.retry_delay > 0:
                time.sleep(self.retry_delay * (attempt + 1))
        raise Problem('无法获取对象存储访问令牌；文字发表不受影响', 503) from last_exc

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        data: bytes | None = None,
        content_type: str | None = None,
        allow_404: bool = False,
    ) -> dict | None:
        last_exc: Exception | None = None
        refreshed = False
        for attempt in range(self.max_retries):
            token = self._get_token(force_refresh=refreshed)
            headers = {'Authorization': f'Bearer {token}'}
            if content_type is not None:
                headers['Content-Type'] = content_type
            if data is not None:
                headers['Content-Length'] = str(len(data))
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with self._urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    if not raw:
                        return {}
                    return json.loads(raw.decode('utf-8'))
            except urllib.error.HTTPError as exc:
                exc.close()
                last_exc = exc
                if exc.code == 404 and allow_404:
                    return None
                if exc.code == 401 and not refreshed:
                    refreshed = True
                    continue
                if exc.code not in _RETRYABLE_HTTP_CODES or attempt + 1 >= self.max_retries:
                    break
            except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException, OSError, ValueError) as exc:
                last_exc = exc
                if attempt + 1 >= self.max_retries:
                    break
            if self.retry_delay > 0:
                time.sleep(self.retry_delay * (attempt + 1))
        raise Problem('对象存储服务暂不可用，请稍后重试；文字发表不受影响', 503) from last_exc

    def _metadata_url(self, digest: str) -> str:
        name = quote(self.object_name(digest), safe='')
        bucket = quote(self.bucket, safe='')
        return f'{self.api_endpoint}/storage/v1/b/{bucket}/o/{name}'

    def _upload_url(self, digest: str) -> str:
        name = quote(self.object_name(digest), safe='')
        bucket = quote(self.bucket, safe='')
        return f'{self.api_endpoint}/upload/storage/v1/b/{bucket}/o?uploadType=media&name={name}'

    def exists(self, digest: str) -> bool:
        validate_sha256(digest)
        meta = self._request_json('GET', self._metadata_url(digest), allow_404=True)
        return meta is not None

    def _verify_upload_metadata(self, meta: dict, raw: bytes, digest: str) -> bool:
        if not isinstance(meta, dict):
            return False
        if 'size' in meta:
            try:
                if int(meta['size']) != len(raw):
                    return False
            except (TypeError, ValueError):
                return False
        checked_hash = False
        if 'md5Hash' in meta and meta['md5Hash'] is not None:
            if meta['md5Hash'] != md5_b64(raw):
                return False
            checked_hash = True
        # The pure-Python CRC32C is slow on large files, so it is only the fallback when GCS sent no MD5.
        if not checked_hash and meta.get('crc32c') is not None:
            if meta['crc32c'] != crc32c_b64(raw):
                return False
            checked_hash = True
        if not checked_hash:
            # Fallback: re-download and verify SHA-256 if response omitted md5Hash/crc32c.
            try:
                downloaded = self.get(digest)
            except Problem:
                return False
            if downloaded != raw:
                return False
        return True

    def _best_effort_delete(self, digest: str) -> None:
        try:
            token = self._get_token()
            req = urllib.request.Request(
                self._metadata_url(digest),
                headers={'Authorization': f'Bearer {token}'},
                method='DELETE',
            )
            with self._urlopen(req, timeout=self.timeout) as resp:
                resp.read()
        except Exception:
            pass

    def put(self, data: bytes) -> str:
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise Problem('文件编码不正确')
        raw = bytes(data)
        digest = sha256_hex(raw)
        existing_meta = self._request_json('GET', self._metadata_url(digest), allow_404=True)
        if existing_meta is not None:
            if not self._verify_upload_metadata(existing_meta, raw, digest):
                raise Problem('对象存储上传校验失败；文字发表不受影响', 503)
            return digest
        meta = self._request_json(
            'POST',
            self._upload_url(digest),
            data=raw,
            content_type='application/octet-stream',
        )
        if not self._verify_upload_metadata(meta, raw, digest):
            self._best_effort_delete(digest)
            raise Problem('对象存储上传校验失败；文字发表不受影响', 503)
        return digest

    def open_stream(self, digest: str, chunk_size: int = DEFAULT_CHUNK_SIZE) -> tuple[int, Iterator[bytes]]:
        validate_sha256(digest)
        url = self._metadata_url(digest) + '?alt=media'
        last_exc: Exception | None = None
        refreshed = False
        for attempt in range(self.max_retries):
            token = self._get_token(force_refresh=refreshed)
            req = urllib.request.Request(
                url,
                headers={'Authorization': f'Bearer {token}'},
                method='GET',
            )
            spooled = tempfile.SpooledTemporaryFile(max_size=2 * 1024 * 1024)
            try:
                with self._urlopen(req, timeout=self.timeout) as resp:
                    hasher = hashlib.sha256()
                    total_size = 0
                    while True:
                        chunk = resp.read(chunk_size)
                        if not chunk:
                            break
                        hasher.update(chunk)
                        spooled.write(chunk)
                        total_size += len(chunk)
                if hasher.hexdigest() != digest:
                    spooled.close()
                    raise Problem('附件内容校验失败，请联系维护者', 503)
                spooled.seek(0)

                def _gen(temp_file=spooled) -> Iterator[bytes]:
                    try:
                        while True:
                            part = temp_file.read(chunk_size)
                            if not part:
                                break
                            yield part
                    finally:
                        temp_file.close()

                return total_size, _gen()
            except Problem:
                spooled.close()
                raise
            except urllib.error.HTTPError as exc:
                exc.close()
                spooled.close()
                last_exc = exc
                if exc.code == 404:
                    raise Problem('附件不存在', 404) from exc
                if exc.code == 401 and not refreshed:
                    refreshed = True
                    continue
                if exc.code not in _RETRYABLE_HTTP_CODES or attempt + 1 >= self.max_retries:
                    break
            except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException, OSError) as exc:
                spooled.close()
                last_exc = exc
                if attempt + 1 >= self.max_retries:
                    break
            if self.retry_delay > 0:
                time.sleep(self.retry_delay * (attempt + 1))
        raise Problem('对象存储服务暂不可用，请稍后重试；文字发表不受影响', 503) from last_exc

    def stream(self, digest: str, chunk_size: int = DEFAULT_CHUNK_SIZE) -> Iterator[bytes]:
        _, chunks = self.open_stream(digest, chunk_size=chunk_size)
        return chunks

    def get(self, digest: str) -> bytes:
        _, chunks = self.open_stream(digest)
        return b''.join(chunks)

    def delete(self, digest: str) -> None:
        validate_sha256(digest)
        self._best_effort_delete(digest)


def create_blobstore(db: sqlite3.Connection | None = None, *, blobstore=None):
    if blobstore is not None:
        return blobstore
    bucket = os.environ.get('EKSMU_BLOB_BUCKET', '').strip()
    if bucket:
        prefix = os.environ.get('EKSMU_BLOB_PREFIX', '').strip()
        api_endpoint = os.environ.get('EKSMU_GCS_API_ENDPOINT', GCS_API_ENDPOINT).strip() or GCS_API_ENDPOINT
        token_url = os.environ.get('EKSMU_GCS_TOKEN_URL', METADATA_TOKEN_URL).strip() or METADATA_TOKEN_URL
        return GcsBlobStore(bucket=bucket, prefix=prefix, api_endpoint=api_endpoint, token_url=token_url)
    if db is None:
        raise ValueError('SQLite blobstore requires a database connection')
    return SqliteBlobStore(db)


def ensure_research_files_schema(db: sqlite3.Connection) -> None:
    cols = {r[1]: r for r in db.execute('PRAGMA table_info(research_files)')}
    if not cols:
        return
    if cols['data'][3] != 0 or 'size' not in cols or 'preview_sha256' not in cols:
        size_expr = 'COALESCE(size, length(data))' if 'size' in cols else 'length(data)'
        prev_sha_expr = 'preview_sha256' if 'preview_sha256' in cols else 'NULL'
        origin_expr = "COALESCE(origin, 'upload')" if 'origin' in cols else "'upload'"
        derived_expr = 'derived_from_sha256' if 'derived_from_sha256' in cols else 'NULL'
        db.executescript(f'''
          BEGIN IMMEDIATE;
          CREATE TABLE research_files_new(
            id TEXT PRIMARY KEY, revision_id TEXT NOT NULL, actor_id TEXT NOT NULL,
            filename TEXT NOT NULL, mime TEXT NOT NULL, sha256 TEXT NOT NULL, created_at TEXT NOT NULL,
            data BLOB, preview BLOB, size INTEGER, preview_sha256 TEXT,
            origin TEXT NOT NULL DEFAULT 'upload', derived_from_sha256 TEXT,
            UNIQUE(revision_id,filename,sha256));
          INSERT INTO research_files_new(rowid, id, revision_id, actor_id, filename, mime, sha256, created_at, data, preview, size, preview_sha256, origin, derived_from_sha256)
            SELECT rowid, id, revision_id, actor_id, filename, mime, sha256, created_at, data, preview, {size_expr}, {prev_sha_expr}, {origin_expr}, {derived_expr}
            FROM research_files ORDER BY rowid;
          DROP TABLE research_files;
          ALTER TABLE research_files_new RENAME TO research_files;
          COMMIT;
        ''')
        cols = {r[1]: r for r in db.execute('PRAGMA table_info(research_files)')}
    if 'origin' not in cols:
        db.execute("ALTER TABLE research_files ADD COLUMN origin TEXT NOT NULL DEFAULT 'upload'")
    if 'derived_from_sha256' not in cols:
        db.execute('ALTER TABLE research_files ADD COLUMN derived_from_sha256 TEXT')


def ensure_publication_blobs_schema(db: sqlite3.Connection) -> None:
    cols = {r[1]: r for r in db.execute('PRAGMA table_info(publication_blobs)')}
    if not cols:
        return
    if cols['data'][3] != 0 or 'size' not in cols:
        size_expr = 'COALESCE(size, length(data))' if 'size' in cols else 'length(data)'
        db.executescript(f'''
          BEGIN IMMEDIATE;
          CREATE TABLE publication_blobs_new(sha256 TEXT PRIMARY KEY, data BLOB, size INTEGER);
          INSERT INTO publication_blobs_new(rowid, sha256, data, size)
            SELECT rowid, sha256, data, {size_expr} FROM publication_blobs ORDER BY rowid;
          DROP TABLE publication_blobs;
          ALTER TABLE publication_blobs_new RENAME TO publication_blobs;
          COMMIT;
        ''')


def migrate_blobs(database: str | Path, *, blobstore=None, max_rows: int | None = None) -> dict:
    """Offline migration of inline SQLite BLOBs in research_files and publication_blobs to the current backend."""
    db_path = local_path(database)
    with InstanceLock(db_path, exclusive=True):
        migrated_research = 0
        migrated_publications = 0
        remaining_research = 0
        remaining_publications = 0
        processed = 0
        backend_kind = getattr(blobstore, 'kind', 'gcs' if is_gcs_configured() else 'sqlite')

        research_path = Path(str(db_path) + '.research.sqlite')
        if not research_path.is_file() and db_path.is_file():
            with closing(sqlite3.connect(str(db_path))) as probe:
                tables = {r[0] for r in probe.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if 'research_files' in tables:
                    research_path = db_path

        publications_path = Path(str(db_path) + '.publications.sqlite')
        if not publications_path.is_file() and db_path.is_file():
            with closing(sqlite3.connect(str(db_path))) as probe:
                tables = {r[0] for r in probe.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if 'publication_blobs' in tables:
                    publications_path = db_path

        if research_path.is_file():
            with closing(sqlite3.connect(str(research_path), timeout=5, isolation_level=None)) as rdb:
                rdb.row_factory = sqlite3.Row
                ensure_research_files_schema(rdb)
                store = create_blobstore(rdb, blobstore=blobstore)
                backend_kind = store.kind
                rows = rdb.execute(
                    'SELECT id, sha256, data, preview, size, preview_sha256 FROM research_files '
                    'WHERE data IS NOT NULL OR preview IS NOT NULL ORDER BY rowid'
                ).fetchall()
                for row in rows:
                    if max_rows is not None and processed >= max_rows:
                        break
                    file_size = row['size']
                    if row['data'] is not None:
                        raw = bytes(row['data'])
                        if sha256_hex(raw) != row['sha256']:
                            raise ValueError(f"Corrupt inline research_files BLOB for id={row['id']}")
                        put_sha = store.put(raw)
                        if put_sha != row['sha256'] or store.get(row['sha256']) != raw:
                            raise ValueError(f"Verification failed after migrating research_files id={row['id']}")
                        file_size = len(raw)
                    else:
                        if not store.exists(row['sha256']):
                            raise ValueError(f"Missing target blob for research_files id={row['id']}")
                    prev_sha = row['preview_sha256']
                    if row['preview'] is not None:
                        prev_raw = bytes(row['preview'])
                        prev_sha = store.put(prev_raw)
                        if store.get(prev_sha) != prev_raw:
                            raise ValueError(f"Preview verification failed for research_files id={row['id']}")
                    rdb.execute('BEGIN IMMEDIATE')
                    try:
                        rdb.execute(
                            'UPDATE research_files SET data=NULL, preview=NULL, size=?, preview_sha256=? WHERE id=?',
                            (file_size, prev_sha, row['id']),
                        )
                        rdb.commit()
                    except BaseException:
                        rdb.rollback()
                        raise
                    migrated_research += 1
                    processed += 1
                if store.kind != 'sqlite':
                    has_blobs_table = rdb.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='blobs'"
                    ).fetchone()
                    if has_blobs_table:
                        blob_rows = rdb.execute('SELECT sha256, data FROM blobs ORDER BY rowid').fetchall()
                        for brow in blob_rows:
                            if max_rows is not None and processed >= max_rows:
                                break
                            raw = bytes(brow['data'])
                            if sha256_hex(raw) != brow['sha256']:
                                raise ValueError(f"Corrupt sqlite blob for sha256={brow['sha256']}")
                            put_sha = store.put(raw)
                            if put_sha != brow['sha256'] or store.get(brow['sha256']) != raw:
                                raise ValueError(f"Verification failed after migrating blob sha256={brow['sha256']}")
                            rdb.execute('BEGIN IMMEDIATE')
                            try:
                                rdb.execute('DELETE FROM blobs WHERE sha256=?', (brow['sha256'],))
                                rdb.commit()
                            except BaseException:
                                rdb.rollback()
                                raise
                            migrated_research += 1
                            processed += 1
                remaining_research = rdb.execute(
                    'SELECT COUNT(*) FROM research_files WHERE data IS NOT NULL OR preview IS NOT NULL'
                ).fetchone()[0]
                if store.kind != 'sqlite' and rdb.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='blobs'"
                ).fetchone():
                    remaining_research += rdb.execute('SELECT COUNT(*) FROM blobs').fetchone()[0]

        if publications_path.is_file():
            with closing(sqlite3.connect(str(publications_path), timeout=5, isolation_level=None)) as pdb:
                pdb.row_factory = sqlite3.Row
                ensure_publication_blobs_schema(pdb)
                store = create_blobstore(pdb, blobstore=blobstore)
                backend_kind = store.kind
                rows = pdb.execute(
                    'SELECT sha256, data, size FROM publication_blobs WHERE data IS NOT NULL ORDER BY rowid'
                ).fetchall()
                for row in rows:
                    if max_rows is not None and processed >= max_rows:
                        break
                    raw = bytes(row['data'])
                    if sha256_hex(raw) != row['sha256']:
                        raise ValueError(f"Corrupt inline publication_blobs BLOB for sha256={row['sha256']}")
                    put_sha = store.put(raw)
                    if put_sha != row['sha256'] or store.get(row['sha256']) != raw:
                        raise ValueError(f"Verification failed after migrating publication_blobs sha256={row['sha256']}")
                    pdb.execute('BEGIN IMMEDIATE')
                    try:
                        pdb.execute(
                            'UPDATE publication_blobs SET data=NULL, size=? WHERE sha256=?',
                            (len(raw), row['sha256']),
                        )
                        pdb.commit()
                    except BaseException:
                        pdb.rollback()
                        raise
                    migrated_publications += 1
                    processed += 1
                if store.kind != 'sqlite':
                    has_blobs_table = pdb.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='blobs'"
                    ).fetchone()
                    if has_blobs_table:
                        blob_rows = pdb.execute('SELECT sha256, data FROM blobs ORDER BY rowid').fetchall()
                        for brow in blob_rows:
                            if max_rows is not None and processed >= max_rows:
                                break
                            raw = bytes(brow['data'])
                            if sha256_hex(raw) != brow['sha256']:
                                raise ValueError(f"Corrupt sqlite blob for sha256={brow['sha256']}")
                            put_sha = store.put(raw)
                            if put_sha != brow['sha256'] or store.get(brow['sha256']) != raw:
                                raise ValueError(f"Verification failed after migrating blob sha256={brow['sha256']}")
                            pdb.execute('BEGIN IMMEDIATE')
                            try:
                                pdb.execute('DELETE FROM blobs WHERE sha256=?', (brow['sha256'],))
                                pdb.commit()
                            except BaseException:
                                pdb.rollback()
                                raise
                            migrated_publications += 1
                            processed += 1
                remaining_publications = pdb.execute(
                    'SELECT COUNT(*) FROM publication_blobs WHERE data IS NOT NULL'
                ).fetchone()[0]
                if store.kind != 'sqlite' and pdb.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='blobs'"
                ).fetchone():
                    remaining_publications += pdb.execute('SELECT COUNT(*) FROM blobs').fetchone()[0]

        return {
            'backend': backend_kind,
            'migrated_research_files': migrated_research,
            'migrated_publication_blobs': migrated_publications,
            'remaining_research_blobs': remaining_research,
            'remaining_publication_blobs': remaining_publications,
            'complete': (remaining_research + remaining_publications) == 0,
        }
