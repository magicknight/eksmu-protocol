"""Offline/timer worker for the isolated LaTeX compilation queue (Cloud Run Job + dedicated GCS bucket).

Designed to run every 2 minutes alongside a live server via ``python -m eksmu --db ... run-compile-queue``.
Database writes acquire ``RequestLock(db)`` so the HTTP server and worker never contend on SQLite writes.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
from html import escape as html_escape
from html.parser import HTMLParser
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
import time
import urllib.error
from urllib.parse import quote
import urllib.request

from .blobstore import (
    GCS_API_ENDPOINT,
    MAX_FILE,
    METADATA_TOKEN_URL,
    md5_b64,
    sha256_hex,
)
from .maintenance import InstanceLock, RequestLock, local_path
from .research import (
    MAX_COMPILE_LOG_CHARS,
    Research,
    is_compile_enabled,
    now,
    sha,
)
from .store import Problem, Store, canonical

CLOUD_RUN_API_ENDPOINT = 'https://run.googleapis.com'
COMPILE_JOB_TIMEOUT_SECONDS = 35 * 60  # 35 minutes
MAX_LOG_DOWNLOAD_BYTES = 2 * 1024 * 1024  # 2 MiB download cap before 256 KiB truncation
MAX_HTML_BYTES = 16 * 1024 * 1024  # 16 MiB cap for compiled HTML

_DROP_CONTENT_TAGS = frozenset({
    'script', 'iframe', 'object', 'applet', 'frame', 'frameset',
    'noscript', 'foreignobject', 'audio', 'video', 'canvas', 'template',
    'animate', 'set', 'animatemotion', 'animatetransform', 'handler', 'listener',  # SVG can animate href to javascript:
})
_DROP_ONLY_TAGS = frozenset({
    'embed', 'form', 'input', 'button', 'select', 'textarea', 'option',
    'optgroup', 'fieldset', 'base', 'link', 'param', 'source', 'track',
})
_VOID_HTML_TAGS = frozenset({
    'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
    'link', 'meta', 'param', 'source', 'track', 'wbr',
})
_FORBIDDEN_ATTR_NAMES = frozenset({
    'srcdoc', 'formaction', 'ping', 'background', 'dynsrc', 'lowsrc',
    'codebase', 'classid', 'archive', 'srcset', 'usemap', 'longdesc',
})
_URL_ATTR_NAMES = frozenset({'href', 'xlink:href', 'src', 'action', 'cite', 'poster', 'data'})
_SAFE_NAME_RE = re.compile(r'[a-z][a-z0-9:_.-]*')  # html.parser accepts quotes etc. in names; never echo those
_DATA_IMAGE_RE = re.compile(
    r'^data:image/(png|jpeg|jpg|gif|webp|svg\+xml);base64,([A-Za-z0-9+/=\s]+)$',
    re.IGNORECASE,
)
_DANGEROUS_CSS_RE = re.compile(
    r'@import\b[^;{}]*;?'
    r'|url\s*\([^)]*\)'
    r'|expression\s*\([^)]*\)'
    r'|(?:javascript|vbscript)\s*:'
    r'|-moz-binding\s*:[^;{}]*'
    r'|behavior\s*:[^;{}]*',
    re.IGNORECASE,
)


def _sanitize_css_text(css: str) -> str:
    cleaned = re.sub(r'/\*[\s\S]*?\*/', '', css or '')
    cleaned = _DANGEROUS_CSS_RE.sub('', cleaned)
    return cleaned


def _is_safe_data_image_uri(uri: str) -> bool:
    s = (uri or '').strip()
    m = _DATA_IMAGE_RE.match(s)
    if not m:
        return False
    subtype = m.group(1).lower()
    b64_part = re.sub(r'\s+', '', m.group(2))
    try:
        raw = base64.b64decode(b64_part, validate=False)
    except Exception:
        return False
    if not raw:
        return False
    if subtype == 'svg+xml':
        svg_text = raw.decode('utf-8', errors='replace').lower()
        if (
            '<script' in svg_text
            or '<iframe' in svg_text
            or '<foreignobject' in svg_text
            or '<object' in svg_text
            or '<embed' in svg_text
            or 'javascript:' in svg_text
            or 'vbscript:' in svg_text
            or 'data:text/html' in svg_text
            or re.search(r'\bon[a-z0-9_]+\s*=', svg_text)
        ):
            return False
    return True


def _is_safe_link_href(value: str) -> bool:
    norm = re.sub(r'[\x00-\x20\x7f]+', '', value or '').lower()
    if not norm:
        return True
    if norm.startswith(('javascript:', 'vbscript:', 'data:', 'file:', 'blob:', 'filesystem:', 'about:')):
        return False
    return True


class _CompiledHtmlSanitizer(HTMLParser):
    """Server-side stdlib HTMLParser sanitizer for compiled HTML before database/blobstore persistence."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._out: list[str] = []
        self._drop_stack: list[str] = []
        self._in_style: bool = False
        self._style_buf: list[str] = []

    def handle_decl(self, decl: str) -> None:
        if not self._drop_stack and decl.strip().lower().startswith('doctype html'):
            self._out.append('<!DOCTYPE html>')

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if self._drop_stack:
            if t in _DROP_CONTENT_TAGS and t not in _VOID_HTML_TAGS:
                self._drop_stack.append(t)
            return
        if not _SAFE_NAME_RE.fullmatch(t):
            return
        if t in _DROP_CONTENT_TAGS:
            if t not in _VOID_HTML_TAGS:
                self._drop_stack.append(t)
            return
        if t in _DROP_ONLY_TAGS:
            return
        if t == 'meta':
            if any((k or '').lower() == 'http-equiv' for k, _ in attrs):
                return
        if t == 'img':
            src_val = None
            for k, v in attrs:
                if (k or '').lower() == 'src' and v is not None:
                    src_val = v.strip()
                    break
            if not src_val or not _is_safe_data_image_uri(src_val):
                return

        clean_attrs: list[str] = []
        for raw_k, raw_v in attrs:
            if not raw_k:
                continue
            k_low = raw_k.lower()
            if not _SAFE_NAME_RE.fullmatch(k_low) or k_low.startswith('on') or k_low in _FORBIDDEN_ATTR_NAMES:
                continue
            val = '' if raw_v is None else raw_v
            if k_low == 'style':
                val = _sanitize_css_text(val)
                if not val.strip():
                    continue
            elif k_low in _URL_ATTR_NAMES:
                if t == 'img' and k_low == 'src':
                    val = re.sub(r'\s+', '', val.strip())
                    if not _is_safe_data_image_uri(val):
                        return
                elif k_low == 'src':
                    continue
                elif t == 'use' and k_low in ('href', 'xlink:href'):
                    if not val.strip().startswith('#'):
                        continue
                else:
                    if not _is_safe_link_href(val):
                        continue
            clean_attrs.append(f' {k_low}="{html_escape(val, quote=True)}"')

        self._out.append(f'<{t}{"".join(clean_attrs)}>')
        if t == 'style':
            self._in_style = True
            self._style_buf = []

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if self._drop_stack:
            if t in self._drop_stack:
                while self._drop_stack:
                    popped = self._drop_stack.pop()
                    if popped == t:
                        break
            return
        if t in _DROP_CONTENT_TAGS or t in _DROP_ONLY_TAGS or t in _VOID_HTML_TAGS or not _SAFE_NAME_RE.fullmatch(t):
            return
        if t == 'style' and self._in_style:
            css_clean = _sanitize_css_text(''.join(self._style_buf))
            self._out.append(css_clean)
            self._in_style = False
            self._style_buf = []
        self._out.append(f'</{t}>')

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        self.handle_starttag(tag, attrs)
        if t not in _VOID_HTML_TAGS:
            self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._drop_stack:
            return
        if self._in_style:
            self._style_buf.append(data)
            return
        self._out.append(html_escape(data, quote=False))

    def handle_entityref(self, name: str) -> None:
        if self._drop_stack or self._in_style:
            return
        self._out.append(f'&{name};')

    def handle_charref(self, name: str) -> None:
        if self._drop_stack or self._in_style:
            return
        self._out.append(f'&#{name};')

    def handle_comment(self, data: str) -> None:
        return

    def get_html(self) -> str:
        return ''.join(self._out)


