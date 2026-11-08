"""Research materials and collaborative steps, separate from publication permission."""
from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import difflib
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import tarfile
import uuid
import warnings
import zipfile

from .blobstore import (
    DEFAULT_CHUNK_SIZE,
    MAX_AUTHOR_STORAGE,
    MAX_FILE,
    MAX_PER_REVISION,
    MAX_STORAGE_GCS,
    MAX_STORAGE_SQLITE,
    create_blobstore,
    ensure_research_files_schema,
    iter_bytes,
)
from .store import Problem, canonical, exact, identifier, text

MAX_STORAGE = MAX_STORAGE_SQLITE
MAX_TASKS_PER_WORK = 100
MAX_EVENTS_PER_TASK = 200
MAX_LEAN_ENROLLMENTS_PER_ACTOR = 20
FILE_FORMAT = 'eksmu.research-materials.v1'
TEXT_EXTS = {'.txt', '.md', '.csv', '.tsv', '.json', '.py', '.jl', '.r', '.cpp', '.c', '.h', '.lean', '.tex', '.ipynb'}
IMAGE_FORMATS = {'.png': 'PNG', '.jpg': 'JPEG', '.jpeg': 'JPEG', '.webp': 'WEBP'}
ARCHIVE_EXTS = {'.zip', '.tar.gz', '.tgz', '.tar'}
MAX_ARCHIVE_ENTRIES = 2000
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_COMPRESSION_RATIO = 100
MAX_COMPILE_LOG_CHARS = 256 * 1024
MAX_COMPILE_ACTIVE_PER_AUTHOR = 1
MAX_COMPILE_DAILY_PER_AUTHOR = 20
MAX_COMPILE_DAILY_SITE = 200
CATEGORIES = {'reproduce', 'counterexample', 'explain', 'formalize', 'data', 'other'}
REDACTION_REASONS = {'privacy', 'copyright', 'illegal', 'author_request', 'other'}


def now():
    return datetime.now(timezone.utc).isoformat()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def is_compile_enabled() -> bool:
    return os.environ.get('EKSMU_COMPILE_ENABLED', '').strip().lower() in {'1', 'true', 'yes', 'on'}


def archive_ext(filename: str) -> str | None:
    lower = (filename or '').lower()
    if lower.endswith('.tar.gz'):
        return '.tar.gz'
    ext = Path(lower).suffix
    if ext in {'.zip', '.tgz', '.tar'}:
        return ext
    return None


def is_source_archive_filename(filename: str) -> bool:
    return archive_ext(filename) is not None


def _archive_stem(filename: str) -> str:
    name = Path(filename or '').name
    lower = name.lower()
    for suf in ('.tar.gz', '.tgz', '.zip', '.tar'):
        if lower.endswith(suf) and len(name) > len(suf):
            return name[:-len(suf)]
    return Path(name).stem


def _validate_archive_rel_path(raw_name: str) -> str:
    if not raw_name or '\x00' in raw_name:
        raise Problem('源码包路径不能为空')
    if '\\' in raw_name:
        raise Problem('源码包路径不能包含 .. 或非法路径分隔符')
    if raw_name.startswith('/') or re.match(r'^[A-Za-z]:', raw_name):
        raise Problem('源码包不能包含绝对路径')
    stripped = raw_name.rstrip('/')
    if not stripped:
        return ''
    parts = stripped.split('/')
    for part in parts:
        if part == '..':
            raise Problem('源码包路径不能包含 .. 或非法路径分隔符')
    clean = [p for p in parts if p and p != '.']
    if not clean:
        return ''
    return '/'.join(clean)


def _strip_tex_comments(content: str) -> str:
    out = []
    for line in content.splitlines():
        idx = re.search(r'(?<!\\)%', line)
        out.append(line[:idx.start()] if idx else line)
    return '\n'.join(out)


def inspect_source_archive(raw: bytes, filename: str) -> dict[str, str]:
    """Validate archive structure in memory without extracting to disk, returning {rel_path: tex_text}."""
    kind = archive_ext(filename)
    if kind is None:
        raise Problem('支持图片、PDF、文本、代码与小型数据文件')
    if not raw:
        raise Problem('附件不能为空；文字发表不受影响')

    entries = 0
    total_uncompressed = 0
    tex_sources: dict[str, str] = {}

    if kind == '.zip':
        try:
            zf = zipfile.ZipFile(BytesIO(raw), 'r')
        except (zipfile.BadZipFile, OSError, ValueError) as exc:
            raise Problem('源码包格式损坏或无法读取') from exc
        with zf:
            infolist = zf.infolist()
            if len(infolist) > MAX_ARCHIVE_ENTRIES:
                raise Problem('源码包条目数不能超过 2000')
            for info in infolist:
                entries += 1
                if entries > MAX_ARCHIVE_ENTRIES:
                    raise Problem('源码包条目数不能超过 2000')
                rel = _validate_archive_rel_path(info.filename)
                mode = (info.external_attr >> 16) & 0o170000
                if mode in {0o120000, 0o020000, 0o060000, 0o010000, 0o140000}:
                    raise Problem('源码包不能包含符号链接、硬链接或设备文件')
                file_size = int(info.file_size or 0)
                compress_size = int(info.compress_size or 0)
                if file_size < 0:
                    raise Problem('源码包格式损坏或无法读取')
                total_uncompressed += file_size
                if total_uncompressed > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                    raise Problem('源码包解压后总大小不能超过 256 MiB')
                if file_size > 0 and (file_size / max(1, compress_size)) > MAX_ARCHIVE_COMPRESSION_RATIO:
                    raise Problem('源码包单文件压缩比不能超过 100 倍')
                if not info.is_dir() and rel.lower().endswith('.tex'):
                    try:
                        with zf.open(info, 'r') as src:
                            data = src.read(min(file_size + 1024, MAX_ARCHIVE_UNCOMPRESSED_BYTES + 1))
                    except (zipfile.BadZipFile, OSError, RuntimeError, EOFError, ValueError) as exc:
                        raise Problem('源码包格式损坏或无法读取') from exc
                    if len(data) > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                        raise Problem('源码包解压后总大小不能超过 256 MiB')
                    if len(data) > 0 and (len(data) / max(1, compress_size)) > MAX_ARCHIVE_COMPRESSION_RATIO:
                        raise Problem('源码包单文件压缩比不能超过 100 倍')
                    tex_sources[rel] = data.decode('utf-8', errors='replace')
    else:
        tar_mode = 'r:gz' if kind in {'.tar.gz', '.tgz'} else 'r:'
        try:
            tf = tarfile.open(fileobj=BytesIO(raw), mode=tar_mode)
        except (tarfile.TarError, OSError, EOFError, ValueError) as exc:
            raise Problem('源码包格式损坏或无法读取') from exc
        with tf:
            try:
                for member in tf:
                    entries += 1
                    if entries > MAX_ARCHIVE_ENTRIES:
                        raise Problem('源码包条目数不能超过 2000')
                    rel = _validate_archive_rel_path(member.name)
                    if member.issym() or member.islnk() or member.isdev() or member.ischr() or member.isfifo() or not (member.isfile() or member.isdir()):
                        raise Problem('源码包不能包含符号链接、硬链接或设备文件')
                    file_size = int(member.size or 0)
                    if file_size < 0:
                        raise Problem('源码包格式损坏或无法读取')
                    total_uncompressed += file_size
                    if total_uncompressed > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                        raise Problem('源码包解压后总大小不能超过 256 MiB')
                    if file_size > 0 and (file_size / max(1, len(raw))) > MAX_ARCHIVE_COMPRESSION_RATIO:
                        raise Problem('源码包单文件压缩比不能超过 100 倍')
                    if member.isfile() and rel.lower().endswith('.tex'):
                        fobj = tf.extractfile(member)
                        if fobj is not None:
                            data = fobj.read(file_size + 1)
                            if len(data) != file_size:
                                raise Problem('源码包格式损坏或无法读取')
                            tex_sources[rel] = data.decode('utf-8', errors='replace')
            except Problem:
                raise
            except (tarfile.TarError, OSError, EOFError, ValueError) as exc:
                raise Problem('源码包格式损坏或无法读取') from exc

    if total_uncompressed > 0 and (total_uncompressed / max(1, len(raw))) > MAX_ARCHIVE_COMPRESSION_RATIO:
        raise Problem('源码包单文件压缩比不能超过 100 倍')
    if not tex_sources:
        raise Problem('源码包中未找到 .tex 文件')
    return tex_sources


