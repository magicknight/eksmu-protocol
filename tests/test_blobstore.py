"""Tests for SQLite/GCS content-addressed blobstore, quotas, migration, backup, and concurrency limits."""
from __future__ import annotations

import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlsplit
import urllib.error
import urllib.request
import uuid

from PIL import Image

from eksmu import blobstore as bs
from eksmu.backup import create_backup, restore_backup, verify_backup
from eksmu.publication_contract import verify_package
from eksmu.publications import Publications
from eksmu.research import Research
from eksmu.server import LARGE_REQUEST_SEMAPHORE, make_server
from eksmu.store import Problem, Store


class FakeGcsServer:
    """Local HTTP server simulating GCE metadata token endpoint and GCS JSON API."""

    def __init__(self):
        self.lock = threading.Lock()
        self.objects: dict[tuple[str, str], bytes] = {}
        self.token = 'fake-gcs-token-1'
        self.expires_in = 3600
        self.token_requests = 0
        self.upload_requests = 0
        self.download_requests = 0
        self.metadata_requests = 0
        self.delete_requests = 0
        self.fail_next_uploads = 0
        self.fail_next_downloads = 0
        self.fail_next_tokens = 0
        self.corrupt_upload_hash = False
        self.corrupt_download_bytes = False
        self.omit_upload_hashes = False

        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def _send_json(self, status: int, doc: dict):
                raw = json.dumps(doc).encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _check_auth(self) -> bool:
                auth = self.headers.get('Authorization', '')
                with outer.lock:
                    expected = f'Bearer {outer.token}'
                if auth != expected:
                    self._send_json(401, {'error': 'unauthorized'})
                    return False
                return True

            def do_GET(self):
                parsed = urlsplit(self.path)
                if parsed.path == '/computeMetadata/v1/instance/service-accounts/default/token':
                    if self.headers.get('Metadata-Flavor') != 'Google':
                        self._send_json(403, {'error': 'missing Metadata-Flavor'})
                        return
                    with outer.lock:
                        outer.token_requests += 1
                        if outer.fail_next_tokens > 0:
                            outer.fail_next_tokens -= 1
                            self._send_json(503, {'error': 'metadata unavailable'})
                            return
                        tok = outer.token
                        exp = outer.expires_in
                    self._send_json(200, {'access_token': tok, 'expires_in': exp, 'token_type': 'Bearer'})
                    return

                if not self._check_auth():
                    return

                # /storage/v1/b/<bucket>/o/<name>
                parts = parsed.path.strip('/').split('/')
                if len(parts) == 6 and parts[:3] == ['storage', 'v1', 'b'] and parts[4] == 'o':
                    bucket = unquote(parts[3])
                    name = unquote(parts[5])
                    params = parse_qs(parsed.query)
                    if params.get('alt') == ['media']:
                        with outer.lock:
                            outer.download_requests += 1
                            if outer.fail_next_downloads > 0:
                                outer.fail_next_downloads -= 1
                                self._send_json(503, {'error': 'transient download failure'})
                                return
                            data = outer.objects.get((bucket, name))
                            corrupt = outer.corrupt_download_bytes
                        if data is None:
                            self._send_json(404, {'error': 'not found'})
                            return
                        payload = (data[:-1] + bytes([data[-1] ^ 0xFF])) if (corrupt and data) else data
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/octet-stream')
                        self.send_header('Content-Length', str(len(payload)))
                        self.end_headers()
                        self.wfile.write(payload)
                        return
                    else:
                        with outer.lock:
                            outer.metadata_requests += 1
                            data = outer.objects.get((bucket, name))
                        if data is None:
                            self._send_json(404, {'error': 'not found'})
                            return
                        self._send_json(
                            200,
                            {
                                'bucket': bucket,
                                'name': name,
                                'size': str(len(data)),
                                'md5Hash': bs.md5_b64(data),
                                'crc32c': bs.crc32c_b64(data),
                            },
                        )
                        return
                self._send_json(404, {'error': 'unknown route'})

            def do_POST(self):
                if not self._check_auth():
                    return
                parsed = urlsplit(self.path)
                parts = parsed.path.strip('/').split('/')
                # /upload/storage/v1/b/<bucket>/o?uploadType=media&name=<name>
                if len(parts) == 6 and parts[:4] == ['upload', 'storage', 'v1', 'b'] and parts[5] == 'o':
                    bucket = unquote(parts[4])
                    params = parse_qs(parsed.query)
                    if params.get('uploadType') != ['media'] or 'name' not in params:
                        self._send_json(400, {'error': 'bad upload params'})
                        return
                    name = params['name'][0]
                    length = int(self.headers.get('Content-Length', '0'))
                    raw = self.rfile.read(length)
                    with outer.lock:
                        outer.upload_requests += 1
                        if outer.fail_next_uploads > 0:
                            outer.fail_next_uploads -= 1
                            self._send_json(503, {'error': 'transient upload failure'})
                            return
                        outer.objects[(bucket, name)] = raw
                        corrupt_hash = outer.corrupt_upload_hash
                        omit_hashes = outer.omit_upload_hashes
                    meta = {'bucket': bucket, 'name': name, 'size': str(len(raw))}
                    if not omit_hashes:
                        meta['md5Hash'] = bs.md5_b64(b'wrong') if corrupt_hash else bs.md5_b64(raw)
                        meta['crc32c'] = bs.crc32c_b64(raw)
                    self._send_json(200, meta)
                    return
                self._send_json(404, {'error': 'unknown route'})

            def do_DELETE(self):
                if not self._check_auth():
                    return
                parsed = urlsplit(self.path)
                parts = parsed.path.strip('/').split('/')
                if len(parts) == 6 and parts[:3] == ['storage', 'v1', 'b'] and parts[4] == 'o':
                    bucket = unquote(parts[3])
                    name = unquote(parts[5])
                    with outer.lock:
                        outer.delete_requests += 1
                        outer.objects.pop((bucket, name), None)
                    self._send_json(200, {})
                    return
                self._send_json(404, {'error': 'unknown route'})

        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.port = self.httpd.server_port
        self.base_url = f'http://127.0.0.1:{self.port}'
        self.token_url = f'{self.base_url}/computeMetadata/v1/instance/service-accounts/default/token'
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def make_store(self, bucket: str = 'test-bucket', prefix: str = 'eksmu-test', **kwargs) -> bs.GcsBlobStore:
        bs.clear_token_cache()
        return bs.GcsBlobStore(
            bucket=bucket,
            prefix=prefix,
            api_endpoint=self.base_url,
            token_url=self.token_url,
            retry_delay=0.005,
            **kwargs,
        )

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode('ascii')


class BlobStoreAndQuotaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'data').mkdir()
        self.db_path = self.root / 'data/community.sqlite'
        self.store = Store(self.db_path)
        self.addCleanup(self.store.close)
        self.actor_a = self.store.register('作者甲')
        self.actor_b = self.store.register('作者乙')
        self.aid = self.actor_a['actor']['id']
        self.bid = self.actor_b['actor']['id']
        pub_a = self.store.publish(self.actor_a['token'], '甲的研究正文', title='甲文')
        pub_b = self.store.publish(self.actor_b['token'], '乙的研究正文', title='乙文')
        self.wid_a = pub_a['exploration']['id']
        self.rev_a = pub_a['revision']
        self.wid_b = pub_b['exploration']['id']
        self.rev_b = pub_b['revision']

    def test_quota_constants_match_specification(self):
        self.assertEqual(bs.MAX_FILE, 32 * 1024 * 1024)
        self.assertEqual(bs.MAX_AUTHOR_STORAGE, 256 * 1024 * 1024)
        self.assertEqual(bs.MAX_STORAGE_SQLITE, 2 * 1024 * 1024 * 1024)
        self.assertEqual(bs.MAX_STORAGE_GCS, 200 * 1024 * 1024 * 1024)
        self.assertEqual(bs.MAX_PER_REVISION, 16)
        self.assertEqual(bs.MAX_CONCURRENT_LARGE_REQUESTS, 2)

    def test_sqlite_blobstore_deduplication_streaming_and_corruption_detection(self):
        research = Research(str(self.db_path) + '.research.sqlite', self.store.rows())
        self.addCleanup(research.close)
        payload = b'content-addressed sqlite payload\n'
        rev2 = self.store.revise(self.actor_a['token'], self.wid_a, self.rev_a['id'], '甲文v2', '更新正文')
        research.records = self.store.rows()
        research.index = {r['id']: r for r in research.records}

        m1 = research.upload(self.aid, self.rev_a['id'], 'one.txt', b64(payload))
        m2 = research.upload(self.aid, rev2['id'], 'two.txt', b64(payload))
        self.assertEqual(m1['sha256'], m2['sha256'])
        # Only one row in blobs table despite two attachment rows referencing it
        blob_count = research.db.execute('SELECT COUNT(*) FROM blobs').fetchone()[0]
        self.assertEqual(blob_count, 1)
        # research_files stores NULL for inline data
        inline = research.db.execute('SELECT data FROM research_files WHERE id=?', (m1['id'],)).fetchone()[0]
        self.assertIsNone(inline)
        # Streaming read returns exact bytes
        total, chunks, mime, fname = research.download_stream(m1['id'], chunk_size=7)
        self.assertEqual(total, len(payload))
        self.assertEqual(b''.join(chunks), payload)
        self.assertEqual(fname, 'one.txt')
        # Corrupting the underlying blob is caught on read
        research.db.execute('UPDATE blobs SET data=? WHERE sha256=?', (b'tampered', m1['sha256']))
        with self.assertRaises(Problem) as ctx:
            research.download(m1['id'])
        self.assertEqual(ctx.exception.status, 503)

    def test_gcs_blobstore_upload_download_dedup_token_cache_retry_and_integrity(self):
        fake = FakeGcsServer()
        self.addCleanup(fake.close)
        gcs = fake.make_store(bucket='eksmu-bucket', prefix='alpha')
        research = Research(str(self.db_path) + '.research.sqlite', self.store.rows(), blobstore=gcs)
        self.addCleanup(research.close)

        raw = b'%PDF-1.7\nfake-gcs-pdf-payload'
        digest = bs.sha256_hex(raw)
        expected_key = f'alpha/sha256/{digest[:2]}/{digest}'

        m1 = research.upload(self.aid, self.rev_a['id'], 'doc.pdf', b64(raw))
        self.assertEqual(m1['sha256'], digest)
        self.assertIn(('eksmu-bucket', expected_key), fake.objects)
        self.assertEqual(fake.token_requests, 1)
        self.assertEqual(fake.upload_requests, 1)

        # Uploading same bytes in another filename deduplicates via metadata check without re-uploading bytes
        m2 = research.upload(self.aid, self.rev_a['id'], 'doc-copy.pdf', b64(raw))
        self.assertEqual(m2['sha256'], digest)
        self.assertEqual(fake.upload_requests, 1)
        self.assertEqual(fake.token_requests, 1)  # Cached token reused

        # Fallback verification when upload response omits md5Hash/crc32c
        fake.omit_upload_hashes = True
        raw2 = b'fallback-verify-content\n'
        m3 = research.upload(self.aid, self.rev_a['id'], 'fallback.txt', b64(raw2))
        self.assertEqual(research.download(m3['id'])[0], raw2)
        fake.omit_upload_hashes = False

        # Transient network/503 error on upload retries and succeeds
        fake.fail_next_uploads = 2
        raw3 = b'retry-success-content\n'
        m4 = research.upload(self.aid, self.rev_a['id'], 'retry.txt', b64(raw3))
        self.assertEqual(research.download(m4['id'])[0], raw3)

        # Persistent upload error fails and leaves no dirty metadata row in research_files
        before_count = research.db.execute('SELECT COUNT(*) FROM research_files').fetchone()[0]
        fake.fail_next_uploads = 5
        with self.assertRaises(Problem) as ctx:
            research.upload(self.aid, self.rev_a['id'], 'fail.txt', b64(b'will-fail-upload\n'))
        self.assertEqual(ctx.exception.status, 503)
        self.assertIn('文字发表不受影响', str(ctx.exception))
        after_count = research.db.execute('SELECT COUNT(*) FROM research_files').fetchone()[0]
        self.assertEqual(after_count, before_count)

        # Upload checksum mismatch rejected, deletes bad object, and leaves no metadata row
        fake.corrupt_upload_hash = True
        with self.assertRaises(Problem) as ctx:
            research.upload(self.aid, self.rev_a['id'], 'corrupt-up.txt', b64(b'bad-upload-hash\n'))
        self.assertEqual(ctx.exception.status, 503)
        self.assertGreaterEqual(fake.delete_requests, 1)
        self.assertEqual(research.db.execute('SELECT COUNT(*) FROM research_files').fetchone()[0], before_count)
        fake.corrupt_upload_hash = False

        # Download SHA-256 mismatch rejected and never returns corrupted bytes
        fake.corrupt_download_bytes = True
        with self.assertRaises(Problem) as ctx:
            research.download(m1['id'])
        self.assertEqual(ctx.exception.status, 503)
        fake.corrupt_download_bytes = False
        self.assertEqual(research.download(m1['id'])[0], raw)

    def test_all_quota_boundaries_and_messages_preserve_text_publication(self):
        research = Research(str(self.db_path) + '.research.sqlite', self.store.rows())
        self.addCleanup(research.close)

        # 1. Single file limit boundary
        with patch('eksmu.research.MAX_FILE', 10):
            ok_item = research.upload(self.aid, self.rev_a['id'], 'exact10.txt', b64(b'0123456789'))
            self.assertEqual(ok_item['size'], 10)
            with self.assertRaises(Problem) as ctx:
                research.upload(self.aid, self.rev_a['id'], 'over10.txt', b64(b'0123456789A'))
            self.assertEqual(ctx.exception.status, 413)
            self.assertIn('单个附件不能超过 32 MiB；文字发表不受影响', str(ctx.exception))

        # 2. Author quota boundary (256 MiB default, patched to 25 bytes; duplicate references count per row)
        with patch('eksmu.research.MAX_AUTHOR_STORAGE', 25):
            # Actor A already has 10 bytes ('exact10.txt'). Uploading same 10 bytes under a new filename adds 10 -> 20 bytes
            dup_item = research.upload(self.aid, self.rev_a['id'], 'dup10.txt', b64(b'0123456789'))
            self.assertEqual(dup_item['size'], 10)
            # Adding 5 more bytes reaches exact boundary 25
            five_item = research.upload(self.aid, self.rev_a['id'], 'five.txt', b64(b'12345'))
            self.assertEqual(five_item['size'], 5)
            # Adding 1 more byte exceeds 25
            with self.assertRaises(Problem) as ctx:
                research.upload(self.aid, self.rev_a['id'], 'overflow-author.txt', b64(b'x'))
            self.assertEqual(ctx.exception.status, 413)
            self.assertIn('作者附件总配额已满（上限 256 MiB）；文字发表不受影响', str(ctx.exception))
            # Actor B has 0 bytes used, so Actor B can still upload
            b_item = research.upload(self.bid, self.rev_b['id'], 'b-ok.txt', b64(b'1234567890'))
            self.assertEqual(b_item['size'], 10)

        # 3. Site-wide storage quota boundary (25 + 10 = 35 bytes currently used)
        with patch('eksmu.research.MAX_STORAGE', 38):
            research.upload(self.bid, self.rev_b['id'], 'b-3.txt', b64(b'abc'))
            with self.assertRaises(Problem) as ctx:
                research.upload(self.bid, self.rev_b['id'], 'b-over.txt', b64(b'z'))
            self.assertEqual(ctx.exception.status, 413)
            self.assertIn('全站附件存储配额已满；文字发表不受影响', str(ctx.exception))

        # 4. Per-revision attachment count boundary
        with patch('eksmu.research.MAX_PER_REVISION', 3):
            with self.assertRaises(Problem) as ctx:
                research.upload(self.aid, self.rev_a['id'], 'fourth.txt', b64(b'y'))
            self.assertEqual(ctx.exception.status, 413)
            self.assertIn('每版本最多 16 个附件，可以在新版本继续整理；文字发表不受影响', str(ctx.exception))

        # Text publication remains unaffected after all quota rejections
        new_work = self.store.publish(self.actor_a['token'], '超限后依然可以正常发表文字')
        self.assertEqual(new_work['revision']['payload']['body'], '超限后依然可以正常发表文字')

    def test_legacy_inline_blobs_compatibility_and_resumable_idempotent_migration(self):
        r_path = Path(str(self.db_path) + '.research.sqlite')
        p_path = Path(str(self.db_path) + '.publications.sqlite')

        # Create legacy schema tables with inline BLOBs (no size or preview_sha256 columns)
        img = Image.new('RGB', (6, 6), color=(10, 20, 30))
        buf = BytesIO()
        img.save(buf, format='PNG')
        png_bytes = buf.getvalue()
        txt_bytes = b'legacy inline text attachment\n'
        fid1 = str(uuid.uuid4())
        fid2 = str(uuid.uuid4())

        with sqlite3.connect(r_path) as rdb:
            rdb.executescript('''
              CREATE TABLE research_files(
                id TEXT PRIMARY KEY, revision_id TEXT NOT NULL, actor_id TEXT NOT NULL,
                filename TEXT NOT NULL, mime TEXT NOT NULL, sha256 TEXT NOT NULL, created_at TEXT NOT NULL,
                data BLOB NOT NULL, preview BLOB,
                UNIQUE(revision_id,filename,sha256));
              CREATE TABLE research_tasks(
                id TEXT PRIMARY KEY, revision_id TEXT NOT NULL, actor_id TEXT NOT NULL,
                title TEXT NOT NULL, description TEXT NOT NULL, category TEXT NOT NULL,
                state TEXT NOT NULL, version INTEGER NOT NULL, created_at TEXT NOT NULL);
              CREATE TABLE research_events(
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES research_tasks(id),
                actor_id TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL);
              CREATE TABLE research_claims(
                task_id TEXT NOT NULL REFERENCES research_tasks(id), actor_id TEXT NOT NULL,
                PRIMARY KEY(task_id,actor_id));
            ''')
            rdb.execute(
                'INSERT INTO research_files VALUES(?,?,?,?,?,?,?,?,?)',
                (fid1, self.rev_a['id'], self.aid, 'legacy.txt', 'text/plain',
                 bs.sha256_hex(txt_bytes), '2026-01-01T00:00:00+00:00', txt_bytes, None),
            )
            rdb.execute(
                'INSERT INTO research_files VALUES(?,?,?,?,?,?,?,?,?)',
                (fid2, self.rev_a['id'], self.aid, 'legacy.png', 'image/png',
                 bs.sha256_hex(png_bytes), '2026-01-01T00:00:01+00:00', png_bytes, png_bytes),
            )

        # Open Research and Publications: legacy inline rows must work before migration
        research = Research(r_path, self.store.rows())
        pub = Publications(p_path, self.store.rows())
        try:
            self.assertEqual(research.download(fid1)[0], txt_bytes)
            self.assertEqual(research.download(fid2)[0], png_bytes)
            self.assertEqual(research.download(fid2, preview=True)[0], png_bytes)
            snap = pub.create_snapshot(research, self.aid, self.rev_a['id'], [fid1], None, str(uuid.uuid4()))
            # Simulate a legacy inline publication_blobs row
            pub.db.execute('UPDATE publication_blobs SET data=? WHERE sha256=?', (txt_bytes, bs.sha256_hex(txt_bytes)))
            self.assertEqual(pub.file(snap['id'], fid1)[0], txt_bytes)
        finally:
            pub.close()
            research.close()

        # Migrate to fake GCS in interrupted steps (max_rows=1) and then via CLI to completion
        fake = FakeGcsServer()
        self.addCleanup(fake.close)
        env = {
            **os.environ,
            'EKSMU_BLOB_BUCKET': 'migrate-bucket',
            'EKSMU_BLOB_PREFIX': 'mig',
            'EKSMU_GCS_API_ENDPOINT': fake.base_url,
            'EKSMU_GCS_TOKEN_URL': fake.token_url,
        }
        with patch.dict(os.environ, env):
            bs.clear_token_cache()
            step1 = bs.migrate_blobs(self.db_path, max_rows=1)
            self.assertFalse(step1['complete'])
            self.assertEqual(step1['migrated_research_files'], 1)
            self.assertGreaterEqual(step1['remaining_research_blobs'], 1)

            # Run CLI migrate-blobs to finish remaining rows
            proc = subprocess.run(
                [sys.executable, '-m', 'eksmu', '--db', str(self.db_path), 'migrate-blobs', '--offline'],
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            step2 = json.loads(proc.stdout)
            self.assertTrue(step2['complete'])
            self.assertEqual(step2['remaining_research_blobs'], 0)
            self.assertEqual(step2['remaining_publication_blobs'], 0)

            # Re-running migrate-blobs is idempotent
            step3 = bs.migrate_blobs(self.db_path)
            self.assertTrue(step3['complete'])
            self.assertEqual(step3['migrated_research_files'], 0)
            self.assertEqual(step3['migrated_publication_blobs'], 0)

            # Verify SQLite inline BLOBs are now NULL and downloads/exports read from GCS
            research = Research(r_path, self.store.rows())
            pub = Publications(p_path, self.store.rows())
            try:
                rem = research.db.execute(
                    'SELECT COUNT(*) FROM research_files WHERE data IS NOT NULL OR preview IS NOT NULL'
                ).fetchone()[0]
                self.assertEqual(rem, 0)
                self.assertEqual(research.download(fid1)[0], txt_bytes)
                self.assertEqual(research.download(fid2)[0], png_bytes)
                self.assertEqual(research.download(fid2, preview=True)[0], png_bytes)
                pkg = pub.export(snap['id'])
                self.assertTrue(verify_package(pkg)['content_integrity'])
            finally:
                pub.close()
                research.close()

    def test_gcs_backup_records_referenced_blobs_and_detects_missing_object_on_verify_and_restore(self):
        fake = FakeGcsServer()
        self.addCleanup(fake.close)
        env = {
            **os.environ,
            'EKSMU_BLOB_BUCKET': 'backup-bucket',
            'EKSMU_BLOB_PREFIX': 'bk',
            'EKSMU_GCS_API_ENDPOINT': fake.base_url,
            'EKSMU_GCS_TOKEN_URL': fake.token_url,
        }
        with patch.dict(os.environ, env):
            bs.clear_token_cache()
            research = Research(str(self.db_path) + '.research.sqlite', self.store.rows())
            pub = Publications(str(self.db_path) + '.publications.sqlite', self.store.rows())
            try:
                raw_file = b'important attachment for backup verification\n'
                att = research.upload(self.aid, self.rev_a['id'], 'backup.txt', b64(raw_file))
                snap = pub.create_snapshot(research, self.aid, self.rev_a['id'], [att['id']], None, str(uuid.uuid4()))
            finally:
                pub.close()
                research.close()

            backup_dir = self.root / 'backup-gcs'
            restored_dir = self.root / 'restored-gcs'
            result = create_backup(self.db_path, backup_dir, offline=True)
            self.assertTrue(result['data_verified'])
            self.assertEqual(result['manifest']['blob_backend'], 'gcs')
            self.assertIn(att['sha256'], result['manifest']['referenced_blobs'])

            # Restore succeeds when all referenced blobs exist in GCS
            restored_db = restore_backup(backup_dir, restored_dir)
            r_restored = Research(str(restored_db) + '.research.sqlite', self.store.rows())
            p_restored = Publications(str(restored_db) + '.publications.sqlite', self.store.rows())
            try:
                self.assertEqual(r_restored.download(att['id'])[0], raw_file)
                self.assertEqual(p_restored.file(snap['id'], att['id'])[0], raw_file)
            finally:
                p_restored.close()
                r_restored.close()

            # Delete the object from GCS: verify-backup and restore must fail clearly
            fake.objects.clear()
            with self.assertRaisesRegex(ValueError, 'Missing referenced blob in object storage'):
                verify_backup(backup_dir)
            with self.assertRaisesRegex(ValueError, 'Missing referenced blob in object storage'):
                restore_backup(backup_dir, self.root / 'restored-missing')

    def test_portable_backup_recovers_without_source_bucket_and_rejects_corruption(self):
        fake = FakeGcsServer()
        self.addCleanup(fake.close)
        env = {'EKSMU_BLOB_BUCKET': 'backup-bucket', 'EKSMU_BLOB_PREFIX': 'portable',
               'EKSMU_GCS_API_ENDPOINT': fake.base_url, 'EKSMU_GCS_TOKEN_URL': fake.token_url}
        raw = b'portable attachment and snapshot\n'
        with patch.dict(os.environ, env):
            bs.clear_token_cache()
            research = Research(str(self.db_path) + '.research.sqlite', self.store.rows())
            pub = Publications(str(self.db_path) + '.publications.sqlite', self.store.rows())
            try:
                att = research.upload(self.aid, self.rev_a['id'], 'portable.txt', b64(raw))
                snap = pub.create_snapshot(research, self.aid, self.rev_a['id'], [att['id']], None, str(uuid.uuid4()))
            finally:
                pub.close(); research.close()
            directory = self.root / 'portable'
            result = create_backup(self.db_path, directory, offline=True, portable_blobs=True)
            self.assertEqual(result['manifest']['blob_backend'], 'embedded-sqlite')
            fake.objects.clear()
            self.assertTrue(verify_backup(directory)['data_verified'])
            restored = restore_backup(directory, self.root / 'portable-restored')
        with patch.dict(os.environ, {'EKSMU_BLOB_BUCKET': ''}):
            research = Research(str(restored) + '.research.sqlite', self.store.rows())
            pub = Publications(str(restored) + '.publications.sqlite', self.store.rows())
            try:
                self.assertEqual(research.download(att['id'])[0], raw)
                self.assertEqual(pub.file(snap['id'], att['id'])[0], raw)
            finally:
                pub.close(); research.close()
        with sqlite3.connect(directory / 'research.sqlite') as db:
            db.execute('UPDATE blobs SET data=?', (b'corrupt',))
        # Even a recomputed outer database digest must not hide bad blob bytes.
        manifest_path = directory / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        data = (directory / 'research.sqlite').read_bytes()
        manifest['components']['research'] = {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'Portable backup missing or corrupted blob'):
            verify_backup(directory)

    def test_concurrent_large_requests_rate_limited_with_503(self):
        server = make_server(self.db_path, 0)
        port = server.server_port
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(timeout=5)))

        headers = {
            'Host': f'127.0.0.1:{port}',
            'Origin': f'http://127.0.0.1:{port}',
            'X-Eksmu-Client': 'local-prototype',
            'Content-Type': 'application/json',
            'Authorization': f"Bearer {self.actor_a['token']}",
        }
        body = json.dumps({
            'revision_id': self.rev_a['id'],
            'filename': 'concurrent.txt',
            'data_b64': b64(b'hello concurrent world\n'),
        }).encode('utf-8')

        # Acquire both semaphore slots to simulate 2 in-flight large uploads
        self.assertTrue(LARGE_REQUEST_SEMAPHORE.acquire(blocking=False))
        self.assertTrue(LARGE_REQUEST_SEMAPHORE.acquire(blocking=False))
        try:
            req = urllib.request.Request(
                f'http://127.0.0.1:{port}/api/research/upload',
                data=body,
                headers=headers,
                method='POST',
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(ctx.exception.code, 503)
            err_doc = json.loads(ctx.exception.read().decode('utf-8'))
            ctx.exception.close()
            self.assertIn('文字发表不受影响', err_doc['error'])
        finally:
            LARGE_REQUEST_SEMAPHORE.release()
            LARGE_REQUEST_SEMAPHORE.release()

        # Once slots are released, upload succeeds
        req_ok = urllib.request.Request(
            f'http://127.0.0.1:{port}/api/research/upload',
            data=body,
            headers=headers,
            method='POST',
        )
        with urllib.request.urlopen(req_ok, timeout=5) as resp:
            self.assertEqual(resp.status, 200)


if __name__ == '__main__':
    unittest.main()