def sanitize_compiled_html(html_text: str) -> str:
    """Sanitize compiled HTML using stdlib html.parser before storing in Eksmu."""
    parser = _CompiledHtmlSanitizer()
    parser.feed(html_text or '')
    parser.close()
    return parser.get_html()


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _revision_source_date_epoch(rev_row: dict | None) -> int | None:
    if not isinstance(rev_row, dict):
        return None
    dt = _parse_iso(rev_row.get('created_at'))
    if dt is None:
        return None
    epoch = int(dt.timestamp())
    return epoch if epoch >= 0 else None


def _truncate_log(raw_bytes: bytes) -> str:
    limit_bytes = MAX_COMPILE_LOG_CHARS
    trimmed = raw_bytes[:limit_bytes]
    return trimmed.decode('utf-8', errors='replace')


class CloudRunCompileClient:
    """Stdlib urllib client for the dedicated compile GCS bucket and Cloud Run Admin API v2."""

    def __init__(
        self,
        *,
        project: str,
        region: str,
        job_name: str,
        compile_bucket: str,
        attachment_bucket: str = '',
        gcs_api_endpoint: str = GCS_API_ENDPOINT,
        run_api_endpoint: str = CLOUD_RUN_API_ENDPOINT,
        token_url: str = METADATA_TOKEN_URL,
        urlopen=None,
        timeout: float = 30.0,
    ):
        self.project = project.strip()
        self.region = region.strip()
        self.job_name = job_name.strip()
        self.compile_bucket = compile_bucket.strip()
        self.attachment_bucket = attachment_bucket.strip()
        if not self.project or not self.region or not self.job_name or not self.compile_bucket:
            raise Problem('缺少云端编译配置（EKSMU_COMPILE_PROJECT / REGION / JOB / BUCKET）', 503)
        if self.attachment_bucket and self.compile_bucket == self.attachment_bucket:
            raise Problem('编译存储桶不能复用正式附件存储桶', 503)
        self.gcs_api_endpoint = gcs_api_endpoint.rstrip('/')
        self.run_api_endpoint = run_api_endpoint.rstrip('/')
        self.token_url = token_url
        self._urlopen = urlopen or urllib.request.urlopen
        self.timeout = timeout
        self._cached_token: str | None = None
        self._token_expiry: float = 0.0

    def _get_token(self, force_refresh: bool = False) -> str:
        now_mono = time.monotonic()
        if not force_refresh and self._cached_token and now_mono < self._token_expiry:
            return self._cached_token
        req = urllib.request.Request(
            self.token_url,
            headers={'Metadata-Flavor': 'Google'},
            method='GET',
        )
        try:
            with self._urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read(65536)
            doc = json.loads(raw.decode('utf-8'))
            token = doc.get('access_token')
            expires_in = float(doc.get('expires_in', 300))
            if not token or not isinstance(token, str):
                raise ValueError('missing access_token')
        except Exception as exc:
            raise Problem('无法获取云端编译访问令牌', 503) from exc
        self._cached_token = token
        self._token_expiry = now_mono + max(10.0, expires_in - 60.0)
        return token

    def _gcs_meta_url(self, object_name: str) -> str:
        b_enc = quote(self.compile_bucket, safe='')
        o_enc = quote(object_name, safe='')
        return f'{self.gcs_api_endpoint}/storage/v1/b/{b_enc}/o/{o_enc}'

    def _gcs_upload_url(self, object_name: str) -> str:
        b_enc = quote(self.compile_bucket, safe='')
        o_enc = quote(object_name, safe='')
        return f'{self.gcs_api_endpoint}/upload/storage/v1/b/{b_enc}/o?uploadType=media&name={o_enc}'

    def upload_source(self, job_id: str, raw: bytes) -> None:
        token = self._get_token()
        object_name = f'in/{job_id}.src'
        req = urllib.request.Request(
            self._gcs_upload_url(object_name),
            data=raw,
            headers={
                'Authorization': f'Bearer {token}',
                'Content-Type': 'application/octet-stream',
                'Content-Length': str(len(raw)),
            },
            method='POST',
        )
        try:
            with self._urlopen(req, timeout=self.timeout) as resp:
                body = resp.read(65536)
            if body:
                meta = json.loads(body.decode('utf-8'))
                if isinstance(meta, dict) and meta.get('md5Hash') and meta['md5Hash'] != md5_b64(raw):
                    raise Problem('编译源码包上传校验失败', 503)
        except Problem:
            raise
        except Exception as exc:
            raise Problem('无法上传源码包到编译存储桶', 503) from exc

    def download_object(self, object_name: str, *, allow_404: bool = False, max_bytes: int = MAX_FILE) -> bytes | None:
        token = self._get_token()
        url = self._gcs_meta_url(object_name) + '?alt=media'
        req = urllib.request.Request(
            url,
            headers={'Authorization': f'Bearer {token}'},
            method='GET',
        )
        try:
            with self._urlopen(req, timeout=self.timeout) as resp:
                data = resp.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise Problem('编译产物超过大小上限', 503)
            return data
        except urllib.error.HTTPError as exc:
            if allow_404 and exc.code == 404:
                return None
            raise Problem(f'读取编译存储桶失败 (HTTP {exc.code})', 503) from exc
        except Problem:
            raise
        except Exception as exc:
            raise Problem('读取编译存储桶失败', 503) from exc

    def run_job(
        self,
        job_id: str,
        main_file: str,
        source_filename: str,
        *,
        source_date_epoch: int | None = None,
    ) -> str:
        token = self._get_token()
        p_enc = quote(self.project, safe='')
        r_enc = quote(self.region, safe='')
        j_enc = quote(self.job_name, safe='')
        url = f'{self.run_api_endpoint}/v2/projects/{p_enc}/locations/{r_enc}/jobs/{j_enc}:run'
        env_items = [
            {'name': 'JOB_ID', 'value': job_id},
            {'name': 'MAIN_FILE', 'value': main_file},
            {'name': 'COMPILE_BUCKET', 'value': self.compile_bucket},
            {'name': 'SOURCE_FILENAME', 'value': source_filename},
        ]
        if source_date_epoch is not None and not isinstance(source_date_epoch, bool) and int(source_date_epoch) >= 0:
            env_items.append({'name': 'SOURCE_DATE_EPOCH', 'value': str(int(source_date_epoch))})
        payload = {
            'overrides': {
                'containerOverrides': [
                    {
                        'env': env_items
                    }
                ]
            }
        }
        raw_body = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(
            url,
            data=raw_body,
            headers={
                'Authorization': f'Bearer {token}',
                'Content-Type': 'application/json',
                'Content-Length': str(len(raw_body)),
            },
            method='POST',
        )
        try:
            with self._urlopen(req, timeout=self.timeout) as resp:
                doc = json.loads(resp.read(262144).decode('utf-8'))
        except Exception as exc:
            raise Problem('调用 Cloud Run Job 失败', 503) from exc
        exec_name = (
            (doc.get('metadata') or {}).get('name')
            or (doc.get('response') or {}).get('name')
            or doc.get('name')
            or ''
        )
        return str(exec_name)

    def get_execution(self, execution_name: str) -> dict | None:
        if not execution_name:
            return None
        token = self._get_token()
        path = execution_name.lstrip('/')
        url = f'{self.run_api_endpoint}/v2/{path}'
        req = urllib.request.Request(
            url,
            headers={'Authorization': f'Bearer {token}'},
            method='GET',
        )
        try:
            with self._urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read(262144).decode('utf-8'))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            return None
        except Exception:
            return None


