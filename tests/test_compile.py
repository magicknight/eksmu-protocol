"""Unit and integration tests for LaTeX source archives and isolated Cloud Run Job compilation."""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import http.client
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlsplit
import zipfile

from eksmu import backup, work_page
from eksmu.blobstore import md5_b64, sha256_hex
from eksmu.compile_worker import CloudRunCompileClient, run_compile_queue, sanitize_compiled_html
from deployment.compile.compile_entry import (
    _check_recorder_file,
    _compile_html_with_latexml,
    _convert_pdf_to_png_bytes,
    _count_pdf_pages,
    _downscale_raster_image_bytes,
    _extract_xml_graphics_map,
    _format_exit_code,
    _inline_latexml_resources,
    _is_recoverable_latexmlpost_output,
    _sanitize_work_paths_in_log,
    _strip_png_time_chunks,
    _validate_source_date_epoch,
    compile_archive_bytes,
    detect_engine as container_detect_engine,
    sanitize_compiled_html as container_sanitize_compiled_html,
)
from eksmu.research import (
    Research,
    detect_tex_engine,
    file_input,
    inspect_source_archive,
    select_main_tex,
)
from eksmu.server import make_server
from eksmu.store import Problem, Store


def make_zip_bytes(entries: dict[str, bytes | str], *, symlink_name: str | None = None) -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            raw = data.encode('utf-8') if isinstance(data, str) else data
            zf.writestr(name, raw)
        if symlink_name:
            info = zipfile.ZipInfo(symlink_name)
            info.create_system = 3
            info.external_attr = (0o120777 << 16)
            zf.writestr(info, b'/etc/passwd')
    return buf.getvalue()


def make_tar_bytes(
    entries: dict[str, bytes | str],
    *,
    gz: bool = True,
    symlink_name: str | None = None,
    hardlink_name: str | None = None,
    fifo_name: str | None = None,
    declared_size_override: dict[str, int] | None = None,
) -> bytes:
    buf = BytesIO()
    mode = 'w:gz' if gz else 'w'
    with tarfile.open(fileobj=buf, mode=mode) as tf:
        for name, data in entries.items():
            raw = data.encode('utf-8') if isinstance(data, str) else data
            ti = tarfile.TarInfo(name=name)
            ti.size = (declared_size_override or {}).get(name, len(raw))
            tf.addfile(ti, BytesIO(raw))
        if symlink_name:
            ti = tarfile.TarInfo(name=symlink_name)
            ti.type = tarfile.SYMTYPE
            ti.linkname = '/etc/passwd'
            tf.addfile(ti)
        if hardlink_name:
            ti = tarfile.TarInfo(name=hardlink_name)
            ti.type = tarfile.LNKTYPE
            ti.linkname = 'main.tex'
            tf.addfile(ti)
        if fifo_name:
            ti = tarfile.TarInfo(name=fifo_name)
            ti.type = tarfile.FIFOTYPE
            tf.addfile(ti)
    return buf.getvalue()


class FakeResponse:
    def __init__(self, status: int, body: bytes, headers: dict[str, str] | None = None):
        self.status = status
        self._stream = BytesIO(body)
        self.headers = headers or {}

    def read(self, amt: int | None = None) -> bytes:
        return self._stream.read() if amt is None else self._stream.read(amt)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeCloudRunAndGcsService:
    """In-memory fake for GCE metadata token, GCS compile bucket, and Cloud Run Admin API v2."""

    def __init__(self, expected_bucket: str = 'test-compile-bucket'):
        self.expected_bucket = expected_bucket
        self.objects: dict[str, bytes] = {}
        self.runs: list[dict] = []
        self.executions: dict[str, dict] = {}
        self.token = 'fake-compile-token'

    def urlopen(self, req, timeout=None):
        import urllib.error

        method = req.get_method()
        parsed = urlsplit(req.full_url)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path.endswith('/token'):
            if req.headers.get('Metadata-flavor') != 'Google' and req.headers.get('Metadata-Flavor') != 'Google':
                raise urllib.error.HTTPError(req.full_url, 403, 'Forbidden', {}, BytesIO(b'missing header'))
            return FakeResponse(200, json.dumps({'access_token': self.token, 'expires_in': 3600}).encode('utf-8'))

        auth = req.headers.get('Authorization', '')
        if auth != f'Bearer {self.token}':
            raise urllib.error.HTTPError(req.full_url, 401, 'Unauthorized', {}, BytesIO(b'bad token'))

        # GCS upload: /upload/storage/v1/b/<bucket>/o?uploadType=media&name=<name>
        if method == 'POST' and path == f'/upload/storage/v1/b/{self.expected_bucket}/o':
            obj_name = query.get('name', [''])[0]
            raw = bytes(req.data or b'')
            self.objects[obj_name] = raw
            return FakeResponse(200, json.dumps({'name': obj_name, 'size': str(len(raw)), 'md5Hash': md5_b64(raw)}).encode('utf-8'))

        # GCS download: /storage/v1/b/<bucket>/o/<escaped_name>?alt=media
        prefix = f'/storage/v1/b/{self.expected_bucket}/o/'
        if method == 'GET' and path.startswith(prefix):
            obj_name = unquote(path[len(prefix):])
            if obj_name not in self.objects:
                raise urllib.error.HTTPError(req.full_url, 404, 'Not Found', {}, BytesIO(b'not found'))
            return FakeResponse(200, self.objects[obj_name])

        # Cloud Run Admin API v2 jobs:run
        if method == 'POST' and path.endswith(':run'):
            body = json.loads((req.data or b'{}').decode('utf-8'))
            exec_idx = len(self.runs) + 1
            exec_name = f'projects/test-proj/locations/us-central1/jobs/tex-job/executions/exec-{exec_idx}'
            self.runs.append({'path': path, 'body': body, 'execution_name': exec_name})
            self.executions[exec_name] = {'name': exec_name, 'runningCount': 1}
            return FakeResponse(200, json.dumps({'name': f'operations/{exec_idx}', 'metadata': {'name': exec_name}}).encode('utf-8'))

        # Cloud Run Admin API v2 execution GET
        if method == 'GET' and '/executions/' in path:
            exec_name = path.split('/v2/', 1)[-1]
            if exec_name not in self.executions:
                raise urllib.error.HTTPError(req.full_url, 404, 'Not Found', {}, BytesIO(b'not found'))
            return FakeResponse(200, json.dumps(self.executions[exec_name]).encode('utf-8'))

        raise urllib.error.HTTPError(req.full_url, 404, 'Unhandled route', {}, BytesIO(path.encode('utf-8')))