def select_main_tex(tex_sources: dict[str, str], archive_filename: str = '') -> str:
    if not tex_sources:
        raise Problem('源码包中未找到 .tex 文件')
    all_paths = sorted(tex_sources.keys())
    with_docclass = [
        p for p in all_paths
        if re.search(r'\\documentclass\b', _strip_tex_comments(tex_sources[p]))
    ]
    top_docclass = [p for p in with_docclass if '/' not in p]
    stem = _archive_stem(archive_filename).lower()
    priority_names = ['main.tex', 'ms.tex', 'paper.tex']
    if stem:
        candidate = f'{stem}.tex'
        if candidate not in priority_names:
            priority_names.append(candidate)

    pool = top_docclass or with_docclass or [p for p in all_paths if '/' not in p] or all_paths
    by_lower_name = {PurePosixPath(p).name.lower(): p for p in reversed(pool)}
    for pname in priority_names:
        if pname in by_lower_name:
            return by_lower_name[pname]
    return pool[0]


def list_tex_candidates(tex_sources: dict[str, str], archive_filename: str = '') -> list[str]:
    if not tex_sources:
        return []
    default_main = select_main_tex(tex_sources, archive_filename)
    rest = sorted(p for p in tex_sources.keys() if p != default_main)
    return [default_main, *rest]


def detect_tex_engine(tex_sources: dict[str, str], main_file: str) -> str:
    ordered = [main_file] + [p for p in sorted(tex_sources) if p != main_file]
    for path in ordered:
        raw = tex_sources.get(path, '')
        for line in raw.splitlines():
            uncommented = re.sub(r'(?<!\\)%.*$', '', line)
            if re.search(r'\\(?:documentclass|begin\s*\{\s*document\s*\})\b', uncommented):
                break
            m = re.match(r'^\s*%\s*!TEX\s+(?:TS-)?program\s*=\s*(\S+)', line, re.IGNORECASE)
            if m:
                prog = m.group(1).strip().lower()
                if prog in ('xelatex', 'lualatex', 'pdflatex'):
                    return prog
    primary = _strip_tex_comments(tex_sources.get(main_file, ''))
    combined = primary + '\n' + '\n'.join(_strip_tex_comments(v) for k, v in sorted(tex_sources.items()) if k != main_file)
    if re.search(r'\\(?:usepackage|RequirePackage)(?:\[[^\]]*\])?\{[^}]*\b(?:luacode|luatexja|luatextra|luamplib|luapackageloader)\b[^}]*\}|\\directlua\b', combined):
        return 'lualatex'
    if re.search(
        r'\\(?:usepackage|RequirePackage)(?:\[[^\]]*\])?\{[^}]*\b(?:fontspec|unicode-math|xeCJK|xecjk|ctex|xejamo|polyglossia)\b[^}]*\}'
        r'|\\documentclass(?:\[[^\]]*\])?\{(?:ctexart|ctexrep|ctexbook|ctexbeamer)\}',
        combined,
    ):
        return 'xelatex'
    return 'pdflatex'


def _author_storage_limit() -> int:
    if MAX_AUTHOR_STORAGE != 256 * 1024 * 1024:
        return MAX_AUTHOR_STORAGE
    env_bytes = os.environ.get('EKSMU_MAX_AUTHOR_STORAGE_BYTES', '').strip()
    if env_bytes:
        return int(env_bytes)
    return MAX_AUTHOR_STORAGE


def _site_storage_limit(backend_kind: str) -> int:
    if MAX_STORAGE != MAX_STORAGE_SQLITE:
        return MAX_STORAGE
    env_bytes = os.environ.get('EKSMU_MAX_STORAGE_BYTES', '').strip()
    if env_bytes:
        return int(env_bytes)
    env_gib = os.environ.get('EKSMU_MAX_STORAGE_GIB', '').strip()
    if env_gib:
        return int(float(env_gib) * (1024 ** 3))
    return MAX_STORAGE_GCS if backend_kind == 'gcs' else MAX_STORAGE