def _execution_failed(exec_doc: dict | None) -> bool:
    if not isinstance(exec_doc, dict):
        return False
    if int(exec_doc.get('failedCount') or 0) > 0:
        return True
    for cond in exec_doc.get('conditions') or []:
        if isinstance(cond, dict) and cond.get('type') in {'Completed', 'ResourcesAvailable'}:
            if cond.get('state') == 'CONDITION_FAILED':
                return True
    return False


def _prepare_validated_html(html_info: dict, html_bytes: bytes | None, default_stem: str) -> tuple[str, bytes] | None:
    if not isinstance(html_info, dict) or html_info.get('status') != 'succeeded' or not html_bytes:
        return None
    expected_sha = html_info.get('sha256')
    if (
        not isinstance(expected_sha, str)
        or not re.fullmatch(r'[0-9a-f]{64}', expected_sha)
        or sha256_hex(html_bytes) != expected_sha
        or len(html_bytes) > MAX_HTML_BYTES
    ):
        return None
    raw_name = str(html_info.get('filename') or f'{default_stem}.html')
    clean_name = PurePosixPath(raw_name).name
    if not clean_name.lower().endswith('.html'):
        clean_name = f'{default_stem}.html'
    sanitized_text = sanitize_compiled_html(html_bytes.decode('utf-8', errors='replace'))
    sanitized_bytes = sanitized_text.encode('utf-8')
    if not sanitized_bytes or len(sanitized_bytes) > MAX_HTML_BYTES:
        return None
    return clean_name, sanitized_bytes