class ArchiveValidationAndMainFileTests(unittest.TestCase):
    def test_valid_archives_and_main_file_priority(self):
        tex_doc = r'\documentclass{article}\begin{document}Hello\end{document}'
        for fname, raw in (
            ('paper.zip', make_zip_bytes({'main.tex': tex_doc, 'sub/part.tex': 'Sub'})),
            ('paper.tar.gz', make_tar_bytes({'main.tex': tex_doc}, gz=True)),
            ('paper.tgz', make_tar_bytes({'main.tex': tex_doc}, gz=True)),
            ('paper.tar', make_tar_bytes({'main.tex': tex_doc}, gz=False)),
        ):
            with self.subTest(fname=fname):
                out_raw, mime, preview = file_input(fname, base64.b64encode(raw).decode())
                self.assertEqual(out_raw, raw)
                self.assertEqual(mime, 'application/octet-stream')
                self.assertIsNone(preview)

        # Priority: top-level \documentclass -> main.tex > ms.tex > paper.tex > <archive_stem>.tex > alphabetical
        sources1 = {
            'a.tex': r'\documentclass{article}',
            'paper.tex': r'\documentclass{article}',
            'ms.tex': r'\documentclass{article}',
            'main.tex': r'\documentclass{article}',
        }
        self.assertEqual(select_main_tex(sources1, 'bundle.zip'), 'main.tex')
        del sources1['main.tex']
        self.assertEqual(select_main_tex(sources1, 'bundle.zip'), 'ms.tex')
        del sources1['ms.tex']
        self.assertEqual(select_main_tex(sources1, 'bundle.zip'), 'paper.tex')
        del sources1['paper.tex']
        sources1['my_article.tex'] = r'\documentclass{article}'
        self.assertEqual(select_main_tex(sources1, 'my_article.tar.gz'), 'my_article.tex')

        # Subdirectory \documentclass does not beat top-level \documentclass
        sources2 = {
            'sub/main.tex': r'\documentclass{article}',
            'zeta.tex': r'\documentclass{article}',
            'intro.tex': r'No documentclass here',
        }
        self.assertEqual(select_main_tex(sources2, 'work.zip'), 'zeta.tex')

        # Engine detection
        for detect_fn in (detect_tex_engine, container_detect_engine):
            self.assertEqual(detect_fn({'main.tex': r'\documentclass{article}\usepackage{fontspec}'}, 'main.tex'), 'xelatex')
            self.assertEqual(detect_fn({'main.tex': r'\documentclass{ctexart}'}, 'main.tex'), 'xelatex')
            self.assertEqual(detect_fn({'main.tex': r'\documentclass{article}\usepackage{luacode}'}, 'main.tex'), 'lualatex')
            self.assertEqual(detect_fn({'main.tex': r'\documentclass{article}'}, 'main.tex'), 'pdflatex')

            # Magic comment on line 35 (before \documentclass on line 37) beats \usepackage{fontspec}
            header_34 = '\n'.join(f'%% Header comment line {i} (mentioning \\documentclass in comment)' for i in range(1, 35))
            line35_magic_src = (
                header_34
                + '\n% !TEX program = lualatex\n'
                + '%% Blank comment line 36\n'
                + '\\documentclass[10pt]{memoir}\n'
                + '\\usepackage{fontspec}\n'
                + '\\begin{document}\nHello\n\\end{document}\n'
            )
            self.assertEqual(line35_magic_src.splitlines()[34], '% !TEX program = lualatex')
            self.assertEqual(detect_fn({'main.tex': line35_magic_src}, 'main.tex'), 'lualatex')

            # Magic comment AFTER \documentclass is NOT recognized
            after_docclass_src = (
                '\\documentclass{article}\n'
                '% !TEX program = lualatex\n'
                '\\usepackage{fontspec}\n'
                '\\begin{document}\nHello\n\\end{document}\n'
            )
            self.assertEqual(detect_fn({'main.tex': after_docclass_src}, 'main.tex'), 'xelatex')
            after_docclass_pdf_src = (
                '\\documentclass{article}\n'
                '% !TEX program = lualatex\n'
                '\\begin{document}\nHello\n\\end{document}\n'
            )
            self.assertEqual(detect_fn({'main.tex': after_docclass_pdf_src}, 'main.tex'), 'pdflatex')

        # Multi-pass latexmk logs should report the final pass page count, not the first pass
        multipass_log = (
            'Output written on main.pdf (978 pages, 5120000 bytes).\n'
            'Latexmk: applying rule \'lualatex\'...\n'
            'Output written on main.pdf (1001 pages, 5402016 bytes).\n'
        )
        self.assertEqual(_count_pdf_pages(b'%PDF-1.5', multipass_log), 1001)

    def test_fls_recorder_whitelist_allows_fontconfig_symlinks_and_rejects_sensitive_reads_and_outside_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work_dir = root / 'work'
            work_dir.mkdir()
            (work_dir / 'main.tex').write_text(r'\documentclass{article}', encoding='utf-8')
            (work_dir / 'main.aux').write_text('', encoding='utf-8')

            # Create a symlink pointing to /usr/share/fontconfig/conf.avail/10-hinting-slight.conf
            # (simulating /etc/fonts/conf.d/10-hinting-slight.conf -> /usr/share/fontconfig/conf.avail/...)
            fc_symlink = work_dir / '10-hinting-slight.conf'
            fc_symlink.symlink_to('/usr/share/fontconfig/conf.avail/10-hinting-slight.conf')

            fls = work_dir / 'main.fls'
            fls.write_text(
                f'PWD {work_dir}\n'
                f'INPUT main.tex\n'
                f'INPUT /etc/fonts/fonts.conf\n'
                f'INPUT /etc/fonts/conf.d/10-hinting-slight.conf\n'
                f'INPUT /usr/share/fontconfig/conf.avail/10-hinting-slight.conf\n'
                f'INPUT {fc_symlink}\n'
                f'INPUT /usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc\n'
                f'OUTPUT main.aux\n'
                f'OUTPUT {work_dir / "main.pdf"}\n',
                encoding='utf-8',
            )
            self.assertIsNone(_check_recorder_file(fls, work_dir, '/usr/local/texlive/2026/bin/x86_64-linux'))

            # Sensitive paths outside whitelist must be rejected even if tex_bin_dir is /usr/bin
            for forbidden_read in (
                '/etc/passwd',
                '/etc/hostname',
                '/etc/shadow',
                '/proc/self/environ',
                '/home/texrunner/.bashrc',
                '/home/alice/.profile',
                str(root / 'outside_secret.txt'),
            ):
                with self.subTest(forbidden_read=forbidden_read):
                    fls.write_text(
                        f'PWD {work_dir}\nINPUT main.tex\nINPUT {forbidden_read}\nOUTPUT main.aux\n',
                        encoding='utf-8',
                    )
                    err = _check_recorder_file(fls, work_dir, '/usr/bin')
                    self.assertIsNotNone(err)
                    self.assertIn('禁止读取工作目录与 TeX 发行版以外的文件', err)

            # Writing outside work_dir (including to read-whitelisted fontconfig/tex dirs) must be rejected
            for forbidden_write in (
                '../escaped.aux',
                str(root / 'outside.pdf'),
                '/tmp/evil.out',
                '/etc/fonts/conf.d/10-hinting-slight.conf',
                '/usr/share/fontconfig/conf.avail/10-hinting-slight.conf',
                '/usr/local/texlive/2026/texmf-dist/evil.sty',
            ):
                with self.subTest(forbidden_write=forbidden_write):
                    fls.write_text(
                        f'PWD {work_dir}\nINPUT main.tex\nOUTPUT {forbidden_write}\n',
                        encoding='utf-8',
                    )
                    err = _check_recorder_file(fls, work_dir, '/usr/local/texlive/2026/bin/x86_64-linux')
                    self.assertIsNotNone(err)
                    self.assertIn('禁止写入工作目录外的文件', err)

    def test_archive_rejects_traversal_abs_path_symlinks_many_entries_bomb_and_no_tex(self):
        # 1. Zip slip (..)
        with self.assertRaises(Problem) as ctx:
            inspect_source_archive(make_zip_bytes({'../evil.tex': r'\documentclass{article}'}), 'a.zip')
        self.assertIn('..', str(ctx.exception))

        # 2. Absolute path
        with self.assertRaises(Problem) as ctx:
            inspect_source_archive(make_zip_bytes({'/etc/passwd': 'root', 'main.tex': r'\documentclass{article}'}), 'a.zip')
        self.assertIn('绝对路径', str(ctx.exception))

        # 3. Symlink in zip and tar, hardlink/fifo in tar
        with self.assertRaises(Problem) as ctx:
            inspect_source_archive(make_zip_bytes({'main.tex': r'\documentclass{article}'}, symlink_name='link.tex'), 'a.zip')
        self.assertIn('符号链接', str(ctx.exception))

        with self.assertRaises(Problem) as ctx:
            inspect_source_archive(make_tar_bytes({'main.tex': r'\documentclass{article}'}, symlink_name='link.tex'), 'a.tar.gz')
        self.assertIn('符号链接', str(ctx.exception))

        with self.assertRaises(Problem) as ctx:
            inspect_source_archive(make_tar_bytes({'main.tex': r'\documentclass{article}'}, hardlink_name='hard.tex'), 'a.tar.gz')
        self.assertIn('符号链接', str(ctx.exception))

        with self.assertRaises(Problem) as ctx:
            inspect_source_archive(make_tar_bytes({'main.tex': r'\documentclass{article}'}, fifo_name='pipe'), 'a.tar.gz')
        self.assertIn('设备文件', str(ctx.exception))

        # 4. > 2000 entries
        many = {f'f_{i}.txt': 'x' for i in range(2001)}
        many['main.tex'] = r'\documentclass{article}'
        with self.assertRaises(Problem) as ctx:
            inspect_source_archive(make_zip_bytes(many), 'many.zip')
        self.assertIn('2000', str(ctx.exception))

        # 5. Total uncompressed > 256 MiB (checked from header metadata without allocating 256 MiB in test)
        buf = BytesIO()
        with tarfile.open(fileobj=buf, mode='w') as tf:
            ti = tarfile.TarInfo('huge.bin')
            ti.size = 257 * 1024 * 1024
            tf.addfile(ti)
        with self.assertRaises(Problem) as ctx:
            inspect_source_archive(buf.getvalue(), 'huge.tar')
        self.assertIn('256 MiB', str(ctx.exception))

        # 6. Compression ratio > 100x (zip bomb)
        bomb_payload = b'A' * (300 * 1024)  # highly repetitive -> compresses > 200x
        with self.assertRaises(Problem) as ctx:
            inspect_source_archive(make_zip_bytes({'main.tex': bomb_payload}), 'bomb.zip')
        self.assertIn('100 倍', str(ctx.exception))

        # 7. No .tex file
        with self.assertRaises(Problem) as ctx:
            inspect_source_archive(make_zip_bytes({'readme.md': '# hello'}), 'notex.zip')
        self.assertIn('.tex', str(ctx.exception))


class CompileQueueAndWorkPageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        data_dir = self.root / 'data'
        data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = data_dir / 'community.sqlite'

    def _sample_zip(self, extra: dict[str, str] | None = None) -> bytes:
        files = {
            'main.tex': r'\documentclass{article}\begin{document}Main Document\end{document}',
            'alt.tex': r'\documentclass{article}\begin{document}Alt Document\end{document}',
        }
        if extra:
            files.update(extra)
        return make_zip_bytes(files)

    def test_disabled_switch_does_not_queue_and_shows_notice(self):
        with patch.dict(os.environ, {'EKSMU_COMPILE_ENABLED': '0'}, clear=False):
            store = Store(self.db_path)
            try:
                actor = store.register('作者甲')
                rev = store.publish(actor['token'], '正文', '标题')['revision']
                res = Research(str(self.db_path) + '.research.sqlite', store.rows())
                try:
                    att = res.upload(actor['actor']['id'], rev['id'], 'src.zip', base64.b64encode(self._sample_zip()).decode())
                    jobs = res.db.execute('SELECT * FROM compile_jobs').fetchall()
                    self.assertEqual(len(jobs), 0)
                    ws = res.compile_workspace(rev['id'], actor['actor']['id'])
                    self.assertFalse(ws['enabled'])
                    self.assertEqual(ws['notice'], '本站编译暂未开放')
                    with self.assertRaises(Problem) as ctx:
                        res.request_compile(actor['actor']['id'], rev['id'], att['id'])
                    self.assertIn('本站编译暂未开放', str(ctx.exception))
                finally:
                    res.close()
            finally:
                store.close()

    def test_auto_queue_deduplication_and_rate_limits(self):
        with patch.dict(os.environ, {'EKSMU_COMPILE_ENABLED': '1'}, clear=False):
            store = Store(self.db_path)
            try:
                actor = store.register('作者乙')
                aid = actor['actor']['id']
                rev = store.publish(actor['token'], '正文', '标题')['revision']
                res = Research(str(self.db_path) + '.research.sqlite', store.rows())
                try:
                    att = res.upload(aid, rev['id'], 'src.zip', base64.b64encode(self._sample_zip()).decode())
                    jobs = res.db.execute('SELECT * FROM compile_jobs WHERE source_attachment_id=?', (att['id'],)).fetchall()
                    self.assertEqual(len(jobs), 1)
                    self.assertEqual(jobs[0]['state'], 'queued')
                    self.assertEqual(jobs[0]['main_file'], 'main.tex')

                    # Requesting same main_file while queued returns same job idempotently
                    same_job = res.request_compile(aid, rev['id'], att['id'], 'main.tex')
                    self.assertEqual(same_job['id'], jobs[0]['id'])

                    # Requesting a different main_file while 1 job is active hits per-author active limit (max 1)
                    with self.assertRaises(Problem) as ctx:
                        res.request_compile(aid, rev['id'], att['id'], 'alt.tex')
                    self.assertEqual(ctx.exception.status, 429)
                    self.assertIn('1 个编译任务', str(ctx.exception))

                    # Mark job succeeded with an output attachment; requesting again returns existing succeeded job without re-queueing
                    pdf_att = res._add_file(
                        aid, rev['id'], 'main.pdf', b'%PDF-1.5\ncompiled-pdf', 'application/pdf', None,
                        origin='compiled', derived_from_sha256=att['sha256'],
                    )
                    res.db.execute(
                        "UPDATE compile_jobs SET state='succeeded', output_attachment_id=?, finished_at=? WHERE id=?",
                        (pdf_att['id'], datetime.now(timezone.utc).isoformat(), jobs[0]['id']),
                    )
                    dedup_job = res.request_compile(aid, rev['id'], att['id'], 'main.tex')
                    self.assertEqual(dedup_job['id'], jobs[0]['id'])
                    self.assertEqual(dedup_job['state'], 'succeeded')
                    self.assertEqual(res.db.execute('SELECT COUNT(*) FROM compile_jobs').fetchone()[0], 1)

                    # Author daily limit (20/day)
                    now_iso = datetime.now(timezone.utc).isoformat()
                    for i in range(19):
                        res.db.execute(
                            'INSERT INTO compile_jobs(id, revision_id, source_attachment_id, actor_id, state, main_file, engine, requested_at) '
                            'VALUES(?,?,?,?,?,?,?,?)',
                            (f'hist-a-{i}', rev['id'], att['id'], aid, 'failed', 'alt.tex', 'pdflatex', now_iso),
                        )
                    with self.assertRaises(Problem) as ctx:
                        res.request_compile(aid, rev['id'], att['id'], 'alt.tex')
                    self.assertEqual(ctx.exception.status, 429)
                    self.assertIn('20 次', str(ctx.exception))

                    # Site daily limit (200/day)
                    other = store.register('作者丙')
                    other_id = other['actor']['id']
                    rev_other = store.publish(other['token'], '正文2', '标题2')['revision']
                    res2 = Research(str(self.db_path) + '.research.sqlite', store.rows())
                    try:
                        for i in range(180):
                            res2.db.execute(
                                'INSERT INTO compile_jobs(id, revision_id, source_attachment_id, actor_id, state, main_file, engine, requested_at) '
                                'VALUES(?,?,?,?,?,?,?,?)',
                                (f'hist-site-{i}', rev['id'], att['id'], f'actor-{i}', 'failed', 'main.tex', 'pdflatex', now_iso),
                            )
                        att_other = res2.upload(other_id, rev_other['id'], 'other.zip', base64.b64encode(self._sample_zip()).decode())
                        with self.assertRaises(Problem) as ctx:
                            res2.request_compile(other_id, rev_other['id'], att_other['id'], 'alt.tex')
                        self.assertEqual(ctx.exception.status, 429)
                        self.assertIn('200 次', str(ctx.exception))
                    finally:
                        res2.close()
                finally:
                    res.close()
            finally:
                store.close()

    def test_compile_worker_lifecycle_sha_mismatch_timeout_and_bucket_isolation(self):
        fake = FakeCloudRunAndGcsService(expected_bucket='compile-staging-bkt')
        env = {
            'EKSMU_COMPILE_ENABLED': '1',
            'EKSMU_COMPILE_PROJECT': 'test-proj',
            'EKSMU_COMPILE_REGION': 'us-central1',
            'EKSMU_COMPILE_JOB': 'tex-job',
            'EKSMU_COMPILE_BUCKET': 'compile-staging-bkt',
        }
        # Cannot reuse attachment bucket
        with self.assertRaises(Problem) as ctx:
            CloudRunCompileClient(
                project='test-proj',
                region='us-central1',
                job_name='tex-job',
                compile_bucket='same-bkt',
                attachment_bucket='same-bkt',
                urlopen=fake.urlopen,
            )
        self.assertIn('不能复用', str(ctx.exception))

        with patch.dict(os.environ, env, clear=False):
            store = Store(self.db_path)
            try:
                actor = store.register('作者丁')
                aid = actor['actor']['id']
                rev = store.publish(actor['token'], '正文', '标题')['revision']
                res = Research(str(self.db_path) + '.research.sqlite', store.rows())
                try:
                    zip_raw = self._sample_zip()
                    att = res.upload(aid, rev['id'], 'manuscript.zip', base64.b64encode(zip_raw).decode())
                    job = res.db.execute('SELECT * FROM compile_jobs WHERE source_attachment_id=?', (att['id'],)).fetchone()
                    job_id = job['id']
                finally:
                    res.close()
            finally:
                store.close()

            # Step 1: queued -> running (uploads in/<job_id>.src and triggers Cloud Run Job)
            r1 = run_compile_queue(self.db_path, urlopen=fake.urlopen)
            self.assertEqual(len(r1['dispatched']), 1)
            self.assertEqual(r1['dispatched'][0]['job_id'], job_id)
            self.assertEqual(fake.objects[f'in/{job_id}.src'], zip_raw)
            self.assertEqual(len(fake.runs), 1)
            env_vars = {
                e['name']: e['value']
                for e in fake.runs[0]['body']['overrides']['containerOverrides'][0]['env']
            }
            self.assertEqual(env_vars['JOB_ID'], job_id)
            self.assertEqual(env_vars['MAIN_FILE'], 'main.tex')
            expected_epoch = str(int(datetime.fromisoformat(rev['created_at']).timestamp()))
            self.assertEqual(env_vars['SOURCE_DATE_EPOCH'], expected_epoch)

            # Step 2: running -> succeeded when valid result.json + PDF + compile.log appear in out/<job_id>/
            compiled_pdf = b'%PDF-1.5\ncompiled-from-cloud-run-job'
            compile_log = b'This is pdfTeX, Version 3.141592653 (TeX Live 2026)\nOutput written on main.pdf (1 page).\n'
            fake.objects[f'out/{job_id}/main.pdf'] = compiled_pdf
            fake.objects[f'out/{job_id}/compile.log'] = compile_log
            fake.objects[f'out/{job_id}/result.json'] = json.dumps({
                'status': 'succeeded',
                'engine': 'pdflatex',
                'main_file': 'main.tex',
                'toolchain': {
                    'texlive': '2026',
                    'latexmk': 'Latexmk 4.88',
                    'engine': 'pdfTeX (TeX Live 2026)',
                },
                'pdf_sha256': sha256_hex(compiled_pdf),
                'pages': 1,
                'log_sha256': sha256_hex(compile_log),
                'duration_s': 1.2,
                'error': None,
            }).encode('utf-8')

            r2 = run_compile_queue(self.db_path, urlopen=fake.urlopen)
            self.assertEqual(len(r2['completed']), 1)
            self.assertEqual(r2['completed'][0]['state'], 'succeeded')
            out_att_id = r2['completed'][0]['output_attachment_id']

            # Verify stored PDF attachment metadata & SHA-256 mismatch rejection on second job
            store = Store(self.db_path)
            try:
                res = Research(str(self.db_path) + '.research.sqlite', store.rows())
                try:
                    items = {m['id']: m for m in res.files(rev['id'])['items']}
                    pdf_meta = items[out_att_id]
                    self.assertEqual(pdf_meta['filename'], 'main.pdf')
                    self.assertEqual(pdf_meta['origin'], 'compiled')
                    self.assertEqual(pdf_meta['derived_from_sha256'], att['sha256'])
                    self.assertEqual(pdf_meta['compile_info']['texlive'], '2026')
                    self.assertEqual(pdf_meta['compile_info']['engine'], 'pdflatex')
                    self.assertEqual(pdf_meta['compile_info']['source_attachment_id'], att['id'])
                    # The worker records the epoch it dispatched (the revision's creation time).
                    from datetime import datetime as _dt
                    self.assertEqual(pdf_meta['compile_info']['source_date_epoch'],
                                     int(_dt.fromisoformat(rev['created_at']).timestamp()))

                    # Queue second job with alt.tex -> test SHA-256 mismatch rejection
                    job2 = res.request_compile(aid, rev['id'], att['id'], 'alt.tex')
                    job2_id = job2['id']
                finally:
                    res.close()
            finally:
                store.close()

            run_compile_queue(self.db_path, urlopen=fake.urlopen)
            fake.objects[f'out/{job2_id}/alt.pdf'] = b'%PDF-1.5\ntampered-bytes'
            fake.objects[f'out/{job2_id}/compile.log'] = compile_log
            fake.objects[f'out/{job2_id}/result.json'] = json.dumps({
                'status': 'succeeded',
                'engine': 'pdflatex',
                'main_file': 'alt.tex',
                'toolchain': {'texlive': '2026', 'latexmk': '4.88', 'engine': 'pdfTeX'},
                'pdf_sha256': '0' * 64,  # wrong SHA-256!
                'pages': 1,
                'log_sha256': sha256_hex(compile_log),
                'duration_s': 0.9,
                'error': None,
            }).encode('utf-8')
            r_tampered = run_compile_queue(self.db_path, urlopen=fake.urlopen)
            self.assertEqual(r_tampered['completed'][0]['state'], 'failed')
            self.assertIn('SHA-256', r_tampered['completed'][0]['error'])

            # Test 35-minute timeout on a running job without result.json
            store = Store(self.db_path)
            try:
                res = Research(str(self.db_path) + '.research.sqlite', store.rows())
                try:
                    job3 = res.request_compile(aid, rev['id'], att['id'], 'alt.tex')
                    job3_id = job3['id']
                finally:
                    res.close()
            finally:
                store.close()
            run_compile_queue(self.db_path, urlopen=fake.urlopen)
            future_dt = datetime.now(timezone.utc) + timedelta(minutes=40)
            r_timeout = run_compile_queue(self.db_path, urlopen=fake.urlopen, now_dt=future_dt)
            self.assertEqual(r_timeout['completed'][0]['job_id'], job3_id)
            self.assertEqual(r_timeout['completed'][0]['state'], 'failed')
            self.assertIn('超时', r_timeout['completed'][0]['error'])

    def test_work_page_labels_citation_pdf_priority_and_author_only_log_api(self):
        with patch.dict(os.environ, {'EKSMU_COMPILE_ENABLED': '1'}, clear=False):
            server = make_server(self.db_path, 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join()))
            port = server.server_port

            def call(path, data=None, token=None):
                headers = {'Content-Type': 'application/json', 'X-Eksmu-Client': 'local-prototype'}
                if token:
                    headers['Authorization'] = 'Bearer ' + token
                conn = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
                try:
                    conn.request('POST' if data is not None else 'GET', path, None if data is None else json.dumps(data).encode(), headers)
                    resp = conn.getresponse()
                    return resp.status, dict(resp.getheaders()), resp.read()
                finally:
                    conn.close()

            author = json.loads(call('/api/actors', {'alias': '论文作者'})[2])
            peer = json.loads(call('/api/actors', {'alias': '同行读者'})[2])
            tok = author['token']
            peer_tok = peer['token']
            created = json.loads(call('/api/explorations', {'title': 'LaTeX 编译测试作品', 'body': '正文内容'}, tok)[2])
            wid, rev_id = created['exploration']['id'], created['revision']['id']
            # Peer evidence so work passes new-author cooling-off for citation_pdf_url
            self.assertEqual(
                call('/api/evidence', {'revision_id': rev_id, 'source': 'human', 'scope': 's', 'method': 'm', 'result': 'r'}, peer_tok)[0],
                201,
            )

            # Upload source archive
            zip_raw = self._sample_zip()
            code, _, att_raw = call('/api/research/upload', {
                'revision_id': rev_id,
                'filename': 'paper-src.zip',
                'data_b64': base64.b64encode(zip_raw).decode(),
            }, tok)
            self.assertEqual(code, 200)
            src_att = json.loads(att_raw)

            # Simulate worker finishing compilation (succeeded)
            fake = FakeCloudRunAndGcsService(expected_bucket='bkt')
            with patch.dict(os.environ, {
                'EKSMU_COMPILE_PROJECT': 'p', 'EKSMU_COMPILE_REGION': 'r',
                'EKSMU_COMPILE_JOB': 'j', 'EKSMU_COMPILE_BUCKET': 'bkt',
            }):
                r1 = run_compile_queue(self.db_path, urlopen=fake.urlopen)
                job_id = r1['dispatched'][0]['job_id']
                compiled_pdf = b'%PDF-1.5\ncompiled-pdf-bytes'
                log_bytes = b'Private compile log line 1\n'
                fake.objects[f'out/{job_id}/main.pdf'] = compiled_pdf
                fake.objects[f'out/{job_id}/compile.log'] = log_bytes
                fake.objects[f'out/{job_id}/result.json'] = json.dumps({
                    'status': 'succeeded',
                    'engine': 'xelatex',
                    'main_file': 'main.tex',
                    'toolchain': {'texlive': '2026', 'latexmk': '4.88', 'engine': 'XeTeX'},
                    'pdf_sha256': sha256_hex(compiled_pdf),
                    'pages': 1,
                    'log_sha256': sha256_hex(log_bytes),
                    'duration_s': 1.5,
                    'error': None,
                }).encode('utf-8')
                r2 = run_compile_queue(self.db_path, urlopen=fake.urlopen)
                compiled_pdf_id = r2['completed'][0]['output_attachment_id']

            # Work page (zh & en): shows compiled PDF label + link to source archive, and uses compiled PDF for citation_pdf_url
            _, _, zh_html = call(f'/zh/w/{wid}')
            zh_text = zh_html.decode('utf-8')
            self.assertIn('本站从源码编译 · TeX Live 2026 · xelatex', zh_text)
            self.assertIn('同一源码重编可得一致的 PDF', zh_text)
            self.assertNotIn('本版只有 PDF', zh_text)   # has a source package
            self.assertIn(f'/api/research/file?attachment_id={src_att["id"]}', zh_text)
            self.assertIn('源码包：paper-src.zip', zh_text)
            self.assertIn('LaTeX 源码包', zh_text)
            self.assertIn(f'<meta name="citation_pdf_url" content="http://127.0.0.1:{port}/w/{wid}/pdf/{compiled_pdf_id}">', zh_text)

            _, _, en_html = call(f'/en/w/{wid}')
            en_text = en_html.decode('utf-8')
            self.assertIn('Compiled from source by platform · TeX Live 2026 · xelatex', en_text)
            self.assertIn('LaTeX source archive', en_text)

            # Now author uploads their own PDF -> citation_pdf_url prioritizes author-uploaded PDF over compiled PDF
            author_pdf = b'%PDF-1.7\nauthor-own-uploaded-pdf'
            _, _, up_pdf_raw = call('/api/research/upload', {
                'revision_id': rev_id,
                'filename': 'author-final.pdf',
                'data_b64': base64.b64encode(author_pdf).decode(),
            }, tok)
            author_pdf_id = json.loads(up_pdf_raw)['id']
            _, _, zh_html2 = call(f'/zh/w/{wid}')
            zh_text2 = zh_html2.decode('utf-8')
            self.assertIn(f'<meta name="citation_pdf_url" content="http://127.0.0.1:{port}/w/{wid}/pdf/{author_pdf_id}">', zh_text2)

            # Author-only compile-log API check: anonymous -> 401, peer -> 403, author -> 200
            self.assertEqual(call(f'/api/research/compile-log?job_id={job_id}')[0], 401)
            self.assertEqual(call(f'/api/research/compile-log?job_id={job_id}', token=peer_tok)[0], 403)
            code_log, _, raw_log = call(f'/api/research/compile-log?job_id={job_id}', token=tok)
            self.assertEqual(code_log, 200)
            self.assertIn('Private compile log line 1', json.loads(raw_log)['log_text'])

    def test_compile_jobs_covered_by_backup_and_restore(self):
        with patch.dict(os.environ, {'EKSMU_COMPILE_ENABLED': '1'}, clear=False):
            store = Store(self.db_path)
            try:
                actor = store.register('备份测试作者')
                rev = store.publish(actor['token'], '正文', '标题')['revision']
                res = Research(str(self.db_path) + '.research.sqlite', store.rows())
                try:
                    att = res.upload(actor['actor']['id'], rev['id'], 'src.zip', base64.b64encode(self._sample_zip()).decode())
                    jobs_before = [dict(r) for r in res.db.execute('SELECT * FROM compile_jobs').fetchall()]
                    self.assertEqual(len(jobs_before), 1)
                finally:
                    res.close()
            finally:
                store.close()

            snap_dir = self.root / 'backup_snap'
            restored_dir = self.root / 'restored_instance'
            created = backup.create_backup(self.db_path, snap_dir, offline=True)
            self.assertIsNotNone(created['manifest']['components']['research'])
            verified = backup.verify_backup(snap_dir)
            self.assertTrue(verified['data_verified'])
            restored_db = backup.restore_backup(snap_dir, restored_dir)

            with sqlite3.connect(str(restored_db) + '.research.sqlite') as rdb:
                rdb.row_factory = sqlite3.Row
                jobs_after = [dict(r) for r in rdb.execute('SELECT * FROM compile_jobs').fetchall()]
            self.assertEqual(jobs_after, jobs_before)
            self.assertEqual(jobs_after[0]['source_attachment_id'], att['id'])

    def test_html_sanitizer_strips_scripts_events_dangerous_urls_and_external_images(self):
        one_px_png_b64 = (
            'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII='
        )
        valid_data_img = f'data:image/png;base64,{one_px_png_b64}'
        evil_svg_b64 = base64.b64encode(b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"><script>alert(1)</script></svg>').decode('ascii')
        safe_svg_b64 = base64.b64encode(b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><circle cx="5" cy="5" r="4"/></svg>').decode('ascii')

        dirty_html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="0;url=https://evil.example/">
  <base href="https://evil.example/">
  <link rel="stylesheet" href="https://evil.example/steal.css">
  <style>
    @import url("https://evil.example/a.css");
    body {{ color: #111; background: url(https://evil.example/bg.png); }}
  </style>
  <script>alert("xss")</script>
</head>
<body onload="alert(1)" onclick="steal()">
  <h1 onmouseover="evil()">标题与公式</h1>
  <math xmlns="http://www.w3.org/1998/Math/MathML"><mrow><mi>E</mi><mo>=</mo><mi>m</mi><msup><mi>c</mi><mn>2</mn></msup></mrow></math>
  <iframe src="https://evil.example/frame.html">iframe fallback</iframe>
  <object data="https://evil.example/obj.swf">obj fallback</object>
  <embed src="https://evil.example/embed.swf">
  <form action="https://evil.example/post"><input type="text" value="x"><button>Go</button></form>
  <p><a href="javascript:alert(1)" onclick="bad()">JS Link</a></p>
  <p><a href="&#10;  JaVaScRiPt:alert(2)">Obfuscated JS Link</a></p>
  <p><a href="vbscript:msgbox(1)">VB Link</a></p>
  <p><a href="data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==">Data HTML Link</a></p>
  <p><a href="https://example.org/paper" title="Safe">Safe Link</a></p>
  <img src="https://evil.example/tracker.png" alt="external">
  <img src="local/relative.png" alt="relative">
  <img src="data:image/svg+xml;base64,{evil_svg_b64}" alt="evil-svg">
  <img src="data:image/svg+xml;base64,{safe_svg_b64}" alt="safe-svg">
  <img src="{valid_data_img}" alt="kept-png" onerror="alert(3)">
</body>
</html>"""

        for fn in (sanitize_compiled_html, container_sanitize_compiled_html):
            cleaned = fn(dirty_html)
            low = cleaned.lower()
            for forbidden in (
                '<script', '<iframe', '<object', '<embed', '<form', '<input', '<button',
                '<base', '<link', 'http-equiv', 'onload', 'onclick', 'onmouseover', 'onerror',
                'javascript:', 'vbscript:', 'data:text/html', 'https://evil.example', 'local/relative.png',
                evil_svg_b64.lower(), '@import', 'url(',
            ):
                self.assertNotIn(forbidden, low)
            self.assertIn(valid_data_img, cleaned)
            self.assertIn(f'data:image/svg+xml;base64,{safe_svg_b64}', cleaned)
            self.assertIn('href="https://example.org/paper"', cleaned)
            self.assertIn('<math xmlns="http://www.w3.org/1998/Math/MathML">', cleaned)
            self.assertIn('标题与公式', cleaned)

        # Server-side extras: quotes inside tag/attribute names and SVG animations that rewrite href.
        tricky = ('<a x"y=1 href="https://example.org/">t</a><div"><b>k</b></div>'
                  '<svg><a xlink:href="#a">x</a><animate attributeName="href" values="javascript:alert(1)"/>'
                  '<set attributeName="href" to="javascript:alert(2)"/></svg>')
        cleaned = sanitize_compiled_html(tricky)
        self.assertNotIn('"y', cleaned)
        self.assertNotIn('<div"', cleaned)
        self.assertNotIn('javascript:', cleaned.lower())
        self.assertNotIn('<animate', cleaned)
        self.assertIn('<a href="https://example.org/">t</a>', cleaned)
        self.assertIn('<b>k</b>', cleaned)

    def test_compiled_html_storage_route_headers_permissions_and_work_page(self):
        one_px_png_b64 = (
            'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII='
        )
        with patch.dict(os.environ, {'EKSMU_COMPILE_ENABLED': '1'}, clear=False):
            server = make_server(self.db_path, 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join()))
            port = server.server_port

            def call(path, data=None, token=None, method=None):
                headers = {'Content-Type': 'application/json', 'X-Eksmu-Client': 'local-prototype'}
                if token:
                    headers['Authorization'] = 'Bearer ' + token
                conn = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
                try:
                    verb = method or ('POST' if data is not None else 'GET')
                    conn.request(verb, path, None if data is None else json.dumps(data).encode(), headers)
                    resp = conn.getresponse()
                    return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
                finally:
                    conn.close()

            author = json.loads(call('/api/actors', {'alias': 'HTML测试作者'})[2])
            peer = json.loads(call('/api/actors', {'alias': 'HTML测试读者'})[2])
            tok = author['token']
            peer_tok = peer['token']
            created = json.loads(call('/api/explorations', {'title': 'HTML 转换作品', 'body': '正文'}, tok)[2])
            wid, rev_id = created['exploration']['id'], created['revision']['id']
            self.assertEqual(
                call('/api/evidence', {'revision_id': rev_id, 'source': 'human', 'scope': 's', 'method': 'm', 'result': 'r'}, peer_tok)[0],
                201,
            )

            zip_raw = self._sample_zip()
            code, _, att_raw = call('/api/research/upload', {
                'revision_id': rev_id,
                'filename': 'bundle.zip',
                'data_b64': base64.b64encode(zip_raw).decode(),
            }, tok)
            self.assertEqual(code, 200)
            src_att = json.loads(att_raw)

            # Simulate Cloud Run Job producing PDF + HTML (where container HTML still has a stray <script> that server sanitizer must strip)
            fake = FakeCloudRunAndGcsService(expected_bucket='bkt-html')
            with patch.dict(os.environ, {
                'EKSMU_COMPILE_PROJECT': 'p', 'EKSMU_COMPILE_REGION': 'r',
                'EKSMU_COMPILE_JOB': 'j', 'EKSMU_COMPILE_BUCKET': 'bkt-html',
            }):
                r1 = run_compile_queue(self.db_path, urlopen=fake.urlopen)
                job_id = r1['dispatched'][0]['job_id']
                compiled_pdf = b'%PDF-1.5\ncompiled-pdf-with-html'
                raw_html = (
                    f'<!DOCTYPE html><html><head><meta charset="utf-8"><title>Doc</title></head>'
                    f'<body onclick="evil()"><p>中文段落与公式</p>'
                    f'<math xmlns="http://www.w3.org/1998/Math/MathML"><mi>x</mi></math>'
                    f'<img src="data:image/png;base64,{one_px_png_b64}" alt="fig">'
                    f'<script>alert(1)</script><a href="javascript:alert(2)">bad</a></body></html>'
                ).encode('utf-8')
                log_bytes = b'This is XeTeX\n=== LaTeXML (latexml) ===\nConversion complete\n'
                fake.objects[f'out/{job_id}/main.pdf'] = compiled_pdf
                fake.objects[f'out/{job_id}/main.html'] = raw_html
                fake.objects[f'out/{job_id}/compile.log'] = log_bytes
                fake.objects[f'out/{job_id}/result.json'] = json.dumps({
                    'status': 'succeeded',
                    'engine': 'xelatex',
                    'main_file': 'main.tex',
                    'toolchain': {'texlive': '2026', 'latexmk': '4.88', 'engine': 'XeTeX', 'latexml': '0.8.8'},
                    'pdf_sha256': sha256_hex(compiled_pdf),
                    'pages': 1,
                    'log_sha256': sha256_hex(log_bytes),
                    'duration_s': 2.1,
                    'error': None,
                    'html': {
                        'status': 'succeeded',
                        'filename': 'main.html',
                        'sha256': sha256_hex(raw_html),
                        'size': len(raw_html),
                        'latexml_version': '0.8.8',
                        'duration_s': 1.4,
                        'error': None,
                    },
                }).encode('utf-8')
                r2 = run_compile_queue(self.db_path, urlopen=fake.urlopen)
                self.assertEqual(r2['completed'][0]['state'], 'succeeded')
                pdf_att_id = r2['completed'][0]['output_attachment_id']
                html_att_id = r2['completed'][0]['html_attachment_id']
                self.assertTrue(html_att_id)

            # Verify stored HTML attachment in DB and server-side sanitization
            store = Store(self.db_path)
            try:
                res = Research(str(self.db_path) + '.research.sqlite', store.rows())
                try:
                    items = {m['id']: m for m in res.files(rev_id)['items']}
                    html_meta = items[html_att_id]
                    self.assertEqual(html_meta['filename'], 'main.html')
                    self.assertEqual(html_meta['mime'], 'text/html')
                    self.assertEqual(html_meta['origin'], 'compiled')
                    self.assertEqual(html_meta['derived_from_sha256'], src_att['sha256'])
                    self.assertEqual(html_meta['compile_info']['latexml'], '0.8.8')
                    # Also insert a non-compiled text/html row to verify /w/<work>/html/<id> rejects non-compiled HTML
                    uploaded_html = res._add_file(
                        author['actor']['id'], rev_id, 'user-uploaded.html',
                        b'<!DOCTYPE html><html><body>Uploaded HTML</body></html>',
                        'text/html', None, origin='upload',
                    )
                    uploaded_html_id = uploaded_html['id']
                finally:
                    res.close()
            finally:
                store.close()

            # Check GET & HEAD on /w/<work>/html/<html_att_id> (and /zh/w/..., /en/w/...)
            for route in (f'/w/{wid}/html/{html_att_id}', f'/zh/w/{wid}/html/{html_att_id}', f'/en/w/{wid}/html/{html_att_id}'):
                st, hdrs, body = call(route)
                self.assertEqual(st, 200)
                self.assertEqual(hdrs.get('content-type'), 'text/html; charset=utf-8')
                self.assertEqual(
                    hdrs.get('content-security-policy'),
                    "sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
                )
                self.assertEqual(hdrs.get('x-content-type-options'), 'nosniff')
                self.assertEqual(hdrs.get('referrer-policy'), 'no-referrer')
                self.assertEqual(hdrs.get('cache-control'), 'public, max-age=3600')
                body_str = body.decode('utf-8')
                self.assertIn('中文段落与公式', body_str)
                self.assertIn(f'data:image/png;base64,{one_px_png_b64}', body_str)
                self.assertNotIn('<script', body_str.lower())
                self.assertNotIn('onclick', body_str.lower())
                self.assertNotIn('javascript:', body_str.lower())

            st_head, hdrs_head, body_head = call(f'/w/{wid}/html/{html_att_id}', method='HEAD')
            self.assertEqual(st_head, 200)
            self.assertEqual(hdrs_head.get('content-type'), 'text/html; charset=utf-8')
            self.assertEqual(body_head, b'')

            # Non-compiled text/html attachment MUST be rejected (404) on /w/<work>/html/<file_id>
            self.assertEqual(call(f'/w/{wid}/html/{uploaded_html_id}')[0], 404)
            # PDF attachment MUST be rejected (404) on /w/<work>/html/<file_id>
            self.assertEqual(call(f'/w/{wid}/html/{pdf_att_id}')[0], 404)

            # Check work page (zh & en) links, LaTeXML badge, disclaimer, readable size, and citation_pdf_url
            _, _, zh_page = call(f'/zh/w/{wid}')
            zh_str = zh_page.decode('utf-8')
            self.assertIn(f'/w/{wid}/html/{html_att_id}', zh_str)
            self.assertIn('HTML 版', zh_str)
            self.assertIn('本站从源码转换 · LaTeXML 0.8.8', zh_str)
            self.assertIn('HTML 版由程序自动从 LaTeX 源码转换，公式和排版可能与 PDF 不同，请以 PDF 为准。', zh_str)
            self.assertIn(f'<meta name="citation_pdf_url" content="http://127.0.0.1:{port}/w/{wid}/pdf/{pdf_att_id}">', zh_str)

            _, _, en_page = call(f'/en/w/{wid}')
            en_str = en_page.decode('utf-8')
            self.assertIn(f'/w/{wid}/html/{html_att_id}', en_str)
            self.assertIn('HTML version', en_str)
            self.assertIn('Converted from source by platform · LaTeXML 0.8.8', en_str)
            self.assertIn('The HTML version is automatically converted from the LaTeX source by program; formulas and layout may differ from the PDF, and the PDF version is authoritative.', en_str)

    def test_latexmlpost_exit_code_handling_and_log_diagnostics(self):
        one_px_png = base64.b64decode(
            'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII='
        )
        complete_html = (
            '<!DOCTYPE html><html><head><title>Paper</title></head>'
            '<body><article class="ltx_document"><p>Formula <math><mi>x</mi></math></p>'
            '<img src="x1.png" alt="fig1"></article></body></html>'
        )
        self.assertEqual(_format_exit_code(-11), '-11 (SIGSEGV)')
        self.assertEqual(_format_exit_code(0), '0')

        with tempfile.TemporaryDirectory(prefix='eksmu-tex-build-') as tmp:
            tmp_root = Path(tmp)
            src_dir = tmp_root / 'src'
            src_dir.mkdir()
            (src_dir / 'main.tex').write_text(r'\documentclass{article}\begin{document}Hello\end{document}', encoding='utf-8')
            out_dir = tmp_root / 'out'
            out_dir.mkdir()

            # 1. latexmlpost exits with -11 (SIGSEGV) after writing complete main.html and x1.png -> accepted
            def fake_run_recoverable(cmd, cwd, env, timeout_s):
                if cmd[0] == 'latexml':
                    (cwd / 'main.latexml.xml').write_text('<document><p>Hello</p></document>', encoding='utf-8')
                    return 0, f'Conversion complete (See {cwd}/main.latexml.log)\n', False
                (cwd / 'x1.png').write_bytes(one_px_png)
                (cwd / 'main.html').write_text(complete_html, encoding='utf-8')
                return -11, f'latexmlpost paginating {cwd}/main.latexml.xml\n(Writer main.html processing... 0.01 sec) 33.80 sec)', False

            with patch('deployment.compile.compile_entry.shutil.which', return_value='/usr/bin/latexml'), \
                 patch('deployment.compile.compile_entry._run_step_with_timeout', side_effect=fake_run_recoverable):
                res, log = _compile_html_with_latexml(
                    src_dir, tmp_root, 'main.tex', out_dir,
                    {'PATH': '/usr/bin:/bin'}, '/usr/local/texlive/2026/bin/x86_64-linux', '0.8.8', 60.0,
                )
            self.assertEqual(res['status'], 'succeeded')
            self.assertIsNone(res['error'])
            self.assertIn('[Eksmu] latexmlpost exit_code: -11 (SIGSEGV)', log)
            self.assertIn('[Eksmu] output directory files', log)
            self.assertIn('main.html (', log)
            self.assertIn('x1.png (', log)
            self.assertIn('[Eksmu] Note: latexmlpost exited with non-zero code (-11 (SIGSEGV))', log)
            self.assertNotIn(str(tmp_root), log)
            out_html_str = (out_dir / 'main.html').read_text(encoding='utf-8')
            self.assertIn('data:image/png;base64,', out_html_str)

            # 2. latexmlpost exits non-zero with truncated HTML (no </body></html>) -> rejected
            shutil.rmtree(tmp_root / 'work_html', ignore_errors=True)
            def fake_run_truncated(cmd, cwd, env, timeout_s):
                if cmd[0] == 'latexml':
                    (cwd / 'main.latexml.xml').write_text('<document/>', encoding='utf-8')
                    return 0, 'ok\n', False
                (cwd / 'main.html').write_text('<!DOCTYPE html><html><body><p>Truncated', encoding='utf-8')
                return 1, 'Writer interrupted\n', False

            with patch('deployment.compile.compile_entry.shutil.which', return_value='/usr/bin/latexml'), \
                 patch('deployment.compile.compile_entry._run_step_with_timeout', side_effect=fake_run_truncated):
                res_trunc, _ = _compile_html_with_latexml(
                    src_dir, tmp_root, 'main.tex', out_dir,
                    {'PATH': '/usr/bin:/bin'}, None, '0.8.8', 60.0,
                )
            self.assertEqual(res_trunc['status'], 'failed')
            self.assertEqual(res_trunc['error'], 'LaTeXML 后处理未通过，请查看编译日志')

            # 3. latexmlpost exits non-zero with Fatal: error -> rejected even if HTML file exists
            shutil.rmtree(tmp_root / 'work_html', ignore_errors=True)
            def fake_run_fatal(cmd, cwd, env, timeout_s):
                if cmd[0] == 'latexml':
                    (cwd / 'main.latexml.xml').write_text('<document/>', encoding='utf-8')
                    return 0, 'ok\n', False
                (cwd / 'main.html').write_text(complete_html, encoding='utf-8')
                return 1, 'Fatal:internal:something broke\n', False

            with patch('deployment.compile.compile_entry.shutil.which', return_value='/usr/bin/latexml'), \
                 patch('deployment.compile.compile_entry._run_step_with_timeout', side_effect=fake_run_fatal):
                res_fatal, _ = _compile_html_with_latexml(
                    src_dir, tmp_root, 'main.tex', out_dir,
                    {'PATH': '/usr/bin:/bin'}, None, '0.8.8', 60.0,
                )
            self.assertEqual(res_fatal['status'], 'failed')

            # 4. latexmlpost exits with security violation 99 -> rejected with security reason
            shutil.rmtree(tmp_root / 'work_html', ignore_errors=True)
            def fake_run_sec(cmd, cwd, env, timeout_s):
                if cmd[0] == 'latexml':
                    (cwd / 'main.latexml.xml').write_text('<document/>', encoding='utf-8')
                    return 0, 'ok\n', False
                (cwd / 'main.html').write_text(complete_html, encoding='utf-8')
                return 99, '[EKSMU_SECURITY_VIOLATION] 禁止访问工作目录外的路径: /etc/passwd\n', False

            with patch('deployment.compile.compile_entry.shutil.which', return_value='/usr/bin/latexml'), \
                 patch('deployment.compile.compile_entry._run_step_with_timeout', side_effect=fake_run_sec):
                res_sec, _ = _compile_html_with_latexml(
                    src_dir, tmp_root, 'main.tex', out_dir,
                    {'PATH': '/usr/bin:/bin'}, None, '0.8.8', 60.0,
                )
            self.assertEqual(res_sec['status'], 'failed')
            self.assertIn('禁止访问工作目录外的路径', res_sec['error'])

    def test_latexml_pdf_figure_conversion_and_progressive_image_downscaling(self):
        one_px_png = base64.b64decode(
            'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII='
        )
        big_png = b'\x89PNG\r\n\x1a\n' + (b'X' * 2500)

        with tempfile.TemporaryDirectory(prefix='eksmu-tex-build-') as tmp:
            tmp_root = Path(tmp)
            src_dir = tmp_root / 'src'
            figs_dir = src_dir / 'figs'
            figs_dir.mkdir(parents=True)
            (src_dir / 'main.tex').write_text(r'\documentclass{article}\begin{document}Figs\end{document}', encoding='utf-8')
            (figs_dir / 'fig1.pdf').write_bytes(b'%PDF-1.5\nfake-pdf-1')
            (figs_dir / 'fig2.pdf').write_bytes(b'%PDF-1.5\nfake-pdf-2')
            out_dir = tmp_root / 'out'
            out_dir.mkdir()

            xml_content = (
                '<document>'
                '<ltx:graphics xml:id="S5.F1.g1" graphic="fig1.pdf" candidates="figs/fig1.pdf"/>'
                '<ltx:graphics xml:id="S5.F2.g1" graphic="fig2.pdf" candidates="figs/fig2.pdf"/>'
                '</document>'
            )
            html_content = (
                '<!DOCTYPE html><html><head><title>Figs</title></head><body>'
                '<img id="S5.F1.g1" src="" class="ltx_graphics ltx_missing ltx_missing_image" alt="f1">'
                '<img id="S5.F2.g1" src="figs/fig2.pdf" class="ltx_graphics" alt="f2">'
                '</body></html>'
            )

            def fake_run(cmd, cwd, env, timeout_s):
                if cmd[0] == 'latexml':
                    (cwd / 'main.latexml.xml').write_text(xml_content, encoding='utf-8')
                    return 0, 'ok\n', False
                (cwd / 'main.html').write_text(html_content, encoding='utf-8')
                return 0, 'ok\n', False

            def fake_pdf_to_png(pdf_path, work_root, dpi=150):
                return big_png if dpi >= 150 else one_px_png

            def fake_downscale(img_path, raw, work_root, max_bytes, max_dim=1200):
                return one_px_png, 'image/png'

            # Set MAX_HTML_BYTES small (3000 bytes) so the first pass at 150 DPI (2 x big_png -> >6800 B base64)
            # exceeds MAX_HTML_BYTES and triggers progressive image downscaling to fit within MAX_HTML_BYTES!
            with patch('deployment.compile.compile_entry.shutil.which', return_value='/usr/bin/latexml'), \
                 patch('deployment.compile.compile_entry._run_step_with_timeout', side_effect=fake_run), \
                 patch('deployment.compile.compile_entry._convert_pdf_to_png_bytes', side_effect=fake_pdf_to_png), \
                 patch('deployment.compile.compile_entry._downscale_raster_image_bytes', side_effect=fake_downscale), \
                 patch('deployment.compile.compile_entry.MAX_HTML_BYTES', 3000):
                res, log = _compile_html_with_latexml(
                    src_dir, tmp_root, 'main.tex', out_dir,
                    {'PATH': '/usr/bin:/bin'}, None, '0.8.8', 60.0,
                )
            self.assertEqual(res['status'], 'succeeded')
            self.assertLessEqual(res['size'], 3000)
            self.assertIn('[Eksmu] Note: downscaled inline images', log)
            final_html = (out_dir / 'main.html').read_text(encoding='utf-8')
            self.assertEqual(final_html.count('src="data:image/png;base64,'), 2)
            self.assertNotIn('ltx_missing_image', final_html)

    def test_source_date_epoch_validation_and_env_propagation(self):
        # 1. Valid non-negative integers not in the future
        val0, warn0 = _validate_source_date_epoch('0', now_ts=1800000000.0)
        self.assertEqual(val0, 0)
        self.assertIsNone(warn0)

        val_ok, warn_ok = _validate_source_date_epoch('1782864000', now_ts=1800000000.0)
        self.assertEqual(val_ok, 1782864000)
        self.assertIsNone(warn_ok)

        val_none, warn_none = _validate_source_date_epoch(None, now_ts=1800000000.0)
        self.assertIsNone(val_none)
        self.assertIsNone(warn_none)

        # 2. Invalid values: negative, non-numeric, boolean, future timestamp
        for bad_val in ('-5', -1, 'abc', '12.34', True, '1900000000', 1900000000):
            with self.subTest(bad_val=bad_val):
                v, w = _validate_source_date_epoch(bad_val, now_ts=1800000000.0)
                self.assertIsNone(v)
                self.assertIsNotNone(w)
                self.assertIn('SOURCE_DATE_EPOCH', w)

        # 3. Verify compile_archive_bytes passes SOURCE_DATE_EPOCH and FORCE_SOURCE_DATE=1 when valid,
        #    and logs warning + omits SOURCE_DATE_EPOCH when invalid
        zip_raw = self._sample_zip()
        captured_envs: list[dict[str, str]] = []

        def fake_run_step(cmd, cwd, env, timeout_s):
            captured_envs.append(dict(env))
            if cmd[0] == 'latexmk':
                (cwd / 'main.pdf').write_bytes(b'%PDF-1.5\nfake-pdf')
                (cwd / 'main.log').write_text('Output written on main.pdf (1 page).\n', encoding='utf-8')
                return 0, 'Latexmk: ok\n', False
            return 0, 'ok\n', False

        with tempfile.TemporaryDirectory() as tmp:
            out_valid = Path(tmp) / 'out_valid'
            with patch('deployment.compile.compile_entry._run_step_with_timeout', side_effect=fake_run_step), \
                 patch('deployment.compile.compile_entry.shutil.which', return_value=None):
                res_v = compile_archive_bytes(zip_raw, out_valid, source_date_epoch='1782864000')
            self.assertEqual(res_v['status'], 'succeeded')
            self.assertTrue(captured_envs)
            self.assertEqual(captured_envs[0].get('SOURCE_DATE_EPOCH'), '1782864000')
            self.assertEqual(captured_envs[0].get('FORCE_SOURCE_DATE'), '1')
            self.assertEqual(captured_envs[0].get('TZ'), 'UTC')

            captured_envs.clear()
            out_invalid = Path(tmp) / 'out_invalid'
            future_epoch = str(int(datetime.now(timezone.utc).timestamp()) + 86400)
            with patch('deployment.compile.compile_entry._run_step_with_timeout', side_effect=fake_run_step), \
                 patch('deployment.compile.compile_entry.shutil.which', return_value=None):
                res_inv = compile_archive_bytes(zip_raw, out_invalid, source_date_epoch=future_epoch)
            self.assertEqual(res_inv['status'], 'succeeded')
            self.assertNotIn('SOURCE_DATE_EPOCH', captured_envs[0])
            self.assertNotIn('FORCE_SOURCE_DATE', captured_envs[0])
            log_invalid = (out_invalid / 'compile.log').read_text(encoding='utf-8')
            self.assertIn('Warning: SOURCE_DATE_EPOCH', log_invalid)

    def test_html_shared_deadline_stops_remaining_figure_conversions_and_preserves_alt_placeholders(self):
        one_px_png = base64.b64decode(
            'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII='
        )
        with tempfile.TemporaryDirectory(prefix='eksmu-tex-build-') as tmp:
            tmp_root = Path(tmp)
            src_dir = tmp_root / 'src'
            figs_dir = src_dir / 'figs'
            figs_dir.mkdir(parents=True)
            (src_dir / 'main.tex').write_text(r'\documentclass{article}\begin{document}Figs\end{document}', encoding='utf-8')
            for i in range(1, 5):
                (figs_dir / f'fig{i}.pdf').write_bytes(f'%PDF-1.5\nfake-pdf-{i}'.encode('utf-8'))
            out_dir = tmp_root / 'out'
            out_dir.mkdir()

            xml_content = (
                '<document>'
                + ''.join(
                    f'<ltx:graphics xml:id="F{i}" graphic="fig{i}.pdf" candidates="figs/fig{i}.pdf"/>'
                    for i in range(1, 5)
                )
                + '</document>'
            )
            html_content = (
                '<!DOCTYPE html><html><head><title>Many Figs</title></head><body>'
                + ''.join(
                    f'<img id="F{i}" src="figs/fig{i}.pdf" class="ltx_graphics" alt="Alt text for figure {i}">'
                    for i in range(1, 5)
                )
                + '</body></html>'
            )

            # Simulated monotonic clock: starts at 1000.0, budget is 100.0 -> deadline is 1100.0
            clock = [1000.0]
            recorded_timeouts: list[tuple[str, float]] = []
            converted_figs: list[str] = []

            def fake_monotonic():
                return clock[0]

            def fake_run(cmd, cwd, env, timeout_s):
                recorded_timeouts.append((cmd[0], round(timeout_s, 1)))
                if cmd[0] == 'latexml':
                    clock[0] += 30.0  # now 1030.0 (70s remaining)
                    (cwd / 'main.latexml.xml').write_text(xml_content, encoding='utf-8')
                    return 0, 'ok\n', False
                if cmd[0] == 'latexmlpost':
                    clock[0] += 40.0  # now 1070.0 (30s remaining)
                    (cwd / 'main.html').write_text(html_content, encoding='utf-8')
                    return 0, 'ok\n', False
                if cmd[0] == 'pdftocairo':
                    # Each pdftocairo takes 16s; after 2 figures clock reaches 1102.0 >= 1100.0 deadline!
                    pdf_arg = Path(cmd[-2]).name
                    converted_figs.append(pdf_arg)
                    out_png = Path(cmd[-1] + '.png')
                    out_png.write_bytes(one_px_png)
                    clock[0] += 16.0
                    return 0, 'ok\n', False
                return 0, 'ok\n', False

            with patch('deployment.compile.compile_entry.time.monotonic', side_effect=fake_monotonic), \
                 patch('deployment.compile.compile_entry.shutil.which', side_effect=lambda name, path=None: f'/usr/bin/{name}'), \
                 patch('deployment.compile.compile_entry._run_step_with_timeout', side_effect=fake_run):
                res, log = _compile_html_with_latexml(
                    src_dir, tmp_root, 'main.tex', out_dir,
                    {'PATH': '/usr/bin:/bin'}, None, '0.8.8', 100.0,
                )

            self.assertEqual(res['status'], 'succeeded')
            # Verify subprocess timeouts used min(default, remaining_time):
            # latexml: 100.0s, latexmlpost: 70.0s, pdftocairo #1: min(30, 30)=30.0s, pdftocairo #2: min(30, 14)=14.0s
            self.assertEqual(
                recorded_timeouts,
                [('latexml', 100.0), ('latexmlpost', 70.0), ('pdftocairo', 30.0), ('pdftocairo', 14.0)],
            )
            # Only fig1.pdf and fig2.pdf were converted; fig3.pdf and fig4.pdf were skipped when deadline was reached!
            self.assertEqual(converted_figs, ['fig1.pdf', 'fig2.pdf'])
            self.assertIn('2 remaining image(s) kept as alt-text placeholders', log)
            final_html = (out_dir / 'main.html').read_text(encoding='utf-8')
            self.assertEqual(final_html.count('src="data:image/png;base64,'), 2)
            self.assertIn('Alt text for figure 3', final_html)
            self.assertIn('Alt text for figure 4', final_html)


if __name__ == '__main__':
    unittest.main()