def file_input(filename, data_b64):
    text(filename, 180)
    if filename in {'.', '..'} or any(c in filename for c in '/\\') or any(ord(c) < 32 or ord(c) == 127 for c in filename):
        raise Problem('文件名不能包含路径或控制字符')
    ext = Path(filename).suffix.lower()
    arch_ext = archive_ext(filename)
    if arch_ext is None and ext not in TEXT_EXTS | set(IMAGE_FORMATS) | {'.pdf'}:
        raise Problem('支持图片、PDF、文本、代码与小型数据文件')
    if not isinstance(data_b64, str) or len(data_b64) > ((MAX_FILE + 2) // 3) * 4:
        raise Problem('单个附件不能超过 32 MiB；文字发表不受影响', 413)
    try:
        raw = base64.b64decode(data_b64, validate=True)
    except (ValueError, TypeError) as exc:
        raise Problem('文件编码不正确') from exc
    if not raw:
        raise Problem('附件不能为空；文字发表不受影响')
    if len(raw) > MAX_FILE:
        raise Problem('单个附件不能超过 32 MiB；文字发表不受影响', 413)
    preview = None
    media = 'application/octet-stream'
    if arch_ext is not None:
        inspect_source_archive(raw, filename)
        media = 'application/octet-stream'
    elif ext in TEXT_EXTS:
        try:
            decoded = raw.decode('utf-8-sig')
            if '\x00' in decoded:
                raise ValueError
        except (UnicodeError, ValueError) as exc:
            raise Problem('文本、代码和数据附件需要UTF-8格式') from exc
        media = 'text/plain'
    elif ext == '.pdf':
        if not raw.startswith(b'%PDF-'):
            raise Problem('文件不是PDF格式')
        media = 'application/pdf'
    else:
        try:
            from PIL import Image
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with Image.open(BytesIO(raw)) as image:
                    if image.format != IMAGE_FORMATS[ext] or image.width * image.height > 16_000_000:
                        raise ValueError('image format or size')
                    image.load()
                    image.thumbnail((1400, 1400))
                    # A fresh pixel image drops metadata and encoded ancillary content.
                    clean = Image.new('RGBA', image.size)
                    clean.paste(image.convert('RGBA'))
                    output = BytesIO(); clean.save(output, format='PNG'); preview = output.getvalue()
            media = 'image/' + ('jpeg' if ext in {'.jpg', '.jpeg'} else ext[1:])
        except ImportError as exc:
            raise Problem('图片服务需要安装Pillow；仍可发表文字和上传其他材料', 503) from exc
        except Exception as exc:
            raise Problem('无法读取图片，或图片尺寸过大') from exc
    return raw, media, preview


MISSING = {'actor': '找不到对应的参与者', 'revision': '找不到对应的版本', 'exploration': '找不到对应的作品'}


class Research:
    def __init__(self, path, records, *, blobstore=None):
        self.records = records
        self.index = {r['id']: r for r in records}
        self.db = sqlite3.connect(str(path), timeout=5, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        allowed = {'research_files', 'research_tasks', 'research_events', 'research_claims',
                   'research_lean_enrollments', 'research_lean_runs', 'blobs', 'compile_jobs',
                   'research_redactions'}
        tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if tables - allowed:
            self.db.close(); raise Problem('研究材料需要独立数据库', 503)
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS research_files(
            id TEXT PRIMARY KEY, revision_id TEXT NOT NULL, actor_id TEXT NOT NULL,
            filename TEXT NOT NULL, mime TEXT NOT NULL, sha256 TEXT NOT NULL, created_at TEXT NOT NULL,
            data BLOB, preview BLOB, size INTEGER, preview_sha256 TEXT,
            origin TEXT NOT NULL DEFAULT 'upload', derived_from_sha256 TEXT,
            UNIQUE(revision_id,filename,sha256));
          CREATE TABLE IF NOT EXISTS research_tasks(
            id TEXT PRIMARY KEY, revision_id TEXT NOT NULL, actor_id TEXT NOT NULL,
            title TEXT NOT NULL, description TEXT NOT NULL, category TEXT NOT NULL,
            state TEXT NOT NULL, version INTEGER NOT NULL, created_at TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS research_events(
            id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES research_tasks(id),
            actor_id TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS research_claims(
            task_id TEXT NOT NULL REFERENCES research_tasks(id), actor_id TEXT NOT NULL,
            PRIMARY KEY(task_id,actor_id));
          CREATE TABLE IF NOT EXISTS research_lean_enrollments(
            id TEXT PRIMARY KEY, operation_id TEXT NOT NULL UNIQUE, actor_id TEXT NOT NULL,
            revision_id TEXT NOT NULL, attachment_id TEXT NOT NULL, target TEXT NOT NULL,
            problem_reference TEXT NOT NULL, request_sha256 TEXT NOT NULL,
            document TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL,
            withdrawn_at TEXT,
            UNIQUE(actor_id,revision_id,attachment_id,target,problem_reference));
          CREATE TABLE IF NOT EXISTS research_lean_runs(
            id TEXT PRIMARY KEY, enrollment_id TEXT NOT NULL UNIQUE
              REFERENCES research_lean_enrollments(id), document TEXT NOT NULL,
            created_at TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS compile_jobs(
            id TEXT PRIMARY KEY, revision_id TEXT NOT NULL, source_attachment_id TEXT NOT NULL,
            actor_id TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('queued','running','succeeded','failed','cancelled')),
            main_file TEXT NOT NULL, engine TEXT, requested_at TEXT NOT NULL,
            started_at TEXT, finished_at TEXT, execution_name TEXT, toolchain TEXT,
            output_attachment_id TEXT, log_text TEXT, error TEXT);
          CREATE TABLE IF NOT EXISTS research_redactions(
            attachment_id TEXT PRIMARY KEY REFERENCES research_files(id),
            revision_id TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            reason_code TEXT NOT NULL,
            reason TEXT NOT NULL,
            operator_id TEXT NOT NULL,
            decided_at TEXT NOT NULL);
        ''')
        ensure_research_files_schema(self.db)
        self.blobstore = create_blobstore(self.db, blobstore=blobstore)

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback(); raise

    def get_record(self, rid, kind):
        identifier(rid)
        result = self.index.get(rid)
        if not result or result['type'] != kind:
            raise Problem(MISSING.get(kind, '找不到对应的记录'), 404)
        return result

    def owner(self, actor_id, revision_id):
        self.get_record(actor_id, 'actor')
        revision = self.get_record(revision_id, 'revision')
        if revision['actor_id'] != actor_id:
            raise Problem('只有作者能添加本版材料；其他人可以评论、独立发表和创建协作', 403)
        return revision

    def lean_enroll(self, actor_id, revision_id, attachment_id, target, problem_reference,
                    operation_id, consent):
        revision = self.owner(actor_id, revision_id)
        identifier(attachment_id); identifier(operation_id)
        if not isinstance(target, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.]*', target):
            raise Problem('需要普通 Lean 目标声明名')
        text(problem_reference, 160, True)
        exact(consent, {'local_check', 'result_visibility', 'external_transfer'})
        if (type(consent['local_check']) is not bool or consent['local_check'] is not True
                or consent['result_visibility'] != 'private'
                or type(consent['external_transfer']) is not bool or consent['external_transfer'] is not False):
            raise Problem('仅接受明确授权的本站私有预检，不包含对外发送')
        file = self.db.execute('SELECT * FROM research_files WHERE id=?', (attachment_id,)).fetchone()
        if file is None or file['revision_id'] != revision_id or not file['filename'].lower().endswith('.lean'):
            raise Problem('请选择本版的一份 Lean 附件', 404)
        if self._file_size(file) > 256 * 1024:
            raise Problem('当前单文件 Lean 预检最多处理256 KiB；发表附件不受影响', 413)
        request = {'actor_id': actor_id, 'revision_id': revision_id,
                   'revision_sha256': revision['sha256'], 'attachment_id': attachment_id,
                   'attachment_sha256': file['sha256'], 'target': target,
                   'problem_reference': problem_reference.strip(), 'consent': consent}
        request_digest = sha(canonical(request).encode())
        with self.transaction():
            prior = self.db.execute('SELECT id,request_sha256 FROM research_lean_enrollments WHERE operation_id=?',
                                    (operation_id,)).fetchone()
            if prior:
                if prior['request_sha256'] != request_digest:
                    raise Problem('同一操作标识绑定了不同的报名内容', 409)
                return self.lean_enrollment(actor_id, prior['id'])
            prior = self.db.execute('''SELECT id,request_sha256 FROM research_lean_enrollments
                WHERE actor_id=? AND revision_id=? AND attachment_id=? AND target=? AND problem_reference=?''',
                (actor_id, revision_id, attachment_id, target, problem_reference.strip())).fetchone()
            if prior:
                if prior['request_sha256'] != request_digest:
                    raise Problem('这一固定材料已有不同的报名授权', 409)
                return self.lean_enrollment(actor_id, prior['id'])
            count = self.db.execute('SELECT COUNT(*) FROM research_lean_enrollments WHERE actor_id=?',
                                    (actor_id,)).fetchone()[0]
            if count >= MAX_LEAN_ENROLLMENTS_PER_ACTOR:
                raise Problem('当前身份的 Lean 任务名额已满；发表与讨论不受影响', 413)
            enrollment_id = str(uuid.uuid4())
            self.db.execute('''INSERT INTO research_lean_enrollments VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                            (enrollment_id, operation_id, actor_id, revision_id, attachment_id,
                             target, problem_reference.strip(), request_digest, canonical(request),
                             'queued', now(), None))
        return self.lean_enrollment(actor_id, enrollment_id)

    def lean_enrollment(self, actor_id, enrollment_id):
        self.get_record(actor_id, 'actor'); identifier(enrollment_id)
        row = self.db.execute('SELECT * FROM research_lean_enrollments WHERE id=?', (enrollment_id,)).fetchone()
        if row is None:
            raise Problem('找不到 Lean 报名', 404)
        if row['actor_id'] != actor_id:
            raise Problem('只能查看自己的 Lean 报名', 403)
        run = self.db.execute('SELECT document FROM research_lean_runs WHERE enrollment_id=?',
                              (enrollment_id,)).fetchone()
        return {'id': row['id'], 'created_at': row['created_at'], 'state': row['state'],
                'withdrawn_at': row['withdrawn_at'], 'request': json.loads(row['document']),
                'report': json.loads(run[0]) if run else None}

    def lean_enrollments(self, actor_id):
        self.get_record(actor_id, 'actor')
        ids = [r[0] for r in self.db.execute('SELECT id FROM research_lean_enrollments WHERE actor_id=? ORDER BY rowid DESC',
                                              (actor_id,))]
        return {'items': [self.lean_enrollment(actor_id, item) for item in ids]}

    def lean_withdraw(self, actor_id, enrollment_id):
        self.lean_enrollment(actor_id, enrollment_id)
        with self.transaction():
            self.db.execute("UPDATE research_lean_enrollments SET state='withdrawn', withdrawn_at=COALESCE(withdrawn_at,?) WHERE id=?",
                            (now(), enrollment_id))
        return self.lean_enrollment(actor_id, enrollment_id)

    def next_lean_job(self):
        """Offline-only worker input, never exposed through HTTP or ordinary export."""
        row = self.db.execute("SELECT * FROM research_lean_enrollments WHERE state='queued' ORDER BY rowid LIMIT 1").fetchone()
        if row is None:
            return None
        file = self.db.execute('SELECT * FROM research_files WHERE id=?', (row['attachment_id'],)).fetchone()
        request = json.loads(row['document'])
        if (file is None or file['revision_id'] != request['revision_id']
                or file['sha256'] != request['attachment_sha256']):
            raise Problem('Lean 任务的原始附件缺失或摘要变化', 409)
        try:
            raw = self._read_file_bytes(file)
        except Problem as exc:
            raise Problem('Lean 任务的原始附件缺失或摘要变化', 409) from exc
        if sha(raw) != file['sha256']:
            raise Problem('Lean 任务的原始附件缺失或摘要变化', 409)
        return {'id': row['id'], 'source': raw, 'request': request}

    def record_lean_run(self, enrollment_id, report):
        """Offline worker only; checks the fixed source and target before storing a private report."""
        identifier(enrollment_id)
        job = self.next_lean_job()
        if job is None or job['id'] != enrollment_id:
            raise Problem('Lean 任务不是当前队首或已撤回', 409)
        if (not isinstance(report, dict) or report.get('format') != 'eksmu.lean-local-check.v1'
                or report.get('source_sha256') != job['request']['attachment_sha256']
                or report.get('target') != job['request']['target']
                or report.get('sandbox_setup_completed') is not True
                or report.get('status') == 'environment_failed'
                or not isinstance(report.get('log'), str) or len(report['log'].encode()) > 65536):
            raise Problem('Lean 报告与报名材料不匹配')
        with self.transaction():
            self.db.execute('INSERT INTO research_lean_runs VALUES(?,?,?,?)',
                            (str(uuid.uuid4()), enrollment_id, canonical(report), now()))
            self.db.execute("UPDATE research_lean_enrollments SET state='checked' WHERE id=? AND state='queued'",
                            (enrollment_id,))
        return {'enrollment_id': enrollment_id, 'state': 'checked', 'mechanical_status': report['status'],
                'official_submission': 'not_submitted'}

    @staticmethod
    def _file_size(row) -> int:
        keys = row.keys()
        if 'size' in keys and row['size'] is not None:
            return int(row['size'])
        if row['data'] is not None:
            return len(row['data'])
        return 0

    @staticmethod
    def _has_preview(row) -> bool:
        keys = row.keys()
        if row['preview'] is not None:
            return True
        return 'preview_sha256' in keys and bool(row['preview_sha256'])

    def _read_file_bytes(self, row) -> bytes:
        if row['data'] is not None:
            raw = bytes(row['data'])
            if sha(raw) != row['sha256']:
                raise Problem('附件内容校验失败，请联系维护者', 503)
            return raw
        return self.blobstore.get(row['sha256'])

    @staticmethod
    def metadata(row):
        keys = row.keys()
        base = {k: row[k] for k in ('id', 'revision_id', 'actor_id', 'filename', 'mime', 'sha256', 'created_at')} | {
            'size': Research._file_size(row), 'has_preview': Research._has_preview(row)}
        if 'origin' in keys and row['origin'] == 'compiled':
            base['origin'] = 'compiled'
            base['derived_from_sha256'] = row['derived_from_sha256'] if 'derived_from_sha256' in keys else None
        return base

    def _enrich_compiled_item(self, item: dict) -> dict:
        if item.get('origin') != 'compiled':
            return item
        job = self.db.execute(
            'SELECT * FROM compile_jobs WHERE output_attachment_id=? ORDER BY rowid DESC LIMIT 1',
            (item['id'],),
        ).fetchone()
        src_row = None
        if job is not None:
            src_row = self.db.execute('SELECT id, filename, sha256 FROM research_files WHERE id=?', (job['source_attachment_id'],)).fetchone()
        if src_row is None and item.get('derived_from_sha256'):
            src_row = self.db.execute(
                'SELECT id, filename, sha256 FROM research_files WHERE revision_id=? AND sha256=? ORDER BY rowid LIMIT 1',
                (item['revision_id'], item['derived_from_sha256']),
            ).fetchone()
        if job is None and src_row is not None:
            job = self.db.execute(
                'SELECT * FROM compile_jobs WHERE revision_id=? AND source_attachment_id=? ORDER BY rowid DESC LIMIT 1',
                (item['revision_id'], src_row['id']),
            ).fetchone()
        tc = {}
        if job is not None and job['toolchain']:
            try:
                tc = json.loads(job['toolchain'])
            except Exception:
                tc = {}
        enriched = dict(item)
        enriched['compile_info'] = {
            'job_id': job['id'] if job is not None else None,
            'texlive': str(tc.get('texlive') or '2026'),
            'engine': (job['engine'] if job is not None and job['engine'] else 'pdflatex'),
            'engine_detail': str(tc.get('engine') or ''),
            'latexmk': str(tc.get('latexmk') or ''),
            'latexml': str(tc.get('latexml') or ''),
            'main_file': job['main_file'] if job is not None else '',
            'source_attachment_id': src_row['id'] if src_row is not None else (job['source_attachment_id'] if job is not None else None),
            'source_filename': src_row['filename'] if src_row is not None else None,
            'source_date_epoch': tc.get('source_date_epoch') if type(tc.get('source_date_epoch')) is int else None,
        }
        return enriched

    def files(self, revision_id):
        self.get_record(revision_id, 'revision')
        rows = self.db.execute('SELECT * FROM research_files WHERE revision_id=? ORDER BY rowid', (revision_id,)).fetchall()
        base_items = [self.metadata(r) for r in rows]
        redacted_map = {
            r['attachment_id']: {
                'reason_code': r['reason_code'],
                'reason': r['reason'],
                'decided_at': r['decided_at'],
            }
            for r in self.db.execute(
                'SELECT attachment_id, reason_code, reason, decided_at FROM research_redactions WHERE revision_id=?',
                (revision_id,),
            )
        }
        items = []
        for it in base_items:
            enriched = self._enrich_compiled_item(it)
            if enriched['id'] in redacted_map:
                enriched = dict(enriched, has_preview=False, redacted=redacted_map[enriched['id']])
            items.append(enriched)
        return {'items': items, 'manifest_sha256': sha(canonical(base_items).encode()),
                'manifest_scope': '附件可在发表后追加；该摘要不改变原文版本摘要。'}

    def redact_attachment(self, attachment_id: str, reason_code: str, reason: str, operator_id: str) -> dict:
        identifier(attachment_id)
        if not isinstance(reason_code, str) or reason_code not in REDACTION_REASONS:
            raise Problem('不支持的附件移除原因代码')
        text(reason, 1000)
        if not isinstance(operator_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{3,64}', operator_id):
            raise Problem('需要3至64位非秘密操作者标识，仅使用英文字母、数字、下划线或连接号')
        with self.transaction():
            row = self.db.execute('SELECT * FROM research_files WHERE id=?', (attachment_id,)).fetchone()
            if row is None:
                raise Problem('附件不存在', 404)
            decided = now()
            prev_sha = row['preview_sha256'] if 'preview_sha256' in row.keys() else None
            self.db.execute(
                'INSERT OR REPLACE INTO research_redactions(attachment_id, revision_id, sha256, reason_code, reason, operator_id, decided_at) '
                'VALUES(?,?,?,?,?,?,?)',
                (attachment_id, row['revision_id'], row['sha256'], reason_code, reason.strip(), operator_id, decided),
            )
            self.db.execute('UPDATE research_files SET data=NULL, preview=NULL, preview_sha256=NULL WHERE id=?', (attachment_id,))
            still_used = self.db.execute(
                'SELECT 1 FROM research_files WHERE sha256=? AND id NOT IN (SELECT attachment_id FROM research_redactions) LIMIT 1',
                (row['sha256'],),
            ).fetchone()
            if still_used is None:
                if self.blobstore.kind == 'sqlite':
                    self.db.execute('DELETE FROM blobs WHERE sha256=?', (row['sha256'],))
                elif hasattr(self.blobstore, 'delete'):
                    self.blobstore.delete(row['sha256'])
            if prev_sha:
                prev_used = self.db.execute(
                    'SELECT 1 FROM research_files WHERE (preview_sha256=? OR sha256=?) AND id NOT IN (SELECT attachment_id FROM research_redactions) LIMIT 1',
                    (prev_sha, prev_sha),
                ).fetchone()
                if prev_used is None and self.blobstore.kind == 'sqlite':
                    self.db.execute('DELETE FROM blobs WHERE sha256=?', (prev_sha,))
        return {
            'attachment_id': attachment_id,
            'revision_id': row['revision_id'],
            'sha256': row['sha256'],
            'reason_code': reason_code,
            'reason': reason.strip(),
            'operator_id': operator_id,
            'decided_at': decided,
            'bytes_removed': still_used is None,
        }

    def _add_file(self, actor_id, revision_id, filename, raw, media, preview, file_id=None, created_at=None,
                  *, origin='upload', derived_from_sha256=None):
        existing = self.db.execute('SELECT * FROM research_files WHERE revision_id=? AND filename=? AND sha256=?', (revision_id, filename, sha(raw))).fetchone()
        if existing:
            return self._enrich_compiled_item(self.metadata(existing))
        count = self.db.execute('SELECT COUNT(*) FROM research_files WHERE revision_id=?', (revision_id,)).fetchone()[0]
        if count >= MAX_PER_REVISION:
            raise Problem('每版本最多 16 个附件，可以在新版本继续整理；文字发表不受影响', 413)
        author_used = self.db.execute(
            'SELECT COALESCE(SUM(COALESCE(size, length(data), 0)), 0) FROM research_files WHERE actor_id=?',
            (actor_id,),
        ).fetchone()[0]
        if author_used + len(raw) > _author_storage_limit():
            raise Problem('作者附件总配额已满（上限 256 MiB）；文字发表不受影响', 413)
        site_used = self.db.execute(
            'SELECT COALESCE(SUM(COALESCE(size, length(data), 0)), 0) FROM research_files'
        ).fetchone()[0]
        if site_used + len(raw) > _site_storage_limit(self.blobstore.kind):
            raise Problem('全站附件存储配额已满；文字发表不受影响', 413)
        blob_sha = self.blobstore.put(raw)
        preview_sha = self.blobstore.put(preview) if preview is not None else None
        fid = file_id or str(uuid.uuid4())
        self.db.execute(
            'INSERT INTO research_files(id, revision_id, actor_id, filename, mime, sha256, created_at, data, preview, size, preview_sha256, origin, derived_from_sha256) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (fid, revision_id, actor_id, filename, media, blob_sha, created_at or now(), None, None, len(raw), preview_sha, origin, derived_from_sha256),
        )
        return self._enrich_compiled_item(self.metadata(self.db.execute('SELECT * FROM research_files WHERE id=?', (fid,)).fetchone()))

    @staticmethod
    def _compile_job_dict(row) -> dict:
        tc = None
        if row['toolchain']:
            try:
                tc = json.loads(row['toolchain'])
            except Exception:
                tc = None
        return {
            'id': row['id'],
            'revision_id': row['revision_id'],
            'source_attachment_id': row['source_attachment_id'],
            'actor_id': row['actor_id'],
            'state': row['state'],
            'main_file': row['main_file'],
            'engine': row['engine'],
            'requested_at': row['requested_at'],
            'started_at': row['started_at'],
            'finished_at': row['finished_at'],
            'execution_name': row['execution_name'],
            'toolchain': tc,
            'output_attachment_id': row['output_attachment_id'],
            'error': row['error'],
            'has_log': bool(row['log_text']),
        }

    def _check_compile_rate_limits(self, actor_id: str) -> None:
        active = self.db.execute(
            "SELECT COUNT(*) FROM compile_jobs WHERE actor_id=? AND state IN ('queued','running')",
            (actor_id,),
        ).fetchone()[0]
        if active >= MAX_COMPILE_ACTIVE_PER_AUTHOR:
            raise Problem('您已有 1 个编译任务正在排队或运行，请等待完成后再试', 429)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        author_daily = self.db.execute(
            'SELECT COUNT(*) FROM compile_jobs WHERE actor_id=? AND requested_at>=?',
            (actor_id, cutoff),
        ).fetchone()[0]
        if author_daily >= MAX_COMPILE_DAILY_PER_AUTHOR:
            raise Problem('每位作者每天最多发起 20 次编译；作品发表与源码下载不受影响', 429)
        site_daily = self.db.execute(
            'SELECT COUNT(*) FROM compile_jobs WHERE requested_at>=?',
            (cutoff,),
        ).fetchone()[0]
        if site_daily >= MAX_COMPILE_DAILY_SITE:
            raise Problem('全站今日编译配额已满（每天最多 200 次）；作品发表与源码下载不受影响', 429)

    def _queue_compile_locked(self, actor_id: str, revision_id: str, attachment_row, raw: bytes,
                              main_file: str | None = None, *, strict_limits: bool = True) -> dict | None:
        tex_sources = inspect_source_archive(raw, attachment_row['filename'])
        if main_file is not None and main_file != '':
            if not isinstance(main_file, str):
                raise Problem('指定的主文件不在源码包中或不是 .tex 文件')
            chosen = _validate_archive_rel_path(main_file.strip())
            if chosen not in tex_sources:
                raise Problem('指定的主文件不在源码包中或不是 .tex 文件')
        else:
            chosen = select_main_tex(tex_sources, attachment_row['filename'])
        engine = detect_tex_engine(tex_sources, chosen)

        # Deduplicate if this exact source archive + main_file already succeeded and output PDF still exists
        succeeded = self.db.execute(
            "SELECT * FROM compile_jobs WHERE source_attachment_id=? AND main_file=? AND state='succeeded' ORDER BY rowid DESC LIMIT 1",
            (attachment_row['id'], chosen),
        ).fetchone()
        if succeeded is not None and succeeded['output_attachment_id']:
            out_exists = self.db.execute(
                'SELECT id FROM research_files WHERE id=?',
                (succeeded['output_attachment_id'],),
            ).fetchone()
            if out_exists is not None:
                return self._compile_job_dict(succeeded)

        # Also return existing active job if identical source_attachment_id + main_file is already queued/running
        active_same = self.db.execute(
            "SELECT * FROM compile_jobs WHERE source_attachment_id=? AND main_file=? AND state IN ('queued','running') ORDER BY rowid DESC LIMIT 1",
            (attachment_row['id'], chosen),
        ).fetchone()
        if active_same is not None:
            return self._compile_job_dict(active_same)

        try:
            self._check_compile_rate_limits(actor_id)
        except Problem:
            if strict_limits:
                raise
            return None

        job_id = str(uuid.uuid4())
        req_at = now()
        self.db.execute(
            'INSERT INTO compile_jobs(id, revision_id, source_attachment_id, actor_id, state, main_file, engine, '
            'requested_at, started_at, finished_at, execution_name, toolchain, output_attachment_id, log_text, error) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (job_id, revision_id, attachment_row['id'], actor_id, 'queued', chosen, engine,
             req_at, None, None, None, None, None, None, None),
        )
        row = self.db.execute('SELECT * FROM compile_jobs WHERE id=?', (job_id,)).fetchone()
        return self._compile_job_dict(row)

    def inspect_source(self, actor_id, filename, data_b64):
        self.get_record(actor_id, 'actor')
        text(filename, 180)
        if filename in {'.', '..'} or any(c in filename for c in '/\\') or any(ord(c) < 32 or ord(c) == 127 for c in filename):
            raise Problem('文件名不能包含路径或控制字符')
        if not is_source_archive_filename(filename):
            raise Problem('只支持 LaTeX 源码包（.zip、.tar.gz、.tgz、.tar）')
        if not isinstance(data_b64, str) or len(data_b64) > ((MAX_FILE + 2) // 3) * 4:
            raise Problem('单个附件不能超过 32 MiB；文字发表不受影响', 413)
        try:
            raw = base64.b64decode(data_b64, validate=True)
        except (ValueError, TypeError) as exc:
            raise Problem('文件编码不正确') from exc
        if not raw:
            raise Problem('附件不能为空；文字发表不受影响')
        if len(raw) > MAX_FILE:
            raise Problem('单个附件不能超过 32 MiB；文字发表不受影响', 413)

        tex_sources = inspect_source_archive(raw, filename)
        main_file = select_main_tex(tex_sources, filename)
        from .manuscript import flatten_latex
        tex_text, used_files = flatten_latex(tex_sources, main_file)
        if len(tex_text) > 200_000:
            tex_text = tex_text[:200_000]
        return {
            'main_file': main_file,
            'tex': tex_text,
            'files': used_files,
        }

    def upload(self, actor_id, revision_id, filename, data_b64):
        self.owner(actor_id, revision_id)
        raw, media, preview = file_input(filename, data_b64)
        with self.transaction():
            existing = self.db.execute(
                'SELECT id FROM research_files WHERE revision_id=? AND filename=? AND sha256=?',
                (revision_id, filename, sha(raw)),
            ).fetchone()
            item = self._add_file(actor_id, revision_id, filename, raw, media, preview)
            if existing is None and is_source_archive_filename(filename) and is_compile_enabled():
                att_row = self.db.execute('SELECT * FROM research_files WHERE id=?', (item['id'],)).fetchone()
                self._queue_compile_locked(actor_id, revision_id, att_row, raw, main_file=None, strict_limits=False)
            return item

    def request_compile(self, actor_id, revision_id, attachment_id, main_file=None):
        self.owner(actor_id, revision_id)
        identifier(attachment_id)
        if main_file is not None and main_file != '':
            text(main_file, 240)
        if not is_compile_enabled():
            raise Problem('本站编译暂未开放', 503)
        att_row = self.db.execute('SELECT * FROM research_files WHERE id=?', (attachment_id,)).fetchone()
        if att_row is None or att_row['revision_id'] != revision_id or not is_source_archive_filename(att_row['filename']):
            raise Problem('请选择本版的一份 LaTeX 源码包', 404)
        raw = self._read_file_bytes(att_row)
        with self.transaction():
            return self._queue_compile_locked(actor_id, revision_id, att_row, raw, main_file=main_file, strict_limits=True)

    def compile_workspace(self, revision_id, viewer_id=None):
        revision = self.get_record(revision_id, 'revision')
        enabled = is_compile_enabled()
        rows = self.db.execute('SELECT * FROM research_files WHERE revision_id=? ORDER BY rowid', (revision_id,)).fetchall()
        archives = []
        for r in rows:
            if not is_source_archive_filename(r['filename']):
                continue
            tex_files = []
            default_main = ''
            try:
                raw = self._read_file_bytes(r)
                tex_sources = inspect_source_archive(raw, r['filename'])
                default_main = select_main_tex(tex_sources, r['filename'])
                tex_files = list_tex_candidates(tex_sources, r['filename'])
            except Exception:
                pass
            jobs = [
                self._compile_job_dict(j)
                for j in self.db.execute(
                    'SELECT * FROM compile_jobs WHERE source_attachment_id=? ORDER BY rowid DESC',
                    (r['id'],),
                )
            ]
            # Hide failure error detail from non-authors; only author sees failure detail/log
            if viewer_id != revision['actor_id']:
                for j in jobs:
                    if j['state'] == 'failed':
                        j['error'] = None
                        j['has_log'] = False
            archives.append({
                'attachment_id': r['id'],
                'filename': r['filename'],
                'sha256': r['sha256'],
                'default_main_file': default_main,
                'tex_files': tex_files,
                'latest_job': jobs[0] if jobs else None,
                'jobs': jobs,
            })
        return {
            'enabled': enabled,
            'notice': None if enabled else '本站编译暂未开放',
            'archives': archives,
        }

    def compile_log(self, actor_id, job_id):
        self.get_record(actor_id, 'actor')
        identifier(job_id)
        row = self.db.execute('SELECT * FROM compile_jobs WHERE id=?', (job_id,)).fetchone()
        if row is None:
            raise Problem('找不到编译任务', 404)
        if row['actor_id'] != actor_id:
            raise Problem('只有作者能查看编译日志', 403)
        job = self._compile_job_dict(row)
        job['log_text'] = row['log_text'] or ''
        return job

    def download_stream(self, attachment_id, preview=False, chunk_size=DEFAULT_CHUNK_SIZE):
        identifier(attachment_id)
        row = self.db.execute('SELECT * FROM research_files WHERE id=?', (attachment_id,)).fetchone()
        if row is None:
            raise Problem('附件不存在', 404)
        self.get_record(row['revision_id'], 'revision')
        red = self.db.execute('SELECT reason FROM research_redactions WHERE attachment_id=?', (attachment_id,)).fetchone()
        if red is not None:
            raise Problem('该附件因隐私或合规原因已移除', 410)
        if preview:
            if row['preview'] is not None:
                raw = bytes(row['preview'])
                return len(raw), iter_bytes(raw, chunk_size), 'image/png', row['filename']
            preview_sha = row['preview_sha256'] if 'preview_sha256' in row.keys() else None
            if not preview_sha:
                raise Problem('该文件没有图片预览', 404)
            total_size, chunks = self.blobstore.open_stream(preview_sha, chunk_size=chunk_size)
            return total_size, chunks, 'image/png', row['filename']
        if row['data'] is not None:
            raw = bytes(row['data'])
            if sha(raw) != row['sha256']:
                raise Problem('附件内容校验失败，请联系维护者', 503)
            return len(raw), iter_bytes(raw, chunk_size), 'application/octet-stream', row['filename']
        total_size, chunks = self.blobstore.open_stream(row['sha256'], chunk_size=chunk_size)
        return total_size, chunks, 'application/octet-stream', row['filename']

    def download(self, attachment_id, preview=False):
        _, chunks, media, filename = self.download_stream(attachment_id, preview=preview)
        return b''.join(chunks), media, filename

    def export_materials(self, revision_id):
        listing = self.files(revision_id)
        return {'format': FILE_FORMAT, 'revision_id': revision_id,
                'revision_sha256': self.get_record(revision_id, 'revision')['sha256'],
                'items': [dict(meta, data_b64=base64.b64encode(self.download(meta['id'])[0]).decode()) for meta in listing['items']]}

    def import_materials(self, actor_id, bundle):
        exact(bundle, {'format', 'revision_id', 'revision_sha256', 'items'})
        revision = self.owner(actor_id, bundle['revision_id'])
        if bundle['format'] != FILE_FORMAT or bundle['revision_sha256'] != revision['sha256']:
            raise Problem('请先恢复相同的原文版本')
        if type(bundle['items']) is not list or len(bundle['items']) > MAX_PER_REVISION:
            raise Problem('材料清单过大')
        checked = []
        ids = set()
        for item in bundle['items']:
            exact(item, {'id','revision_id','actor_id','filename','mime','sha256','created_at','size','has_preview','data_b64'})
            identifier(item['id'])
            if item['id'] in ids or item['revision_id'] != revision['id'] or item['actor_id'] != actor_id:
                raise Problem('材料归属或标识不一致')
            ids.add(item['id'])
            raw, mime, preview = file_input(item['filename'], item['data_b64'])
            text(item['created_at'], 64)
            try:
                timestamp = datetime.fromisoformat(item['created_at'])
                if timestamp.tzinfo is None: raise ValueError
            except (TypeError, ValueError):
                raise Problem('材料时间记录不正确')
            if sha(raw) != item['sha256'] or len(raw) != item['size'] or mime != item['mime'] or (preview is not None) != item['has_preview']:
                raise Problem('材料校验不匹配')
            checked.append((item, raw, mime, preview))
        with self.transaction():
            for item, raw, mime, preview in checked:
                prior = self.db.execute('SELECT * FROM research_files WHERE id=?', (item['id'],)).fetchone()
                if prior is not None:
                    if self.metadata(prior) != {k:v for k,v in item.items() if k!='data_b64'}:
                        raise Problem('材料标识冲突，不覆盖现有文件', 409)
                    continue
                duplicate = self.db.execute('SELECT id FROM research_files WHERE revision_id=? AND filename=? AND sha256=?', (revision['id'],item['filename'],item['sha256'])).fetchone()
                if duplicate and duplicate['id'] != item['id']:
                    raise Problem('相同材料已有不同标识，请保留现有记录并核对来源',409)
                self._add_file(actor_id, revision['id'], item['filename'], raw, mime, preview, item['id'], item['created_at'])
        return self.files(revision['id'])

    def diff(self, before_id, after_id):
        before = self.get_record(before_id, 'revision'); after = self.get_record(after_id, 'revision')
        if before['payload']['exploration_id'] != after['payload']['exploration_id']:
            raise Problem('只能比较同一项作品的版本')
        lines = list(difflib.unified_diff(before['payload']['body'].splitlines(), after['payload']['body'].splitlines(), fromfile='before', tofile='after', lineterm=''))
        old, new = before['payload'].get('paper'), after['payload'].get('paper')
        changes = [{'field': key, 'before': (old or {}).get(key), 'after': (new or {}).get(key)}
                   for key in ('format', 'abstract', 'authors', 'references', 'license', 'provenance')
                   if (old or {}).get(key) != (new or {}).get(key)]
        return {'before_id': before_id, 'after_id': after_id, 'lines': lines[:2000], 'truncated': len(lines)>2000,
                'title_before': before['payload']['title'], 'title_after': after['payload']['title'], 'paper_changes': changes}

    def create_task(self, actor_id, revision_id, title, description, category):
        self.get_record(actor_id, 'actor'); self.get_record(revision_id, 'revision')
        text(title, 160); text(description, 4000, True)
        if not isinstance(category, str) or category not in CATEGORIES:
            raise Problem('未知协作类型')
        work_id = self.index[revision_id]['payload']['exploration_id']
        revision_ids = {r['id'] for r in self.records if r['type']=='revision' and r['payload']['exploration_id']==work_id}
        with self.transaction():
            count = sum(r[0] in revision_ids for r in self.db.execute('SELECT revision_id FROM research_tasks'))
            if count >= MAX_TASKS_PER_WORK: raise Problem('这项作品的协作任务名额已满', 413)
            tid = str(uuid.uuid4())
            self.db.execute('INSERT INTO research_tasks VALUES(?,?,?,?,?,?,?,?,?)', (tid,revision_id,actor_id,title,description,category,'open',1,now()))
            self._event(tid, actor_id, 'created', description)
        return self.task(tid)

    def _event(self, task_id, actor_id, kind, body):
        self.db.execute('INSERT INTO research_events VALUES(?,?,?,?,?,?)', (str(uuid.uuid4()),task_id,actor_id,kind,body,now()))

    def task(self, task_id):
        identifier(task_id)
        row = self.db.execute('SELECT * FROM research_tasks WHERE id=?',(task_id,)).fetchone()
        if row is None: raise Problem('协作任务不存在',404)
        self.get_record(row['revision_id'],'revision')
        events = [dict(r) for r in self.db.execute('SELECT * FROM research_events WHERE task_id=? ORDER BY rowid',(task_id,))]
        members = [r[0] for r in self.db.execute('SELECT actor_id FROM research_claims WHERE task_id=? ORDER BY rowid',(task_id,))]
        return dict(row) | {'participants':members,'events':events}

    def tasks(self, revision_id):
        self.get_record(revision_id, 'revision')
        return {'items':[self.task(r[0]) for r in self.db.execute('SELECT id FROM research_tasks WHERE revision_id=? ORDER BY rowid',(revision_id,))]}

    def collaboration_notifications(self, actor_id):
        """Derive each event's audience at that point in the task history."""
        self.get_record(actor_id, 'actor')
        notices = []
        for task in self.db.execute('SELECT * FROM research_tasks ORDER BY rowid'):
            revision = self.get_record(task['revision_id'], 'revision')
            work_id = revision['payload']['exploration_id']
            work = self.get_record(work_id, 'exploration')
            self.get_record(task['actor_id'], 'actor')
            members = set()
            for event in self.db.execute('SELECT * FROM research_events WHERE task_id=? ORDER BY rowid', (task['id'],)):
                author = self.get_record(event['actor_id'], 'actor')
                if event['kind'] == 'join': members.add(event['actor_id'])
                elif event['kind'] == 'leave': members.discard(event['actor_id'])
                elif event['kind'] not in {'created', 'progress', 'close', 'reopen'}:
                    raise Problem('未知协作事件', 503)
                audience = members | {task['actor_id'], work['actor_id']}
                if actor_id not in audience or actor_id == event['actor_id']:
                    continue
                notices.append({'id': event['id'], 'exploration_id': work_id,
                                'revision_id': revision['id'], 'type': 'collaboration',
                                'action': event['kind'], 'reason': 'collaboration',
                                'from': author['payload']['alias'], 'title': task['title'],
                                'created_at': event['created_at'],
                                'excerpt': (event['body'] or task['description'])[:160]})
        return notices

    def task_action(self, actor_id, task_id, action, body='', expected_version=None):
        self.get_record(actor_id,'actor'); text(body,4000,True)
        if action not in {'join','leave','progress','close','reopen'}: raise Problem('未知协作操作')
        with self.transaction():
            task = self.task(task_id)
            if expected_version is not None and (type(expected_version) is not int or expected_version!=task['version']):
                raise Problem('任务已有新进展，请刷新后再操作',409)
            if action in {'close','reopen'}:
                if task['actor_id']!=actor_id: raise Problem('只有发起者能结束或重开自己提出的协作',403)
                if expected_version is None: raise Problem('需要任务版本')
                state='closed' if action=='close' else 'open'
                if state==task['state']: return task
            elif task['state']=='closed' and action!='leave':
                raise Problem('这个任务已结束，可以重新提出新的协作',409)
            if action=='join' and actor_id in task['participants']: return task
            if action=='leave' and actor_id not in task['participants']: return task
            if action=='progress':
                if actor_id not in task['participants']: raise Problem('先加入这项协作，再记录自己的进展',403)
                text(body,4000)
            if len(task['events'])>=MAX_EVENTS_PER_TASK: raise Problem('本地协作记录配额已满',413)
            if action=='join': self.db.execute('INSERT INTO research_claims VALUES(?,?)',(task_id,actor_id))
            elif action=='leave': self.db.execute('DELETE FROM research_claims WHERE task_id=? AND actor_id=?',(task_id,actor_id))
            elif action in {'close','reopen'}: self.db.execute('UPDATE research_tasks SET state=? WHERE id=?',(state,task_id))
            self._event(task_id,actor_id,action,body)
            self.db.execute('UPDATE research_tasks SET version=version+1 WHERE id=?',(task_id,))
        return self.task(task_id)