def _apply_result_to_db(
    db_path: Path,
    research: Research,
    job_row,
    result_doc: dict,
    pdf_bytes: bytes | None,
    log_bytes: bytes,
    html_bytes: bytes | None = None,
) -> dict:
    """Validate result_doc + pdf_bytes (+ optional html_bytes) and update compile_jobs & research_files."""
    job_id = job_row['id']
    log_text = _truncate_log(log_bytes)
    engine = str(result_doc.get('engine') or job_row['engine'] or 'pdflatex')
    toolchain_obj = dict(result_doc.get('toolchain')) if isinstance(result_doc.get('toolchain'), dict) else {}
    html_info = result_doc.get('html') if isinstance(result_doc.get('html'), dict) else {}
    if html_info.get('latexml_version') and not toolchain_obj.get('latexml'):
        toolchain_obj['latexml'] = str(html_info['latexml_version'])
    # Dispatch passes the same value to the Job (run_job / compile_archive_bytes); recorded so pages only
    # promise a reproducible PDF for builds that were made with it.
    epoch = _revision_source_date_epoch(research.index.get(job_row['revision_id']))
    if epoch is not None:
        toolchain_obj['source_date_epoch'] = epoch
    toolchain_json = canonical(toolchain_obj)
    finished = now()

    log_expected_sha = result_doc.get('log_sha256')
    if log_bytes and isinstance(log_expected_sha, str) and re.fullmatch(r'[0-9a-f]{64}', log_expected_sha):
        if sha256_hex(log_bytes) != log_expected_sha:
            with RequestLock(db_path):
                with research.transaction():
                    research.db.execute(
                        "UPDATE compile_jobs SET state='failed', finished_at=?, engine=?, toolchain=?, log_text=?, error=? WHERE id=?",
                        (finished, engine, toolchain_json, '', '编译日志 SHA-256 校验不匹配，已拒收', job_id),
                    )
            return {'job_id': job_id, 'state': 'failed', 'error': '编译日志 SHA-256 校验不匹配，已拒收'}

    main_file = str(result_doc.get('main_file') or job_row['main_file'] or 'main.tex')
    stem = PurePosixPath(main_file).stem or 'main'
    prepared_html = _prepare_validated_html(html_info, html_bytes, stem)

    if result_doc.get('status') == 'succeeded':
        expected_sha = result_doc.get('pdf_sha256')
        if (
            not pdf_bytes
            or not isinstance(expected_sha, str)
            or not re.fullmatch(r'[0-9a-f]{64}', expected_sha)
            or sha256_hex(pdf_bytes) != expected_sha
        ):
            err = '编译产物 SHA-256 校验不匹配，已拒收'
            with RequestLock(db_path):
                with research.transaction():
                    research.db.execute(
                        "UPDATE compile_jobs SET state='failed', finished_at=?, engine=?, toolchain=?, log_text=?, error=? WHERE id=?",
                        (finished, engine, toolchain_json, log_text, err, job_id),
                    )
            return {'job_id': job_id, 'state': 'failed', 'error': err}
        if not pdf_bytes.startswith(b'%PDF-'):
            err = '编译产物不是有效的 PDF 文件，已拒收'
            with RequestLock(db_path):
                with research.transaction():
                    research.db.execute(
                        "UPDATE compile_jobs SET state='failed', finished_at=?, engine=?, toolchain=?, log_text=?, error=? WHERE id=?",
                        (finished, engine, toolchain_json, log_text, err, job_id),
                    )
            return {'job_id': job_id, 'state': 'failed', 'error': err}
        if len(pdf_bytes) > MAX_FILE:
            err = '编译生成的 PDF 超过单文件 32 MiB 上限'
            with RequestLock(db_path):
                with research.transaction():
                    research.db.execute(
                        "UPDATE compile_jobs SET state='failed', finished_at=?, engine=?, toolchain=?, log_text=?, error=? WHERE id=?",
                        (finished, engine, toolchain_json, log_text, err, job_id),
                    )
            return {'job_id': job_id, 'state': 'failed', 'error': err}

        pdf_filename = f'{stem}.pdf'
        html_item = None

        with RequestLock(db_path):
            with research.transaction():
                src_row = research.db.execute(
                    'SELECT * FROM research_files WHERE id=?',
                    (job_row['source_attachment_id'],),
                ).fetchone()
                if src_row is None:
                    research.db.execute(
                        "UPDATE compile_jobs SET state='failed', finished_at=?, engine=?, toolchain=?, log_text=?, error=? WHERE id=?",
                        (finished, engine, toolchain_json, log_text, '源附件不存在', job_id),
                    )
                    return {'job_id': job_id, 'state': 'failed', 'error': '源附件不存在'}
                try:
                    pdf_item = research._add_file(
                        job_row['actor_id'],
                        job_row['revision_id'],
                        pdf_filename,
                        pdf_bytes,
                        'application/pdf',
                        None,
                        origin='compiled',
                        derived_from_sha256=src_row['sha256'],
                    )
                except Problem as exc:
                    research.db.execute(
                        "UPDATE compile_jobs SET state='failed', finished_at=?, engine=?, toolchain=?, log_text=?, error=? WHERE id=?",
                        (finished, engine, toolchain_json, log_text, str(exc), job_id),
                    )
                    return {'job_id': job_id, 'state': 'failed', 'error': str(exc)}
                if prepared_html is not None:
                    html_fname, clean_html_bytes = prepared_html
                    try:
                        html_item = research._add_file(
                            job_row['actor_id'],
                            job_row['revision_id'],
                            html_fname,
                            clean_html_bytes,
                            'text/html',
                            None,
                            origin='compiled',
                            derived_from_sha256=src_row['sha256'],
                        )
                    except Problem:
                        html_item = None
                research.db.execute(
                    "UPDATE compile_jobs SET state='succeeded', finished_at=?, engine=?, toolchain=?, "
                    'output_attachment_id=?, log_text=?, error=NULL WHERE id=?',
                    (finished, engine, toolchain_json, pdf_item['id'], log_text, job_id),
                )
        out_res = {'job_id': job_id, 'state': 'succeeded', 'output_attachment_id': pdf_item['id']}
        if html_item is not None:
            out_res['html_attachment_id'] = html_item['id']
        return out_res

    err = str(result_doc.get('error') or 'LaTeX 编译未通过，请查看编译日志')
    with RequestLock(db_path):
        with research.transaction():
            if prepared_html is not None:
                src_row = research.db.execute(
                    'SELECT * FROM research_files WHERE id=?',
                    (job_row['source_attachment_id'],),
                ).fetchone()
                if src_row is not None:
                    html_fname, clean_html_bytes = prepared_html
                    try:
                        research._add_file(
                            job_row['actor_id'],
                            job_row['revision_id'],
                            html_fname,
                            clean_html_bytes,
                            'text/html',
                            None,
                            origin='compiled',
                            derived_from_sha256=src_row['sha256'],
                        )
                    except Problem:
                        pass
            research.db.execute(
                "UPDATE compile_jobs SET state='failed', finished_at=?, engine=?, toolchain=?, log_text=?, error=? WHERE id=?",
                (finished, engine, toolchain_json, log_text, err, job_id),
            )
    return {'job_id': job_id, 'state': 'failed', 'error': err}


def create_compile_client_from_env(*, urlopen=None) -> CloudRunCompileClient:
    return CloudRunCompileClient(
        project=os.environ.get('EKSMU_COMPILE_PROJECT', ''),
        region=os.environ.get('EKSMU_COMPILE_REGION', ''),
        job_name=os.environ.get('EKSMU_COMPILE_JOB', ''),
        compile_bucket=os.environ.get('EKSMU_COMPILE_BUCKET', ''),
        attachment_bucket=os.environ.get('EKSMU_BLOB_BUCKET', ''),
        gcs_api_endpoint=os.environ.get('EKSMU_GCS_API_ENDPOINT', GCS_API_ENDPOINT),
        run_api_endpoint=os.environ.get('EKSMU_CLOUD_RUN_API_ENDPOINT', CLOUD_RUN_API_ENDPOINT),
        token_url=os.environ.get('EKSMU_GCS_TOKEN_URL', METADATA_TOKEN_URL),
        urlopen=urlopen,
    )


def run_compile_queue(
    database: str | Path,
    *,
    client: CloudRunCompileClient | None = None,
    urlopen=None,
    limit: int = 10,
    timeout_seconds: float = COMPILE_JOB_TIMEOUT_SECONDS,
    local_mode: bool = False,
    now_dt: datetime | None = None,
) -> dict:
    """Poll running compile jobs and dispatch queued compile jobs."""
    db_path = local_path(database)
    if not is_compile_enabled():
        return {'enabled': False, 'completed': [], 'dispatched': []}
    if not db_path.is_file():
        raise Problem('Instance database is missing')
    sidecar = Path(str(db_path) + '.research.sqlite')
    if not sidecar.is_file():
        return {'enabled': True, 'completed': [], 'dispatched': []}

    use_local = local_mode or (os.environ.get('EKSMU_COMPILE_LOCAL', '').strip().lower() in {'1', 'true', 'yes'})
    if not use_local and client is None:
        client = create_compile_client_from_env(urlopen=urlopen)

    with InstanceLock(db_path, exclusive=False):
        store = Store(db_path)
        try:
            records = store.rows()
        finally:
            store.close()
        research = Research(sidecar, records)
        try:
            completed = []
            dispatched = []
            current_dt = now_dt or datetime.now(timezone.utc)

            # 1. Check all running jobs first
            running_rows = research.db.execute(
                "SELECT * FROM compile_jobs WHERE state='running' ORDER BY rowid"
            ).fetchall()
            for job_row in running_rows:
                job_id = job_row['id']
                if use_local:
                    continue
                assert client is not None
                raw_res = client.download_object(f'out/{job_id}/result.json', allow_404=True, max_bytes=262144)
                if raw_res is not None:
                    try:
                        res_doc = json.loads(raw_res.decode('utf-8'))
                    except Exception:
                        res_doc = {'status': 'failed', 'error': 'result.json 格式无效'}
                    log_bytes = client.download_object(
                        f'out/{job_id}/compile.log',
                        allow_404=True,
                        max_bytes=MAX_LOG_DOWNLOAD_BYTES,
                    ) or b''
                    main_f = str(res_doc.get('main_file') or job_row['main_file'] or 'main.tex')
                    stem = PurePosixPath(main_f).stem or 'main'
                    pdf_bytes = None
                    if res_doc.get('status') == 'succeeded':
                        pdf_bytes = client.download_object(
                            f'out/{job_id}/{stem}.pdf',
                            allow_404=True,
                            max_bytes=MAX_FILE + 1024,
                        )
                    html_bytes = None
                    html_info = res_doc.get('html') if isinstance(res_doc.get('html'), dict) else {}
                    if html_info.get('status') == 'succeeded':
                        html_fname = PurePosixPath(str(html_info.get('filename') or f'{stem}.html')).name
                        try:
                            html_bytes = client.download_object(
                                f'out/{job_id}/{html_fname}',
                                allow_404=True,
                                max_bytes=MAX_HTML_BYTES + 1024,
                            )
                        except Problem:
                            html_bytes = None
                    completed.append(
                        _apply_result_to_db(db_path, research, job_row, res_doc, pdf_bytes, log_bytes, html_bytes)
                    )
                    continue

                # No result.json yet: check timeout (35 min) or Cloud Run execution failure
                started_dt = _parse_iso(job_row['started_at']) or _parse_iso(job_row['requested_at'])
                if started_dt is not None and (current_dt - started_dt).total_seconds() >= timeout_seconds:
                    err = '编译任务超时（超过 35 分钟）'
                    with RequestLock(db_path):
                        with research.transaction():
                            research.db.execute(
                                "UPDATE compile_jobs SET state='failed', finished_at=?, error=? WHERE id=?",
                                (now(), err, job_id),
                            )
                    completed.append({'job_id': job_id, 'state': 'failed', 'error': err})
                    continue

                exec_doc = client.get_execution(job_row['execution_name'] or '')
                if _execution_failed(exec_doc):
                    err = 'Cloud Run 编译任务执行失败'
                    with RequestLock(db_path):
                        with research.transaction():
                            research.db.execute(
                                "UPDATE compile_jobs SET state='failed', finished_at=?, error=? WHERE id=?",
                                (now(), err, job_id),
                            )
                    completed.append({'job_id': job_id, 'state': 'failed', 'error': err})

            # 2. Dispatch queued jobs
            queued_rows = research.db.execute(
                "SELECT * FROM compile_jobs WHERE state='queued' ORDER BY rowid LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
            for job_row in queued_rows:
                job_id = job_row['id']
                src_row = research.db.execute(
                    'SELECT * FROM research_files WHERE id=?',
                    (job_row['source_attachment_id'],),
                ).fetchone()
                if src_row is None:
                    with RequestLock(db_path):
                        with research.transaction():
                            research.db.execute(
                                "UPDATE compile_jobs SET state='failed', finished_at=?, error=? WHERE id=?",
                                (now(), '源码包附件不存在', job_id),
                            )
                    completed.append({'job_id': job_id, 'state': 'failed', 'error': '源码包附件不存在'})
                    continue
                try:
                    raw_src = research._read_file_bytes(src_row)
                    if sha(raw_src) != src_row['sha256']:
                        raise Problem('源码包 SHA-256 校验失败', 503)
                except Problem as exc:
                    with RequestLock(db_path):
                        with research.transaction():
                            research.db.execute(
                                "UPDATE compile_jobs SET state='failed', finished_at=?, error=? WHERE id=?",
                                (now(), str(exc), job_id),
                            )
                    completed.append({'job_id': job_id, 'state': 'failed', 'error': str(exc)})
                    continue

                rev_row = research.index.get(job_row['revision_id'])
                source_date_epoch = _revision_source_date_epoch(rev_row)

                if use_local:
                    from deployment.compile.compile_entry import compile_archive_bytes
                    with RequestLock(db_path):
                        with research.transaction():
                            research.db.execute(
                                "UPDATE compile_jobs SET state='running', started_at=?, execution_name=? WHERE id=?",
                                (now(), f'local/{job_id}', job_id),
                            )
                    dispatched.append({'job_id': job_id, 'execution_name': f'local/{job_id}'})
                    with tempfile.TemporaryDirectory(prefix='eksmu-local-compile-') as tmp_out:
                        out_dir = Path(tmp_out)
                        res_doc = compile_archive_bytes(
                            raw_src,
                            out_dir,
                            filename=src_row['filename'],
                            main_file=job_row['main_file'],
                            source_date_epoch=source_date_epoch,
                        )
                        log_f = out_dir / 'compile.log'
                        log_bytes = log_f.read_bytes() if log_f.is_file() else b''
                        stem = PurePosixPath(res_doc.get('main_file') or job_row['main_file'] or 'main.tex').stem
                        pdf_f = out_dir / f'{stem}.pdf'
                        pdf_bytes = pdf_f.read_bytes() if pdf_f.is_file() else None
                        html_f = out_dir / f'{stem}.html'
                        html_bytes = html_f.read_bytes() if html_f.is_file() else None
                        refreshed_job = research.db.execute('SELECT * FROM compile_jobs WHERE id=?', (job_id,)).fetchone()
                        completed.append(
                            _apply_result_to_db(db_path, research, refreshed_job, res_doc, pdf_bytes, log_bytes, html_bytes)
                        )
                else:
                    assert client is not None
                    client.upload_source(job_id, raw_src)
                    exec_name = client.run_job(
                        job_id,
                        job_row['main_file'],
                        src_row['filename'],
                        source_date_epoch=source_date_epoch,
                    )
                    with RequestLock(db_path):
                        with research.transaction():
                            research.db.execute(
                                "UPDATE compile_jobs SET state='running', started_at=?, execution_name=? WHERE id=?",
                                (now(), exec_name, job_id),
                            )
                    dispatched.append({'job_id': job_id, 'execution_name': exec_name})

            return {'enabled': True, 'dispatched': dispatched, 'completed': completed}
        finally:
            research.close()
