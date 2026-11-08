#!/usr/bin/env python3
"""Isolated LaTeX source archive compiler for Eksmu (Cloud Run Job & local test mode).

Uses only the Python standard library. Compilation failures still exit with 0 after
writing result.json and compile.log; only infrastructure errors exit non-zero.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import closing
import gzip
import hashlib
from html import escape as html_escape
from html.parser import HTMLParser
from io import BytesIO
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
from urllib.parse import quote, unquote
import urllib.request
import zipfile

MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 2000
MAX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100
MAX_PDF_BYTES = 64 * 1024 * 1024
MAX_PDF_PAGES = 2000
MAX_HTML_BYTES = 16 * 1024 * 1024
MAX_LOG_BYTES = 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 1500
DEFAULT_HTML_TIMEOUT_SECONDS = 300
DEFAULT_JOB_TIMEOUT_SECONDS = 1800
PREBUILT_TEXMFVAR_DIR = Path('/var/cache/eksmu-texmfvar')

METADATA_TOKEN_URL = 'http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token'
GCS_API_ENDPOINT = 'https://storage.googleapis.com'

XELATEX_PACKAGES = {
    'fontspec', 'unicode-math', 'xecjk', 'ctex', 'ctexart', 'ctexrep',
    'ctexbook', 'ctexbeamer', 'xejamo', 'polyglossia',
}
LUALATEX_PACKAGES = {'luacode', 'luatexja', 'luatextra', 'luamplib'}

# Reject obvious attempts to read/write outside the sandbox or invoke shell commands,
# even on TeX Live 2026 where kpathsea's openin_any became a no-op.
_UNSAFE_IO_RE = re.compile(
    r'\\(?:immediate\s*)?\\write18\b'
    r'|\\(?:input|include|verbatiminput|lstinputlisting|includegraphics)\s*(?:\[[^\]]*\]\s*)?\{?\s*(?:/|\.\./|~)'
    r'|\\open(?:in|out)\b[^\n%]*?(?:=\s*|\s+)(?:/|\.\./|~)'
    r'|\\csname\s*(?:input|include|openin|openout|write18)\b'
)

# HTML sanitizer constants (shared contract with eksmu/compile_worker.py)
_DROP_CONTENT_TAGS = frozenset({
    'script', 'iframe', 'object', 'applet', 'frame', 'frameset',
    'noscript', 'foreignobject', 'audio', 'video', 'canvas', 'template',
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


class ArchiveError(ValueError):
    """Raised when an input archive violates safety or format constraints."""


class _BoundedReader:
    """Wrap a file-like stream and abort if total bytes read exceed ``limit``."""

    def __init__(self, stream, limit: int, message: str):
        self._stream = stream
        self._limit = limit
        self._message = message
        self.total = 0

    def read(self, size: int = -1) -> bytes:
        if size < 0 or size > 1024 * 1024:
            chunks = []
            while True:
                step = 1024 * 1024 if size < 0 else min(1024 * 1024, size)
                if step <= 0:
                    break
                part = self._stream.read(step)
                if not part:
                    break
                self.total += len(part)
                if self.total > self._limit:
                    raise ArchiveError(self._message)
                chunks.append(part)
                if size > 0:
                    size -= len(part)
            return b''.join(chunks)
        data = self._stream.read(size)
        self.total += len(data)
        if self.total > self._limit:
            raise ArchiveError(self._message)
        return data

    def seek(self, *args):
        return self._stream.seek(*args)

    def tell(self):
        return self._stream.tell()

    def close(self):
        return self._stream.close()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _validate_source_date_epoch(
    raw_val: int | str | None,
    *,
    now_ts: float | None = None,
) -> tuple[int | None, str | None]:
    """Validate SOURCE_DATE_EPOCH is a non-negative integer not in the future."""
    if raw_val is None or (isinstance(raw_val, str) and not raw_val.strip()):
        return None, None
    if isinstance(raw_val, bool):
        return None, f'[Eksmu] Warning: invalid SOURCE_DATE_EPOCH ({raw_val!r}); ignoring.\n'
    if isinstance(raw_val, int):
        if raw_val < 0:
            return None, f'[Eksmu] Warning: negative SOURCE_DATE_EPOCH ({raw_val}); ignoring.\n'
        val = raw_val
    else:
        s = str(raw_val).strip()
        if not re.fullmatch(r'[0-9]+', s):
            return None, f'[Eksmu] Warning: invalid SOURCE_DATE_EPOCH ({raw_val!r}); must be a non-negative integer; ignoring.\n'
        val = int(s)
    cur_ts = int(time.time() if now_ts is None else now_ts)
    if val > cur_ts:
        return None, f'[Eksmu] Warning: SOURCE_DATE_EPOCH ({val}) is in the future (> {cur_ts}); ignoring.\n'
    return val, None


def _remaining_s(deadline_mono: float | None, default_s: float) -> float:
    """Return min(default_s, remaining time until deadline_mono)."""
    if deadline_mono is None:
        return float(default_s)
    return min(float(default_s), max(0.0, deadline_mono - time.monotonic()))


def _strip_png_time_chunks(raw: bytes) -> bytes:
    """Remove ancillary timestamp/text chunks (tIME, tEXt, zTXt, iTXt) from PNG bytes for deterministic HTML."""
    if not raw.startswith(b'\x89PNG\r\n\x1a\n'):
        return raw
    out = [raw[:8]]
    pos = 8
    n = len(raw)
    while pos + 8 <= n:
        length = struct.unpack('>I', raw[pos:pos + 4])[0]
        ctype = raw[pos + 4:pos + 8]
        end = pos + 12 + length
        if end > n:
            return raw
        if ctype not in (b'tIME', b'tEXt', b'zTXt', b'iTXt'):
            out.append(raw[pos:end])
        pos = end
        if ctype == b'IEND':
            break
    return b''.join(out)


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
    """Stdlib HTMLParser-based sanitizer for self-contained compiled HTML."""

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
            if k_low.startswith('on') or k_low in _FORBIDDEN_ATTR_NAMES:
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
            clean_attrs.append(f' {raw_k}="{html_escape(val, quote=True)}"')

        self._out.append(f'<{tag}{"".join(clean_attrs)}>')
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
        if t in _DROP_CONTENT_TAGS or t in _DROP_ONLY_TAGS or t in _VOID_HTML_TAGS:
            return
        if t == 'style' and self._in_style:
            css_clean = _sanitize_css_text(''.join(self._style_buf))
            self._out.append(css_clean)
            self._in_style = False
            self._style_buf = []
        self._out.append(f'</{tag}>')

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
    """Sanitize compiled HTML to a self-contained single-file document without scripts or external resources."""
    parser = _CompiledHtmlSanitizer()
    parser.feed(html_text or '')
    parser.close()
    return parser.get_html()


def strip_tex_comments(text: str) -> str:
    lines = []
    for line in text.splitlines():
        cleaned = re.sub(r'(?<!\\)%.*$', '', line)
        lines.append(cleaned)
    return '\n'.join(lines)


def _archive_stem(filename: str) -> str:
    name = PurePosixPath(filename or 'archive.zip').name
    lower = name.lower()
    if lower.endswith('.tar.gz'):
        return name[:-7] or 'main'
    for ext in ('.zip', '.tgz', '.tar'):
        if lower.endswith(ext):
            return name[:-len(ext)] or 'main'
    return PurePosixPath(name).stem or 'main'


def _validate_rel_path(raw_name: str) -> str:
    if not isinstance(raw_name, str) or not raw_name.strip():
        raise ArchiveError('源码包包含空文件名')
    if any(ord(c) < 32 or ord(c) == 127 for c in raw_name) or '\\' in raw_name:
        raise ArchiveError('源码包不能包含绝对路径')
    if raw_name.startswith('/') or re.match(r'^[A-Za-z]:', raw_name):
        raise ArchiveError('源码包不能包含绝对路径')
    parts = [p for p in raw_name.split('/') if p not in ('', '.')]
    if any(p == '..' for p in raw_name.split('/')):
        raise ArchiveError('源码包路径不能包含 ..')
    if not parts:
        return ''
    norm = PurePosixPath(*parts)
    if norm.is_absolute() or '..' in norm.parts:
        raise ArchiveError('源码包路径不能包含 ..')
    return norm.as_posix()


def select_main_tex(tex_sources: dict[str, str], archive_filename: str = '') -> str:
    """Choose the main .tex file according to Eksmu's deterministic priority rule."""
    if not tex_sources:
        raise ArchiveError('源码包中没有任何 .tex 文件')
    stem_tex = f'{_archive_stem(archive_filename).lower()}.tex'

    def priority_key(path: str) -> tuple[int, str]:
        base = PurePosixPath(path).name.lower()
        if base == 'main.tex':
            rank = 0
        elif base == 'ms.tex':
            rank = 1
        elif base == 'paper.tex':
            rank = 2
        elif base == stem_tex:
            rank = 3
        else:
            rank = 4
        return (rank, path)

    def has_documentclass(src: str) -> bool:
        return bool(re.search(r'\\documentclass\b', strip_tex_comments(src)))

    top_level = [p for p in tex_sources if '/' not in p]
    top_with_doc = [p for p in top_level if has_documentclass(tex_sources[p])]
    if top_with_doc:
        return min(top_with_doc, key=priority_key)
    any_with_doc = [p for p in tex_sources if has_documentclass(tex_sources[p])]
    if any_with_doc:
        return min(any_with_doc, key=lambda p: (p.count('/'), *priority_key(p)))
    if top_level:
        return min(top_level, key=priority_key)
    return min(tex_sources, key=lambda p: (p.count('/'), *priority_key(p)))


def detect_engine(tex_sources: dict[str, str], main_file: str) -> str:
    """Detect lualatex, xelatex, or pdflatex from the main .tex file and included .tex files."""
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
    combined = '\n'.join(strip_tex_comments(tex_sources[p]) for p in ordered)
    if re.search(r'\\directlua\b', combined):
        return 'lualatex'
    packages: set[str] = set()
    for m in re.finditer(r'\\(?:usepackage|RequirePackage|documentclass)(?:\[[^\]]*\])?\{([^}]*)\}', combined):
        for pkg in m.group(1).split(','):
            cleaned = pkg.strip().lower()
            if cleaned:
                packages.add(cleaned)
    if packages & LUALATEX_PACKAGES:
        return 'lualatex'
    if packages & XELATEX_PACKAGES:
        return 'xelatex'
    return 'pdflatex'


def _detect_archive_kind(raw: bytes, filename: str = '') -> str:
    lower = (filename or '').lower()
    if lower.endswith('.zip'):
        return 'zip'
    if lower.endswith('.tar.gz') or lower.endswith('.tgz'):
        return 'tar.gz'
    if lower.endswith('.tar'):
        return 'tar'
    if raw.startswith(b'PK\x03\x04') or raw.startswith(b'PK\x05\x06'):
        return 'zip'
    if raw.startswith(b'\x1f\x8b'):
        return 'tar.gz'
    return 'tar'


def validate_and_extract_archive(raw: bytes, dest_dir: Path, filename: str = '') -> tuple[dict[str, str], list[str]]:
    """Validate archive limits/paths/ratios and safely extract into ``dest_dir``."""
    if not raw:
        raise ArchiveError('源码包不能为空')
    if len(raw) > MAX_ARCHIVE_BYTES:
        raise ArchiveError('单个附件不能超过 32 MiB')
    kind = _detect_archive_kind(raw, filename)
    dest_root = dest_dir.resolve()
    total_uncompressed = 0
    entry_count = 0
    tex_files: list[str] = []
    tex_sources: dict[str, str] = {}

    if kind == 'zip':
        try:
            zf_ctx = zipfile.ZipFile(BytesIO(raw), 'r')
        except (zipfile.BadZipFile, OSError, ValueError) as exc:
            raise ArchiveError('源码包格式损坏或无法读取') from exc
        with zf_ctx as zf:
            infos = zf.infolist()
            if len(infos) > MAX_ARCHIVE_ENTRIES:
                raise ArchiveError('源码包条目不能超过 2000 个')
            for info in infos:
                entry_count += 1
                if entry_count > MAX_ARCHIVE_ENTRIES:
                    raise ArchiveError('源码包条目不能超过 2000 个')
                rel = _validate_rel_path(info.filename)
                mode = (info.external_attr >> 16) & 0xFFFF
                if (stat.S_ISLNK(mode) or stat.S_ISBLK(mode) or stat.S_ISCHR(mode)
                        or stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode)):
                    raise ArchiveError('源码包不能包含符号链接、硬链接或设备文件')
                if info.file_size < 0:
                    raise ArchiveError('源码包格式损坏或无法读取')
                comp_size = max(1, int(info.compress_size))
                if info.file_size > comp_size * MAX_COMPRESSION_RATIO or info.file_size > max(1, len(raw)) * MAX_COMPRESSION_RATIO:
                    raise ArchiveError('源码包单文件压缩比不能超过 100 倍')
                total_uncompressed += info.file_size
                if total_uncompressed > MAX_UNCOMPRESSED_BYTES:
                    raise ArchiveError('源码包解压后总大小不能超过 256 MiB')
                if not rel or info.is_dir() or info.filename.endswith('/'):
                    if rel:
                        target_dir = (dest_root / rel).resolve()
                        if not target_dir.is_relative_to(dest_root):
                            raise ArchiveError('源码包路径不能包含 ..')
                        target_dir.mkdir(parents=True, exist_ok=True)
                    continue
                target = (dest_root / rel).resolve()
                if not target.is_relative_to(dest_root) or target == dest_root:
                    raise ArchiveError('源码包路径不能包含 ..')
                target.parent.mkdir(parents=True, exist_ok=True)
                file_limit = min(MAX_UNCOMPRESSED_BYTES, comp_size * MAX_COMPRESSION_RATIO)
                with zf.open(info, 'r') as src:
                    bounded = _BoundedReader(src, file_limit, '源码包单文件压缩比不能超过 100 倍')
                    data = bounded.read()
                if len(data) != info.file_size:
                    raise ArchiveError('源码包文件大小与目录不一致')
                target.write_bytes(data)
                os.chmod(target, 0o600)
                if PurePosixPath(rel).suffix.lower() == '.tex' and not PurePosixPath(rel).name.startswith('._'):
                    tex_files.append(rel)
                    tex_sources[rel] = data.decode('utf-8', errors='replace')
    else:
        stream_limit = min(MAX_UNCOMPRESSED_BYTES + 16 * 1024 * 1024, max(1, len(raw)) * MAX_COMPRESSION_RATIO + 1024 * 1024)
        try:
            if kind == 'tar.gz':
                gz = gzip.GzipFile(fileobj=BytesIO(raw), mode='rb')
                bounded_stream = _BoundedReader(gz, stream_limit, '源码包单文件压缩比不能超过 100 倍')
                tf_ctx = tarfile.open(fileobj=bounded_stream, mode='r|')
            else:
                tf_ctx = tarfile.open(fileobj=BytesIO(raw), mode='r:')
        except ArchiveError:
            raise
        except (tarfile.TarError, OSError, EOFError, ValueError) as exc:
            raise ArchiveError('源码包格式损坏或无法读取') from exc
        try:
            with tf_ctx as tf:
                for member in tf:
                    entry_count += 1
                    if entry_count > MAX_ARCHIVE_ENTRIES:
                        raise ArchiveError('源码包条目不能超过 2000 个')
                    rel = _validate_rel_path(member.name)
                    if (member.issym() or member.islnk() or member.isdev()
                            or member.ischr() or member.isblk() or member.isfifo()
                            or not (member.isfile() or member.isdir())):
                        raise ArchiveError('源码包不能包含符号链接、硬链接或设备文件')
                    if member.size < 0:
                        raise ArchiveError('源码包格式损坏或无法读取')
                    if member.size > max(1, len(raw)) * MAX_COMPRESSION_RATIO:
                        raise ArchiveError('源码包单文件压缩比不能超过 100 倍')
                    total_uncompressed += member.size
                    if total_uncompressed > MAX_UNCOMPRESSED_BYTES:
                        raise ArchiveError('源码包解压后总大小不能超过 256 MiB')
                    if not rel or member.isdir():
                        if rel:
                            target_dir = (dest_root / rel).resolve()
                            if not target_dir.is_relative_to(dest_root):
                                raise ArchiveError('源码包路径不能包含 ..')
                            target_dir.mkdir(parents=True, exist_ok=True)
                        continue
                    target = (dest_root / rel).resolve()
                    if not target.is_relative_to(dest_root) or target == dest_root:
                        raise ArchiveError('源码包路径不能包含 ..')
                    target.parent.mkdir(parents=True, exist_ok=True)
                    fobj = tf.extractfile(member)
                    if fobj is None:
                        raise ArchiveError('源码包格式损坏或无法读取')
                    file_limit = min(MAX_UNCOMPRESSED_BYTES, max(1, len(raw)) * MAX_COMPRESSION_RATIO)
                    with closing(fobj) as src:
                        bounded = _BoundedReader(src, file_limit, '源码包单文件压缩比不能超过 100 倍')
                        data = bounded.read()
                    if len(data) != member.size:
                        raise ArchiveError('源码包文件大小与目录不一致')
                    target.write_bytes(data)
                    os.chmod(target, 0o600)
                    if PurePosixPath(rel).suffix.lower() == '.tex' and not PurePosixPath(rel).name.startswith('._'):
                        tex_files.append(rel)
                        tex_sources[rel] = data.decode('utf-8', errors='replace')
        except ArchiveError:
            raise
        except (tarfile.TarError, OSError, EOFError, ValueError) as exc:
            raise ArchiveError('源码包格式损坏或无法读取') from exc

    if not tex_files:
        raise ArchiveError('源码包中没有任何 .tex 文件')
    return tex_sources, sorted(tex_files)


def _check_sources_for_unsafe_primitives(work_dir: Path) -> str | None:
    for path in sorted(work_dir.rglob('*')):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix in {'.ltxml', '.latexml'}:
            return f'禁止在源码包中包含自定义 LaTeXML Perl 绑定文件 ({path.relative_to(work_dir)})'
        if suffix in {'.tex', '.sty', '.cls', '.ltx', '.def', '.cfg', '.bbx', '.cbx', '.bib', '.bst'}:
            text = strip_tex_comments(path.read_text(encoding='utf-8', errors='replace'))
            m = _UNSAFE_IO_RE.search(text)
            if m:
                return f'禁止访问工作目录外的路径或执行外部命令 ({path.relative_to(work_dir)})'
    return None


_SYSTEM_READONLY_TEX_AND_FONT_DIRS = (
    '/usr/share/texlive',
    '/usr/local/texlive',
    '/usr/share/texmf',
    '/var/lib/texmf',
    '/usr/share/fonts',
    '/usr/local/share/fonts',
    '/usr/share/fontconfig',
    '/etc/fonts',
    '/usr/local/etc/fonts',
    '/usr/share/poppler/cMap',
    '/var/cache/fontconfig',
    '/var/cache/eksmu-texmfvar',
)


def _check_recorder_file(fls_path: Path, work_dir: Path, tex_bin_dir: str | None) -> str | None:
    if not fls_path.is_file():
        return None
    work_root = work_dir.resolve()
    allowed_roots: list[Path] = [work_root]
    for sys_dir in _SYSTEM_READONLY_TEX_AND_FONT_DIRS:
        p_sys = Path(sys_dir)
        allowed_roots.append(p_sys)
        try:
            allowed_roots.append(p_sys.resolve())
        except Exception:
            pass
    if tex_bin_dir:
        try:
            tl_root = Path(tex_bin_dir).resolve().parents[1]
            if len(tl_root.parts) >= 4 and (
                'texlive' in tl_root.name.lower() or tl_root.parent.name.lower() == 'texlive'
            ):
                allowed_roots.append(tl_root)
        except Exception:
            pass
    valid_roots = list(dict.fromkeys(allowed_roots))
    pwd = work_root
    for line in fls_path.read_text(encoding='utf-8', errors='replace').splitlines():
        if line.startswith('PWD '):
            try:
                pwd = Path(line[4:].strip()).resolve()
            except Exception:
                pwd = work_root
        elif line.startswith(('INPUT ', 'OUTPUT ')):
            kind, raw_target = line.split(' ', 1)
            raw_target = raw_target.strip()
            p = Path(raw_target)
            resolved = (p if p.is_absolute() else (pwd / p)).resolve()
            if kind == 'OUTPUT':
                if not resolved.is_relative_to(work_root):
                    return f'禁止写入工作目录外的文件: {raw_target}'
            else:
                if not any(resolved.is_relative_to(root) for root in valid_roots):
                    return f'禁止读取工作目录与 TeX 发行版以外的文件: {raw_target}'
    return None


def _seed_texmfvar(dest_texmfvar: Path) -> None:
    """Seed per-job TEXMFVAR with pre-built luaotfload/luatexja caches if available in image."""
    if dest_texmfvar.exists():
        return
    if PREBUILT_TEXMFVAR_DIR.is_dir():
        try:
            shutil.copytree(PREBUILT_TEXMFVAR_DIR, dest_texmfvar)
            for root, dirs, files in os.walk(dest_texmfvar):
                os.chmod(root, 0o700)
                for d in dirs:
                    os.chmod(os.path.join(root, d), 0o700)
                for f in files:
                    os.chmod(os.path.join(root, f), 0o600)
            return
        except Exception:
            shutil.rmtree(dest_texmfvar, ignore_errors=True)
    dest_texmfvar.mkdir(parents=True, exist_ok=True)


def _find_tex_bin_dir() -> str | None:
    for candidate in (
        os.environ.get('EKSMU_TEXBIN', ''),
        str(Path.home() / 'texlive/2026/bin/x86_64-linux'),
        '/usr/local/texlive/2026/bin/x86_64-linux',
        '/usr/local/texlive/2025/bin/x86_64-linux',
    ):
        if candidate and Path(candidate, 'latexmk').is_file():
            return candidate
    which_lmk = shutil.which('latexmk')
    if which_lmk:
        return str(Path(which_lmk).resolve().parent)
    return None


def _detect_latexml_version(env: dict[str, str]) -> str:
    try:
        r = subprocess.run(['latexml', '--VERSION'], env=env, capture_output=True, text=True, timeout=5)
        combined = (r.stdout or '') + '\n' + (r.stderr or '')
        m = re.search(r'LaTeXML\s+version\s+([0-9][0-9A-Za-z._-]*)', combined, re.IGNORECASE)
        if m:
            return m.group(1)
    except Exception:
        pass
    return ''


def _detect_toolchain(engine: str, env: dict[str, str]) -> dict[str, str]:
    info = {'texlive': '2026', 'latexmk': '', 'engine': engine, 'latexml': _detect_latexml_version(env)}
    try:
        r_lmk = subprocess.run(['latexmk', '--version'], env=env, capture_output=True, text=True, timeout=5)
        for line in r_lmk.stdout.splitlines():
            if 'Version' in line:
                info['latexmk'] = line.strip()
                break
    except Exception:
        pass
    try:
        r_eng = subprocess.run([engine, '--version'], env=env, capture_output=True, text=True, timeout=5)
        first = (r_eng.stdout.splitlines() or [''])[0].strip()
        if first:
            info['engine'] = first
            m = re.search(r'TeX Live\s+(20\d{2})', first)
            if m:
                info['texlive'] = m.group(1)
    except Exception:
        pass
    return info


def _count_pdf_pages(pdf_bytes: bytes, log_text: str) -> int:
    matches = re.findall(r'Output written on [^\n]*?\((\d+)\s+page', log_text)
    if matches:
        return int(matches[-1])
    counts = [int(x) for x in re.findall(rb'/Type\s*/Pages\b[^>]*/Count\s+(\d+)', pdf_bytes)]
    if counts:
        return max(counts)
    page_objs = len(re.findall(rb'/Type\s*/Page\b(?!s)', pdf_bytes))
    return max(1, page_objs)


_PERL_SANDBOX_MODULE = r"""package EksmuLaTeXMLSandbox;
use strict;
use warnings;
use utf8;
BEGIN {
    if (defined $ENV{SOURCE_DATE_EPOCH} && $ENV{SOURCE_DATE_EPOCH} =~ /^\d+$/) {
        my $epoch = int($ENV{SOURCE_DATE_EPOCH});
        *CORE::GLOBAL::localtime = sub {
            return CORE::gmtime($epoch);
        };
        *CORE::GLOBAL::gmtime = sub {
            return CORE::gmtime($epoch);
        };
    }
}
use Cwd qw(abs_path);
use File::Basename qw(dirname basename);
use File::Spec;
use POSIX ();
use LaTeXML::Util::Pathname ();

my $orig_is_abs  = \&LaTeXML::Util::Pathname::pathname_is_absolute;
my $orig_find    = \&LaTeXML::Util::Pathname::pathname_find;
my $orig_findall = \&LaTeXML::Util::Pathname::pathname_findall;
my $orig_kpse    = \&LaTeXML::Util::Pathname::pathname_kpsewhich;
my $orig_copy    = \&LaTeXML::Util::Pathname::pathname_copy;
my $orig_is_url  = \&LaTeXML::Util::Pathname::pathname_is_url;

sub _allowed_roots {
    my $work = $ENV{EKSMU_LATEXML_WORK_ROOT} || "";
    my $tl   = $ENV{EKSMU_LATEXML_TL_ROOT}   || "";
    my @candidates = (
        $work,
        $tl,
        "/usr/share/perl5/LaTeXML",
        "/usr/local/share/perl",
        "/usr/share/texlive",
        "/usr/local/texlive",
        "/usr/share/texmf",
        "/var/lib/texmf",
        "/usr/share/fonts",
        "/usr/local/share/fonts",
        "/usr/share/fontconfig",
        "/etc/fonts",
        "/usr/local/etc/fonts",
        "/usr/share/poppler/cMap",
    );
    my @roots = ();
    for my $c (@candidates) {
        next unless defined $c && $c ne "" && -e $c;
        my $real = abs_path($c);
        push(@roots, $real) if defined $real && $real ne "";
    }
    return @roots;
}

sub _resolve_any {
    my ($p) = @_;
    return undef unless defined $p && $p ne "";
    my $work = $ENV{EKSMU_LATEXML_WORK_ROOT} || ".";
    my $abs = File::Spec->file_name_is_absolute($p) ? $p : File::Spec->rel2abs($p, $work);
    if (-e $abs) {
        return abs_path($abs);
    }
    my @tail = ();
    my $cur = $abs;
    while (defined $cur && $cur ne "" && !-e $cur) {
        my $base = basename($cur);
        my $parent = dirname($cur);
        last if $parent eq $cur;
        unshift(@tail, $base);
        $cur = $parent;
    }
    my $real_base = (-e $cur) ? abs_path($cur) : undef;
    return undef unless defined $real_base;
    for my $seg (@tail) {
        return undef if $seg eq "..";
        $real_base = ($real_base eq "/") ? "/$seg" : "$real_base/$seg";
    }
    return $real_base;
}

sub _is_allowed_resolved {
    my ($resolved) = @_;
    return 0 unless defined $resolved && $resolved ne "";
    return 1 if $resolved eq "/dev/null" || $resolved eq "/dev/null.tex";
    for my $root (_allowed_roots()) {
        return 1 if $resolved eq $root || index($resolved, "$root/") == 0;
    }
    return 0;
}

sub _violation {
    my ($msg) = @_;
    binmode(STDERR, ":utf8");
    $| = 1;
    print STDERR "[EKSMU_SECURITY_VIOLATION] $msg\n";
    POSIX::_exit(99);
}

sub _check_candidate {
    my ($raw) = @_;
    return if !defined $raw || $raw eq "" || $raw eq "*";
    return if $raw =~ /^literal:/;
    return if $raw =~ m{^/dev/null(?:\.tex)?$};
    if ($raw =~ m{^(?:https?|ftp|file)://}i) {
        _violation("禁止访问外部网络或文件 URL: $raw");
    }
    if ($raw =~ m{^(?:/|\.\./|~)} || $raw =~ m{(?:^|/)\.\.(?:/|$)}) {
        my $resolved = _resolve_any($raw);
        if (!_is_allowed_resolved($resolved)) {
            _violation("禁止访问工作目录外的路径: $raw");
        }
    }
}

sub _check_resolved {
    my ($res) = @_;
    return undef unless defined $res && $res ne "";
    return $res if $res =~ /^literal:/;
    my $real = _resolve_any($res);
    if (!_is_allowed_resolved($real)) {
        _violation("禁止读取工作目录与 TeX 发行版以外的文件: $res");
    }
    my $work = ($ENV{EKSMU_LATEXML_WORK_ROOT} && -e $ENV{EKSMU_LATEXML_WORK_ROOT}) ? abs_path($ENV{EKSMU_LATEXML_WORK_ROOT}) : "";
    if ($work && ($real eq $work || index($real, "$work/") == 0) && $real =~ /\.(?:ltxml|latexml)$/i) {
        _violation("禁止加载源码包中的自定义 LaTeXML 绑定: $res");
    }
    return $res;
}

{
    no warnings "redefine";
    *LaTeXML::Util::Pathname::pathname_is_absolute = sub {
        my ($pathname) = @_;
        my $caller_pkg = (caller(0))[0] || "";
        my $caller_sub = (caller(1))[3] || "";
        if ($caller_pkg !~ /^LaTeXML::Post::/ && $caller_sub !~ /pathname_relative$/) {
            _check_candidate($pathname);
        }
        return $orig_is_abs->($pathname);
    };
    *LaTeXML::Util::Pathname::pathname_find = sub {
        my ($pathname, %options) = @_;
        _check_candidate($pathname);
        my $res = $orig_find->($pathname, %options);
        return _check_resolved($res);
    };
    *LaTeXML::Util::Pathname::pathname_findall = sub {
        my ($pathname, %options) = @_;
        _check_candidate($pathname);
        my @res = $orig_findall->($pathname, %options);
        return map { _check_resolved($_) } @res;
    };
    *LaTeXML::Util::Pathname::pathname_kpsewhich = sub {
        my (@names) = @_;
        for my $n (@names) { _check_candidate($n); }
        my $res = $orig_kpse->(@names);
        return _check_resolved($res);
    };
    *LaTeXML::Util::Pathname::pathname_copy = sub {
        my ($source, $dest) = @_;
        _check_resolved($source);
        _check_resolved($dest);
        return $orig_copy->($source, $dest);
    };
    *LaTeXML::Util::Pathname::pathname_is_url = sub {
        my ($pathname) = @_;
        return 0;
    };
}

our @KEEP_ALIVE;
our $POST_CHAIN_DONE = 0;

INIT {
    no warnings qw(redefine once);
    if (defined &LaTeXML::Post::Document::openCache) {
        *LaTeXML::Post::Document::openCache = sub {
            my ($self) = @_;
            $$self{cache} = {} unless $$self{cache};
            return;
        };
        *LaTeXML::Post::Document::closeCache = sub {
            return;
        };
    }
    if (defined &LaTeXML::Util::ObjectDB::finish) {
        my $orig_fin = \&LaTeXML::Util::ObjectDB::finish;
        *LaTeXML::Util::ObjectDB::finish = sub {
            my ($self) = @_;
            push(@KEEP_ALIVE, $self);
            return if !$$self{dbfile};
            return $orig_fin->($self);
        };
    }
    if (defined &LaTeXML::Post::Writer::process) {
        my $orig_writer = \&LaTeXML::Post::Writer::process;
        *LaTeXML::Post::Writer::process = sub {
            my ($self, $doc, $root) = @_;
            push(@KEEP_ALIVE, $self, $doc, $root);
            my $res = $orig_writer->($self, $doc, $root);
            push(@KEEP_ALIVE, $res);
            return $res;
        };
    }
    if (defined &LaTeXML::Post::ProcessChain_internal) {
        my $orig_pci = \&LaTeXML::Post::ProcessChain_internal;
        *LaTeXML::Post::ProcessChain_internal = sub {
            my ($self, $doc, @postprocessors) = @_;
            push(@KEEP_ALIVE, $self, $doc, @postprocessors);
            my @docs = $orig_pci->($self, $doc, @postprocessors);
            push(@KEEP_ALIVE, @docs);
            $POST_CHAIN_DONE = 1;
            $SIG{ALRM} = 'DEFAULT';
            POSIX::alarm(5);
            return @docs;
        };
    }
    if (defined &LaTeXML::Post::ProcessChain) {
        my $orig_pc = \&LaTeXML::Post::ProcessChain;
        *LaTeXML::Post::ProcessChain = sub {
            my ($self, $doc, @postprocessors) = @_;
            push(@KEEP_ALIVE, $self, $doc, @postprocessors);
            my @docs = $orig_pc->($self, $doc, @postprocessors);
            push(@KEEP_ALIVE, @docs);
            $SIG{ALRM} = 'DEFAULT';
            POSIX::alarm(5);
            return @docs;
        };
    }
}

END {
    if ($POST_CHAIN_DONE) {
        eval {
            STDOUT->flush() if defined *STDOUT{IO};
            STDERR->flush() if defined *STDERR{IO};
        };
        POSIX::_exit($?);
    }
}
1;
"""


def _format_exit_code(rc: int, timed_out: bool = False) -> str:
    """Format a subprocess return code with signal name when terminated by signal."""
    if timed_out:
        return f'{rc} (TIMEOUT/SIGKILL)'
    if rc < 0:
        try:
            sig_name = signal.Signals(-rc).name
            return f'{rc} ({sig_name})'
        except ValueError:
            return str(rc)
    if 128 < rc <= 192:
        try:
            sig_name = signal.Signals(rc - 128).name
            return f'{rc} ({sig_name})'
        except ValueError:
            return str(rc)
    return str(rc)


def _sanitize_work_paths_in_log(log_text: str, work_dir: Path, tmp_root: Path | None = None) -> str:
    """Strip container-internal temporary directory prefixes from author-visible logs."""
    out = log_text or ''
    prefixes: list[str] = []
    for p in (work_dir, tmp_root):
        if p is None:
            continue
        for cand in (str(p.resolve()), str(p)):
            if cand and cand not in prefixes:
                prefixes.append(cand)
    prefixes.sort(key=len, reverse=True)
    for pref in prefixes:
        out = out.replace(pref + '/', '')
        out = out.replace(pref, '.')
    out = re.sub(r'/tmp/eksmu-tex-build-[A-Za-z0-9_.-]+/(?:work_html|work)/', '', out)
    out = re.sub(r'/tmp/eksmu-tex-build-[A-Za-z0-9_.-]+', '.', out)
    return out


def _list_work_dir_files(work_dir: Path) -> list[str]:
    """List relative file paths and byte sizes in ``work_dir`` (excluding internal TeX caches)."""
    entries: list[str] = []
    if not work_dir.is_dir():
        return entries
    root = work_dir.resolve()
    for p in sorted(root.rglob('*')):
        try:
            if not p.is_file() or p.is_symlink():
                continue
            rel = p.relative_to(root).as_posix()
            if rel.startswith('.texlive-var/'):
                continue
            size = p.stat().st_size
            entries.append(f'{rel} ({size} B)')
        except OSError:
            continue
    return entries


def _is_recoverable_latexmlpost_output(rc2: int, log2: str, raw_html_path: Path) -> tuple[bool, str]:
    """Check whether a non-zero ``latexmlpost`` exit still left a complete, usable HTML output."""
    if rc2 == 99 or re.search(r'\[EKSMU_SECURITY_VIOLATION\]', log2 or ''):
        return False, ''
    if re.search(r'(?m)^Fatal:', log2 or ''):
        return False, ''
    if not raw_html_path.is_file():
        return False, ''
    try:
        if raw_html_path.stat().st_size <= 0:
            return False, ''
        raw_html_text = raw_html_path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return False, ''
    lower = raw_html_text.lower()
    if '<html' not in lower or '<body' not in lower or '</body>' not in lower or '</html>' not in lower:
        return False, ''
    body_match = re.search(r'<body\b[^>]*>(.*)</body>', raw_html_text, re.IGNORECASE | re.DOTALL)
    if not body_match or not body_match.group(1).strip():
        return False, ''
    return True, raw_html_text


def _extract_xml_graphics_map(xml_path: Path) -> dict[str, list[str]]:
    """Extract a mapping of ``xml:id`` (and ``__seq_{i}`` order keys) -> candidate graphic paths from ``main.latexml.xml``."""
    mapping: dict[str, list[str]] = {}
    if not xml_path.is_file():
        return mapping
    try:
        xml_text = xml_path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return mapping
    seq_idx = 0
    for m in re.finditer(r'<ltx:graphics\b([^>]*)/?>', xml_text):
        attrs_str = m.group(1)
        cands: list[str] = []
        cand_m = re.search(r'\bcandidates="([^"]+)"', attrs_str)
        if cand_m:
            for item in cand_m.group(1).split(','):
                s = item.strip()
                if s and s not in cands:
                    cands.append(s)
        graf_m = re.search(r'\bgraphic="([^"]+)"', attrs_str)
        if graf_m:
            g = graf_m.group(1).strip()
            if g and g not in cands:
                cands.append(g)
        if cands:
            mapping[f'__seq_{seq_idx}'] = cands
            seq_idx += 1
            id_m = re.search(r'\bxml:id="([^"]+)"', attrs_str)
            if id_m:
                xml_id = id_m.group(1).strip()
                if xml_id:
                    mapping[xml_id] = cands
    return mapping


def _convert_pdf_to_png_bytes(
    pdf_path: Path,
    work_root: Path,
    dpi: int = 150,
    *,
    deadline_mono: float | None = None,
) -> bytes | None:
    """Convert the first page of a sandboxed PDF figure into PNG bytes."""
    work_resolved = work_root.resolve()
    pdf_resolved = pdf_path.resolve()
    if not pdf_resolved.is_relative_to(work_resolved) or not pdf_resolved.is_file():
        return None
    if deadline_mono is not None and time.monotonic() >= deadline_mono:
        return None
    conv_dir = work_resolved / '.eksmu-img-conv'
    conv_dir.mkdir(mode=0o700, exist_ok=True)
    digest = hashlib.sha256(f'{pdf_resolved}:{dpi}'.encode('utf-8')).hexdigest()[:16]
    out_prefix = conv_dir / f'pdf_{digest}'
    out_png = conv_dir / f'pdf_{digest}.png'
    env = {
        'PATH': '/usr/local/bin:/usr/bin:/bin',
        'HOME': str(work_resolved),
        'TMPDIR': str(work_resolved),
        'LANG': 'C.UTF-8',
        'LC_ALL': 'C.UTF-8',
    }
    if shutil.which('pdftocairo', path=env['PATH']):
        rem = _remaining_s(deadline_mono, 30.0)
        if rem <= 0:
            return None
        rc, _, _ = _run_step_with_timeout(
            ['pdftocairo', '-png', '-singlefile', '-cropbox', '-r', str(dpi), str(pdf_resolved), str(out_prefix)],
            conv_dir,
            env,
            rem,
        )
        if rc == 0 and out_png.is_file() and out_png.stat().st_size > 0:
            try:
                return out_png.read_bytes()
            except OSError:
                pass
    if shutil.which('gs', path=env['PATH']):
        rem = _remaining_s(deadline_mono, 30.0)
        if rem <= 0:
            return None
        rc, _, _ = _run_step_with_timeout(
            [
                'gs', '-dSAFER', '-dBATCH', '-dNOPAUSE', '-dQUIET',
                '-sDEVICE=pngalpha', '-dUseCropBox', '-dFirstPage=1', '-dLastPage=1',
                f'-r{dpi}', f'-sOutputFile={out_png}', str(pdf_resolved),
            ],
            conv_dir,
            env,
            rem,
        )
        if rc == 0 and out_png.is_file() and out_png.stat().st_size > 0:
            try:
                return out_png.read_bytes()
            except OSError:
                pass
    return None


def _downscale_raster_image_bytes(
    img_path: Path,
    raw: bytes,
    work_root: Path,
    max_bytes: int,
    max_dim: int = 1200,
    *,
    deadline_mono: float | None = None,
) -> tuple[bytes, str] | None:
    """Downscale a raster image inside ``work_root`` so its byte size does not exceed ``max_bytes``."""
    if deadline_mono is not None and time.monotonic() >= deadline_mono:
        return None
    work_resolved = work_root.resolve()
    conv_dir = work_resolved / '.eksmu-img-conv'
    conv_dir.mkdir(mode=0o700, exist_ok=True)
    env = {
        'PATH': '/usr/local/bin:/usr/bin:/bin',
        'HOME': str(work_resolved),
        'TMPDIR': str(work_resolved),
        'MAGICK_THREAD_LIMIT': '1',
        'OMP_NUM_THREADS': '1',
        'LANG': 'C.UTF-8',
        'LC_ALL': 'C.UTF-8',
    }
    magick_bin = shutil.which('magick', path=env['PATH']) or shutil.which('convert', path=env['PATH'])
    if not magick_bin:
        return None
    digest = hashlib.sha256(raw[:4096] + f':{len(raw)}:{max_bytes}:{max_dim}'.encode('utf-8')).hexdigest()[:16]
    src_tmp = conv_dir / f'in_{digest}.png'
    try:
        src_tmp.write_bytes(raw)
    except OSError:
        return None
    dims = [d for d in (max_dim, 1200, 900, 640, 480, 320) if d <= max_dim]
    if not dims:
        dims = [max_dim]
    for dim in dims:
        rem = _remaining_s(deadline_mono, 20.0)
        if rem <= 0:
            return None
        out_png = conv_dir / f'out_{digest}_{dim}.png'
        cmd = [
            magick_bin,
            str(src_tmp),
            '-resize', f'{dim}x{dim}>',
            '-strip',
            '-colors', '256',
            '-define', 'png:compression-level=9',
            '-define', 'png:exclude-chunks=date,time',
            f'png:{out_png}',
        ]
        rc, _, _ = _run_step_with_timeout(cmd, conv_dir, env, rem)
        if rc == 0 and out_png.is_file():
            try:
                out_bytes = out_png.read_bytes()
                if 0 < len(out_bytes) <= max_bytes:
                    return out_bytes, 'image/png'
            except OSError:
                continue
    return None


class _ResourceInliner(HTMLParser):
    """Inline local CSS files and convert local image files in ``work_root`` into data:image/* URIs."""

    def __init__(
        self,
        run_cwd: Path,
        work_root: Path,
        *,
        max_image_bytes: int = 8 * 1024 * 1024,
        max_dim: int | None = None,
        pdf_dpi: int = 150,
        graphics_map: dict[str, list[str]] | None = None,
        deadline_mono: float | None = None,
    ) -> None:
        super().__init__(convert_charrefs=False)
        self._run_cwd = run_cwd.resolve()
        self._work_root = work_root.resolve()
        self._max_image_bytes = max_image_bytes
        self._max_dim = max_dim
        self._pdf_dpi = pdf_dpi
        self._graphics_map = graphics_map or {}
        self._deadline_mono = deadline_mono
        self.skipped_due_to_deadline = 0
        self._gfx_seq_idx = 0
        self._out: list[str] = []

    def _deadline_expired(self) -> bool:
        return self._deadline_mono is not None and time.monotonic() >= self._deadline_mono

    def _call_pdf_to_png(self, img_path: Path, dpi: int) -> bytes | None:
        try:
            return _convert_pdf_to_png_bytes(
                img_path,
                self._work_root,
                dpi=dpi,
                deadline_mono=self._deadline_mono,
            )
        except TypeError:
            return _convert_pdf_to_png_bytes(img_path, self._work_root, dpi=dpi)

    def _call_downscale(self, img_path: Path, raw: bytes, target_dim: int) -> tuple[bytes, str] | None:
        try:
            return _downscale_raster_image_bytes(
                img_path,
                raw,
                self._work_root,
                self._max_image_bytes,
                max_dim=target_dim,
                deadline_mono=self._deadline_mono,
            )
        except TypeError:
            return _downscale_raster_image_bytes(
                img_path,
                raw,
                self._work_root,
                self._max_image_bytes,
                max_dim=target_dim,
            )

    def _resolve_local_file(self, rel_href: str) -> Path | None:
        raw = unquote((rel_href or '').strip())
        if not raw or raw.startswith(('/', 'http:', 'https:', 'data:', 'javascript:', 'vbscript:', 'file:', '//')):
            return None
        candidate = (self._run_cwd / raw).resolve()
        if candidate.is_relative_to(self._work_root) and candidate.is_file():
            return candidate
        if not Path(raw).suffix:
            for ext in ('.png', '.pdf', '.svg', '.jpg', '.jpeg', '.gif'):
                cand_ext = (self._run_cwd / f'{raw}{ext}').resolve()
                if cand_ext.is_relative_to(self._work_root) and cand_ext.is_file():
                    return cand_ext
        css_sys = Path('/usr/share/perl5/LaTeXML/resources/CSS') / PurePosixPath(raw).name
        if css_sys.is_file():
            return css_sys.resolve()
        return None

    def _image_to_data_uri(self, img_path: Path) -> str | None:
        if self._deadline_expired():
            return None
        try:
            raw = img_path.read_bytes()
        except OSError:
            return None
        if not raw:
            return None
        ext = img_path.suffix.lower()
        mime: str | None = None
        if raw.startswith(b'%PDF-') or ext == '.pdf':
            for dpi in (self._pdf_dpi, 110, 85, 65, 50):
                if dpi > self._pdf_dpi:
                    continue
                if self._deadline_expired():
                    return None
                png_bytes = self._call_pdf_to_png(img_path, dpi)
                if png_bytes and len(png_bytes) <= self._max_image_bytes:
                    raw = png_bytes
                    ext = '.png'
                    mime = 'image/png'
                    break
                if png_bytes:
                    raw = png_bytes
                    ext = '.png'
                    mime = 'image/png'
            if mime != 'image/png':
                return None
        elif raw.startswith(b'\x89PNG\r\n\x1a\n') or ext == '.png':
            mime = 'image/png'
        elif raw.startswith(b'\xff\xd8\xff') or ext in ('.jpg', '.jpeg'):
            mime = 'image/jpeg'
        elif raw.startswith((b'GIF87a', b'GIF89a')) or ext == '.gif':
            mime = 'image/gif'
        elif raw.startswith(b'RIFF') and raw[8:12] == b'WEBP' or ext == '.webp':
            mime = 'image/webp'
        elif ext == '.svg':
            svg_clean = sanitize_compiled_html(raw.decode('utf-8', errors='replace'))
            raw = svg_clean.encode('utf-8')
            mime = 'image/svg+xml'
        else:
            return None

        if mime != 'image/svg+xml' and (len(raw) > self._max_image_bytes or self._max_dim is not None):
            if self._deadline_expired():
                return None
            target_dim = self._max_dim or 1400
            downscaled = self._call_downscale(img_path, raw, target_dim)
            if downscaled is not None:
                raw, mime = downscaled
            elif len(raw) > self._max_image_bytes:
                return None
        elif len(raw) > self._max_image_bytes:
            return None

        if mime == 'image/png':
            raw = _strip_png_time_chunks(raw)

        b64 = base64.b64encode(raw).decode('ascii')
        uri = f'data:{mime};base64,{b64}'
        return uri if _is_safe_data_image_uri(uri) else None

    def handle_decl(self, decl: str) -> None:
        self._out.append(f'<!{decl}>')

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        attr_dict = {(k or '').lower(): (v or '') for k, v in attrs if k}
        if t == 'link' and 'stylesheet' in attr_dict.get('rel', '').lower():
            css_path = self._resolve_local_file(attr_dict.get('href', ''))
            if css_path is not None:
                try:
                    css_text = _sanitize_css_text(css_path.read_text(encoding='utf-8', errors='replace'))
                    self._out.append(f'<style type="text/css">\n{css_text}\n</style>')
                except OSError:
                    pass
            return
        if t == 'img':
            src_val = attr_dict.get('src', '').strip()
            cls_tokens = attr_dict.get('class', '').split()
            is_ltx_graphics = 'ltx_graphics' in cls_tokens or 'ltx_missing_image' in cls_tokens
            seq_key: str | None = None
            if is_ltx_graphics:
                seq_key = f'__seq_{self._gfx_seq_idx}'
                self._gfx_seq_idx += 1
            img_path: Path | None = None
            if src_val and not src_val.lower().startswith('data:'):
                img_path = self._resolve_local_file(src_val)
            if img_path is None:
                img_id = attr_dict.get('id', '').strip()
                cand_list = list(self._graphics_map.get(img_id, []))
                if seq_key and seq_key in self._graphics_map:
                    for c in self._graphics_map[seq_key]:
                        if c not in cand_list:
                            cand_list.append(c)
                for cand in cand_list:
                    img_path = self._resolve_local_file(cand)
                    if img_path is not None:
                        break
            if img_path is not None:
                if self._deadline_expired():
                    self.skipped_due_to_deadline += 1
                    alt_text = attr_dict.get('alt', '').strip() or attr_dict.get('id', '').strip() or img_path.name
                    self._out.append(f'<span class="ltx_text ltx_missing_image">{html_escape(alt_text)}</span>')
                    return
                data_uri = self._image_to_data_uri(img_path)
                if data_uri:
                    new_attrs: list[tuple[str, str | None]] = []
                    replaced_src = False
                    for k, v in attrs:
                        kl = (k or '').lower()
                        if kl == 'src':
                            new_attrs.append((k, data_uri))
                            replaced_src = True
                        elif kl == 'class' and v:
                            cleaned_cls = ' '.join(
                                c for c in v.split() if c not in ('ltx_missing', 'ltx_missing_image')
                            )
                            new_attrs.append((k, cleaned_cls))
                        else:
                            new_attrs.append((k, v))
                    if not replaced_src:
                        new_attrs.append(('src', data_uri))
                    attrs = new_attrs
                elif self._deadline_expired():
                    self.skipped_due_to_deadline += 1
                    alt_text = attr_dict.get('alt', '').strip() or attr_dict.get('id', '').strip() or img_path.name
                    self._out.append(f'<span class="ltx_text ltx_missing_image">{html_escape(alt_text)}</span>')
                    return
        rendered_attrs = ''.join(
            f' {k}="{html_escape(v or "", quote=True)}"' for k, v in attrs if k
        )
        self._out.append(f'<{tag}{rendered_attrs}>')
        if t == 'head':
            self._out.append(
                '<style type="text/css">'
                'body{max-width:56rem;margin:2rem auto;padding:0 1.25rem;line-height:1.65;color:#111;background:#fff;'
                'font-family:"Noto Serif","Noto Serif CJK SC","Source Han Serif SC",serif;}'
                'img{max-width:100%;height:auto;}'
                'table{border-collapse:collapse;margin:1rem 0;}'
                'td,th{padding:0.35rem 0.6rem;}'
                '</style>'
            )

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in _VOID_HTML_TAGS:
            return
        self._out.append(f'</{tag}>')

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in _VOID_HTML_TAGS:
            self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        self._out.append(data)

    def handle_entityref(self, name: str) -> None:
        self._out.append(f'&{name};')

    def handle_charref(self, name: str) -> None:
        self._out.append(f'&#{name};')

    def get_html(self) -> str:
        return ''.join(self._out)


def _inline_latexml_resources(
    html_text: str,
    run_cwd: Path,
    work_root: Path,
    *,
    max_image_bytes: int = 8 * 1024 * 1024,
    max_dim: int | None = None,
    pdf_dpi: int = 150,
    graphics_map: dict[str, list[str]] | None = None,
    deadline_mono: float | None = None,
) -> str:
    inliner = _ResourceInliner(
        run_cwd,
        work_root,
        max_image_bytes=max_image_bytes,
        max_dim=max_dim,
        pdf_dpi=pdf_dpi,
        graphics_map=graphics_map,
        deadline_mono=deadline_mono,
    )
    inliner.feed(html_text or '')
    inliner.close()
    return inliner.get_html()


def _run_step_with_timeout(cmd: list[str], cwd: Path, env: dict[str, str], timeout_s: float) -> tuple[int, str, bool]:
    timed_out = False
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            out_bytes, _ = proc.communicate(timeout=timeout_s)
            return proc.returncode, out_bytes.decode('utf-8', errors='replace'), False
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                proc.kill()
            out_bytes, _ = proc.communicate()
            return -1, out_bytes.decode('utf-8', errors='replace'), timed_out
    except OSError as exc:
        return 127, f'Failed to launch {cmd[0]}: {exc}\n', False


def _compile_html_with_latexml(
    pristine_src_dir: Path,
    tmp_root: Path,
    chosen: str,
    out_dir: Path,
    base_env: dict[str, str],
    tex_bin: str | None,
    latexml_version: str,
    timeout_s: float,
) -> tuple[dict, str]:
    """Run latexml + latexmlpost in an isolated copy of the source directory and produce sanitized single-file HTML."""
    t0 = time.monotonic()
    deadline_mono = t0 + timeout_s
    stem = PurePosixPath(chosen).stem or 'main'
    html_filename = f'{stem}.html'

    if not shutil.which('latexml', path=base_env.get('PATH')) or not shutil.which('latexmlpost', path=base_env.get('PATH')):
        return {
            'status': 'failed',
            'filename': None,
            'sha256': None,
            'size': None,
            'latexml_version': latexml_version,
            'duration_s': round(time.monotonic() - t0, 3),
            'error': '编译环境中未安装 LaTeXML',
        }, 'LaTeXML not installed in environment.\n'

    work_html = tmp_root / 'work_html'
    shutil.copytree(pristine_src_dir, work_html)
    os.chmod(work_html, 0o700)

    perl_sandbox_dir = tmp_root / 'perl_sandbox'
    perl_sandbox_dir.mkdir(mode=0o700, exist_ok=True)
    (perl_sandbox_dir / 'EksmuLaTeXMLSandbox.pm').write_text(_PERL_SANDBOX_MODULE, encoding='utf-8')

    tl_root = ''
    if tex_bin:
        try:
            tl_root = str(Path(tex_bin).resolve().parents[1])
        except Exception:
            tl_root = ''

    main_path = work_html / chosen
    run_cwd = main_path.parent
    main_arg = main_path.name
    xml_path = run_cwd / f'{stem}.latexml.xml'
    raw_html_path = run_cwd / html_filename

    html_env = {
        **base_env,
        'HOME': str(work_html),
        'TMPDIR': str(work_html),
        'TEXMFVAR': str(work_html / '.texlive-var'),
        'TEXMFCACHE': str(work_html / '.texlive-var'),
        'openin_any': 'p',
        'openout_any': 'p',
        'shell_escape': 'f',
        'MAGICK_THREAD_LIMIT': '1',
        'OMP_NUM_THREADS': '1',
        'PERL5LIB': str(perl_sandbox_dir),
        'PERL5OPT': '-MEksmuLaTeXMLSandbox',
        'EKSMU_LATEXML_WORK_ROOT': str(work_html.resolve()),
        'EKSMU_LATEXML_TL_ROOT': tl_root,
    }

    cmd_latexml = [
        'latexml',
        '--includestyles',
        '--nocomments',
        f'--path={run_cwd}',
        f'--destination={xml_path}',
        main_arg,
    ]
    rem1 = _remaining_s(deadline_mono, timeout_s)
    if rem1 <= 0:
        return {
            'status': 'failed',
            'filename': None,
            'sha256': None,
            'size': None,
            'latexml_version': latexml_version,
            'duration_s': round(time.monotonic() - t0, 3),
            'error': f'HTML 转换超时（超过 {int(timeout_s)} 秒上限）',
        }, '=== LaTeXML (latexml) ===\n[Eksmu] HTML deadline already reached before latexml.\n'
    rc1, log1, to1 = _run_step_with_timeout(cmd_latexml, run_cwd, html_env, rem1)
    clean_log1 = _sanitize_work_paths_in_log(log1, work_html, tmp_root)
    combined_html_log = f'=== LaTeXML (latexml) ===\n{clean_log1}\n[Eksmu] latexml exit_code: {_format_exit_code(rc1, to1)}\n'

    sec_match = re.search(r'\[EKSMU_SECURITY_VIOLATION\]\s*(.+)', clean_log1)
    if sec_match or rc1 == 99:
        reason = sec_match.group(1).strip() if sec_match else '禁止访问工作目录外的路径'
        return {
            'status': 'failed',
            'filename': None,
            'sha256': None,
            'size': None,
            'latexml_version': latexml_version,
            'duration_s': round(time.monotonic() - t0, 3),
            'error': reason,
        }, combined_html_log

    if to1:
        return {
            'status': 'failed',
            'filename': None,
            'sha256': None,
            'size': None,
            'latexml_version': latexml_version,
            'duration_s': round(time.monotonic() - t0, 3),
            'error': f'HTML 转换超时（超过 {int(timeout_s)} 秒上限）',
        }, combined_html_log

    if rc1 != 0 or not xml_path.is_file():
        return {
            'status': 'failed',
            'filename': None,
            'sha256': None,
            'size': None,
            'latexml_version': latexml_version,
            'duration_s': round(time.monotonic() - t0, 3),
            'error': 'LaTeXML 转换未通过，请查看编译日志',
        }, combined_html_log

    remaining = _remaining_s(deadline_mono, timeout_s)
    if remaining <= 0:
        return {
            'status': 'failed',
            'filename': None,
            'sha256': None,
            'size': None,
            'latexml_version': latexml_version,
            'duration_s': round(time.monotonic() - t0, 3),
            'error': f'HTML 转换超时（超过 {int(timeout_s)} 秒上限）',
        }, combined_html_log
    cmd_post = [
        'latexmlpost',
        '--format=html5',
        '--pmml',
        f'--sourcedirectory={run_cwd}',
        f'--destination={raw_html_path}',
        str(xml_path),
    ]
    rc2, log2, to2 = _run_step_with_timeout(cmd_post, run_cwd, html_env, remaining)
    clean_log2 = _sanitize_work_paths_in_log(log2, work_html, tmp_root)
    post_files = _list_work_dir_files(work_html)
    combined_html_log += (
        f'=== LaTeXML (latexmlpost) ===\n{clean_log2}\n'
        f'[Eksmu] latexmlpost exit_code: {_format_exit_code(rc2, to2)}\n'
        f'[Eksmu] output directory files ({len(post_files)}): {", ".join(post_files)}\n'
    )

    sec_match2 = re.search(r'\[EKSMU_SECURITY_VIOLATION\]\s*(.+)', clean_log2)
    if sec_match2 or rc2 == 99:
        reason = sec_match2.group(1).strip() if sec_match2 else '禁止访问工作目录外的路径'
        return {
            'status': 'failed',
            'filename': None,
            'sha256': None,
            'size': None,
            'latexml_version': latexml_version,
            'duration_s': round(time.monotonic() - t0, 3),
            'error': reason,
        }, combined_html_log

    if to2:
        return {
            'status': 'failed',
            'filename': None,
            'sha256': None,
            'size': None,
            'latexml_version': latexml_version,
            'duration_s': round(time.monotonic() - t0, 3),
            'error': f'HTML 转换超时（超过 {int(timeout_s)} 秒上限）',
        }, combined_html_log

    if rc2 == 0 and raw_html_path.is_file():
        raw_html_text = raw_html_path.read_text(encoding='utf-8', errors='replace')
    else:
        recoverable, raw_html_text = _is_recoverable_latexmlpost_output(rc2, clean_log2, raw_html_path)
        if not recoverable:
            return {
                'status': 'failed',
                'filename': None,
                'sha256': None,
                'size': None,
                'latexml_version': latexml_version,
                'duration_s': round(time.monotonic() - t0, 3),
                'error': 'LaTeXML 后处理未通过，请查看编译日志',
            }, combined_html_log
        combined_html_log += (
            f'[Eksmu] Note: latexmlpost exited with non-zero code ({_format_exit_code(rc2)}), '
            f'but complete HTML ({html_filename}, {len(raw_html_text.encode("utf-8"))} B) was written; '
            f'accepting HTML output.\n'
        )

    graphics_map = _extract_xml_graphics_map(xml_path)
    inliner = _ResourceInliner(
        run_cwd,
        work_html,
        graphics_map=graphics_map,
        deadline_mono=deadline_mono,
    )
    inliner.feed(raw_html_text or '')
    inliner.close()
    inlined_html = inliner.get_html()
    if inliner.skipped_due_to_deadline > 0:
        combined_html_log += (
            f'[Eksmu] Note: HTML figure conversion reached {int(timeout_s)}s budget; '
            f'{inliner.skipped_due_to_deadline} remaining image(s) kept as alt-text placeholders.\n'
        )
    sanitized_html = sanitize_compiled_html(inlined_html)
    html_bytes = sanitized_html.encode('utf-8')

    if len(html_bytes) > MAX_HTML_BYTES:
        for max_img_b, max_dim, dpi in (
            (700_000, 1400, 110),
            (400_000, 1000, 85),
            (200_000, 720, 65),
            (100_000, 480, 50),
        ):
            if time.monotonic() >= deadline_mono:
                combined_html_log += (
                    f'[Eksmu] Note: HTML image downscaling stopped at {int(timeout_s)}s deadline.\n'
                )
                break
            ds_inliner = _ResourceInliner(
                run_cwd,
                work_html,
                max_image_bytes=max_img_b,
                max_dim=max_dim,
                pdf_dpi=dpi,
                graphics_map=graphics_map,
                deadline_mono=deadline_mono,
            )
            ds_inliner.feed(raw_html_text or '')
            ds_inliner.close()
            inlined_html = ds_inliner.get_html()
            sanitized_html = sanitize_compiled_html(inlined_html)
            html_bytes = sanitized_html.encode('utf-8')
            if len(html_bytes) <= MAX_HTML_BYTES:
                combined_html_log += (
                    f'[Eksmu] Note: downscaled inline images (max_dim={max_dim}, pdf_dpi={dpi}) '
                    f'to fit within 16 MiB HTML limit (final size: {len(html_bytes)} B).\n'
                )
                break

    if len(html_bytes) > MAX_HTML_BYTES:
        return {
            'status': 'failed',
            'filename': None,
            'sha256': None,
            'size': None,
            'latexml_version': latexml_version,
            'duration_s': round(time.monotonic() - t0, 3),
            'error': '生成的 HTML 超过 16 MiB 上限',
        }, combined_html_log

    out_html = out_dir / html_filename
    out_html.write_bytes(html_bytes)
    return {
        'status': 'succeeded',
        'filename': html_filename,
        'sha256': sha256_hex(html_bytes),
        'size': len(html_bytes),
        'latexml_version': latexml_version,
        'duration_s': round(time.monotonic() - t0, 3),
        'error': None,
    }, combined_html_log


def compile_archive_bytes(
    raw: bytes,
    out_dir: Path,
    *,
    filename: str = 'source.zip',
    main_file: str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_SECONDS,
    html_timeout_s: float = DEFAULT_HTML_TIMEOUT_SECONDS,
    source_date_epoch: int | str | None = None,
) -> dict:
    """Validate archive, compile PDF + HTML in isolated temporary directories, and write outputs to ``out_dir``."""
    started = time.monotonic()
    out_dir.mkdir(parents=True, exist_ok=True)
    tex_bin = _find_tex_bin_dir()
    path_parts = ([tex_bin] if tex_bin else []) + ['/usr/local/bin', '/usr/bin', '/bin']
    raw_sde = source_date_epoch if source_date_epoch is not None else os.environ.get('SOURCE_DATE_EPOCH')
    validated_epoch, sde_warning = _validate_source_date_epoch(raw_sde)
    base_env = {'PATH': ':'.join(path_parts), 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8', 'TZ': 'UTC'}
    if validated_epoch is not None:
        base_env['SOURCE_DATE_EPOCH'] = str(validated_epoch)
        base_env['FORCE_SOURCE_DATE'] = '1'

    with tempfile.TemporaryDirectory(prefix='eksmu-tex-build-') as tmp:
        tmp_root = Path(tmp)
        work_dir = tmp_root / 'work'
        work_dir.mkdir(mode=0o700)
        try:
            tex_sources, tex_files = validate_and_extract_archive(raw, work_dir, filename=filename)
            if main_file and main_file.strip():
                chosen = _validate_rel_path(main_file.strip())
                if chosen not in tex_sources:
                    raise ArchiveError('指定的主文件不在源码包中或不是 .tex 文件')
            else:
                chosen = select_main_tex(tex_sources, filename)
        except ArchiveError as exc:
            duration = round(time.monotonic() - started, 3)
            log_bytes = f'Archive validation failed: {exc}\n'.encode('utf-8')
            (out_dir / 'compile.log').write_bytes(log_bytes)
            tc = _detect_toolchain('pdflatex', base_env)
            result = {
                'status': 'failed',
                'engine': 'pdflatex',
                'main_file': main_file or '',
                'toolchain': tc,
                'pdf_sha256': None,
                'pages': None,
                'log_sha256': sha256_hex(log_bytes),
                'duration_s': duration,
                'error': str(exc),
                'html': {
                    'status': 'failed',
                    'filename': None,
                    'sha256': None,
                    'size': None,
                    'latexml_version': tc.get('latexml', ''),
                    'duration_s': 0.0,
                    'error': str(exc),
                },
            }
            (out_dir / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            return result

        engine = detect_engine(tex_sources, chosen)
        clean_env = {
            **base_env,
            'HOME': str(work_dir),
            'TMPDIR': str(work_dir),
            'TEXMFVAR': str(work_dir / '.texlive-var'),
            'TEXMFCACHE': str(work_dir / '.texlive-var'),
            'openin_any': 'p',
            'openout_any': 'p',
            'shell_escape': 'f',
            'max_print_line': '1000',
        }
        toolchain = _detect_toolchain(engine, clean_env)

        unsafe_reason = _check_sources_for_unsafe_primitives(work_dir)
        if unsafe_reason is not None:
            duration = round(time.monotonic() - started, 3)
            log_bytes = f'Security policy rejected source before execution: {unsafe_reason}\n'.encode('utf-8')
            (out_dir / 'compile.log').write_bytes(log_bytes)
            result = {
                'status': 'failed',
                'engine': engine,
                'main_file': chosen,
                'toolchain': toolchain,
                'pdf_sha256': None,
                'pages': None,
                'log_sha256': sha256_hex(log_bytes),
                'duration_s': duration,
                'error': unsafe_reason,
                'html': {
                    'status': 'failed',
                    'filename': None,
                    'sha256': None,
                    'size': None,
                    'latexml_version': toolchain.get('latexml', ''),
                    'duration_s': 0.0,
                    'error': unsafe_reason,
                },
            }
            (out_dir / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            return result

        # Normalize extracted file mtimes when SOURCE_DATE_EPOCH is fixed
        if validated_epoch is not None:
            for p_ext in work_dir.rglob('*'):
                try:
                    os.utime(p_ext, (validated_epoch, validated_epoch))
                except OSError:
                    pass

        # Keep a pristine copy of extracted sources before PDF compilation writes auxiliary files
        pristine_src_dir = tmp_root / 'pristine_src'
        shutil.copytree(work_dir, pristine_src_dir)

        # Seed per-job TEXMFVAR with pre-built luaotfload/luatexja caches if available
        _seed_texmfvar(work_dir / '.texlive-var')

        engine_flag = {'pdflatex': '-pdf', 'xelatex': '-xelatex', 'lualatex': '-lualatex'}[engine]
        main_path = work_dir / chosen
        run_cwd = main_path.parent
        main_arg = main_path.name
        extra_latex_opts: list[str] = []
        if engine == 'lualatex' and validated_epoch is not None:
            tid = hashlib.md5(f'{validated_epoch}:{chosen}:{sha256_hex(raw)}'.encode('utf-8')).hexdigest().upper()
            lua_init = work_dir / '.texlive-var' / 'eksmu-lualatex-init.lua'
            lua_init.parent.mkdir(parents=True, exist_ok=True)
            lua_init.write_text(
                f'if pdf and pdf.settrailerid then pdf.settrailerid("[<{tid}> <{tid}>]") end\n',
                encoding='utf-8',
            )
            extra_latex_opts.append(f'-latexoption=--lua={lua_init}')
        cmd = [
            'latexmk',
            engine_flag,
            '-interaction=nonstopmode',
            '-halt-on-error',
            '-file-line-error',
            '-no-shell-escape',
            '-latexoption=-no-shell-escape',
            *extra_latex_opts,
            '-recorder',
            main_arg,
        ]

        pdf_started = time.monotonic()
        returncode, stdout_text, timed_out = _run_step_with_timeout(cmd, run_cwd, clean_env, timeout_s)
        pdf_duration = round(time.monotonic() - pdf_started, 3)

        stem = main_path.stem
        tex_log_path = run_cwd / f'{stem}.log'
        fls_path = run_cwd / f'{stem}.fls'
        pdf_path = run_cwd / f'{stem}.pdf'

        fls_violation = _check_recorder_file(fls_path, work_dir, tex_bin)
        if fls_violation is not None:
            combined_log = f'Security policy rejected file access recorded in .fls: {fls_violation}\n'
        else:
            tex_log = tex_log_path.read_text(encoding='utf-8', errors='replace') if tex_log_path.is_file() else ''
            combined_log = (stdout_text + '\n' + tex_log).strip() + '\n'
        if sde_warning:
            combined_log = sde_warning + combined_log

        error_msg: str | None = None
        pdf_bytes: bytes | None = None
        pages: int | None = None
        if fls_violation is not None:
            error_msg = fls_violation
        elif timed_out:
            error_msg = f'编译超时（超过 {int(timeout_s)} 秒上限）'
        elif returncode != 0 or not pdf_path.is_file():
            error_msg = 'LaTeX 编译未通过，请查看编译日志'
        else:
            candidate_pdf = pdf_path.read_bytes()
            if not candidate_pdf.startswith(b'%PDF-'):
                error_msg = '生成的产物不是有效的 PDF 文件'
            elif len(candidate_pdf) > MAX_PDF_BYTES:
                error_msg = '生成的 PDF 超过 64 MiB 上限'
            else:
                counted = _count_pdf_pages(candidate_pdf, combined_log)
                if counted > MAX_PDF_PAGES:
                    error_msg = f'生成的 PDF 页数（{counted} 页）超过 2000 页上限'
                else:
                    pdf_bytes = candidate_pdf
                    pages = counted

        # Run LaTeXML HTML conversion in an isolated copy of pristine_src_dir
        # (unless .fls recorded a sandbox violation or PDF already timed out the whole job budget)
        if fls_violation is not None:
            html_result = {
                'status': 'failed',
                'filename': None,
                'sha256': None,
                'size': None,
                'latexml_version': toolchain.get('latexml', ''),
                'duration_s': 0.0,
                'error': fls_violation,
            }
            html_log = ''
        elif timed_out:
            html_result = {
                'status': 'failed',
                'filename': None,
                'sha256': None,
                'size': None,
                'latexml_version': toolchain.get('latexml', ''),
                'duration_s': 0.0,
                'error': '因 PDF 编译超时跳过 HTML 转换',
            }
            html_log = ''
        else:
            job_budget_s = min(
                float(DEFAULT_JOB_TIMEOUT_SECONDS) - 60.0,
                max(timeout_s + html_timeout_s - 60.0, html_timeout_s),
            )
            remaining_job_budget = max(0.0, min(html_timeout_s, job_budget_s - (time.monotonic() - started)))
            html_result, html_log = _compile_html_with_latexml(
                pristine_src_dir,
                tmp_root,
                chosen,
                out_dir,
                base_env,
                tex_bin,
                toolchain.get('latexml', ''),
                remaining_job_budget,
            )

        if html_log:
            combined_log = combined_log + '\n' + html_log
        log_bytes = combined_log.encode('utf-8', errors='replace')[:MAX_LOG_BYTES]
        (out_dir / 'compile.log').write_bytes(log_bytes)
        log_sha256 = sha256_hex(log_bytes)

        if error_msg is not None or pdf_bytes is None:
            result = {
                'status': 'failed',
                'engine': engine,
                'main_file': chosen,
                'toolchain': toolchain,
                'pdf_sha256': None,
                'pages': None,
                'log_sha256': log_sha256,
                'duration_s': pdf_duration,
                'error': error_msg,
                'html': html_result,
            }
            (out_dir / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            return result

        out_pdf = out_dir / f'{stem}.pdf'
        out_pdf.write_bytes(pdf_bytes)
        result = {
            'status': 'succeeded',
            'engine': engine,
            'main_file': chosen,
            'toolchain': toolchain,
            'pdf_sha256': sha256_hex(pdf_bytes),
            'pages': pages,
            'log_sha256': log_sha256,
            'duration_s': pdf_duration,
            'error': None,
            'html': html_result,
        }
        (out_dir / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        return result


def _fetch_metadata_token(token_url: str) -> str:
    req = urllib.request.Request(token_url, headers={'Metadata-Flavor': 'Google'}, method='GET')
    with urllib.request.urlopen(req, timeout=15) as resp:
        doc = json.loads(resp.read().decode('utf-8'))
    token = doc.get('access_token', '')
    if not token:
        raise RuntimeError('Missing access_token from metadata server')
    return token


def _gcs_download(api_endpoint: str, bucket: str, object_name: str, token: str) -> bytes:
    url = f"{api_endpoint.rstrip('/')}/storage/v1/b/{quote(bucket, safe='')}/o/{quote(object_name, safe='')}?alt=media"
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'}, method='GET')
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read(MAX_ARCHIVE_BYTES + 1)


def _gcs_upload(api_endpoint: str, bucket: str, object_name: str, data: bytes, content_type: str, token: str) -> None:
    url = (
        f"{api_endpoint.rstrip('/')}/upload/storage/v1/b/{quote(bucket, safe='')}/o"
        f"?uploadType=media&name={quote(object_name, safe='')}"
    )
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            'Authorization': f'Bearer {token}',
            'Content-Type': content_type,
            'Content-Length': str(len(data)),
        },
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        resp.read()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--local', nargs=2, metavar=('IN_FILE', 'OUT_DIR'), help='Compile a local archive without network access')
    parser.add_argument('--main-file', default=None, help='Optional main .tex path inside the archive')
    parser.add_argument('--timeout', type=float, default=DEFAULT_TIMEOUT_SECONDS, help='Compile timeout in seconds (default 1500)')
    parser.add_argument('--source-date-epoch', default=None, help='Optional SOURCE_DATE_EPOCH override (non-negative integer)')
    args = parser.parse_args(argv)

    raw_sde = args.source_date_epoch if args.source_date_epoch is not None else os.environ.get('SOURCE_DATE_EPOCH')

    if args.local:
        in_file, out_dir = Path(args.local[0]), Path(args.local[1])
        raw = in_file.read_bytes()
        compile_archive_bytes(
            raw,
            out_dir,
            filename=in_file.name,
            main_file=args.main_file,
            timeout_s=args.timeout,
            source_date_epoch=raw_sde,
        )
        return 0

    job_id = os.environ.get('JOB_ID', '').strip()
    bucket = (os.environ.get('COMPILE_BUCKET') or os.environ.get('EKSMU_COMPILE_BUCKET') or '').strip()
    main_file = args.main_file if args.main_file is not None else os.environ.get('MAIN_FILE', '').strip() or None
    source_filename = os.environ.get('SOURCE_FILENAME', '').strip() or f'{job_id}.src'
    api_endpoint = os.environ.get('EKSMU_GCS_API_ENDPOINT', GCS_API_ENDPOINT).strip() or GCS_API_ENDPOINT
    token_url = os.environ.get('EKSMU_GCS_TOKEN_URL', METADATA_TOKEN_URL).strip() or METADATA_TOKEN_URL

    if not job_id or not bucket or not re.fullmatch(r'[A-Za-z0-9._-]+', job_id):
        print('ERROR: JOB_ID and COMPILE_BUCKET are required', file=sys.stderr)
        return 2

    # Clear sensitive or unrelated environment variables before downloading/compiling.
    os.environ.clear()

    try:
        token = _fetch_metadata_token(token_url)
        raw = _gcs_download(api_endpoint, bucket, f'in/{job_id}.src', token)
        with tempfile.TemporaryDirectory(prefix='eksmu-job-out-') as tmp_out:
            out_dir = Path(tmp_out)
            result = compile_archive_bytes(
                raw,
                out_dir,
                filename=source_filename,
                main_file=main_file,
                timeout_s=args.timeout,
                source_date_epoch=raw_sde,
            )
            token = _fetch_metadata_token(token_url)
            stem = PurePosixPath(result['main_file'] or 'main.tex').stem
            pdf_file = out_dir / f'{stem}.pdf'
            if result['status'] == 'succeeded' and pdf_file.is_file():
                _gcs_upload(api_endpoint, bucket, f'out/{job_id}/{stem}.pdf', pdf_file.read_bytes(), 'application/pdf', token)
            html_info = result.get('html') if isinstance(result.get('html'), dict) else {}
            html_fname = str(html_info.get('filename') or f'{stem}.html')
            html_file = out_dir / PurePosixPath(html_fname).name
            if html_info.get('status') == 'succeeded' and html_file.is_file():
                _gcs_upload(
                    api_endpoint,
                    bucket,
                    f'out/{job_id}/{PurePosixPath(html_fname).name}',
                    html_file.read_bytes(),
                    'text/html; charset=utf-8',
                    token,
                )
            log_file = out_dir / 'compile.log'
            if log_file.is_file():
                _gcs_upload(api_endpoint, bucket, f'out/{job_id}/compile.log', log_file.read_bytes(), 'text/plain; charset=utf-8', token)
            result_bytes = (out_dir / 'result.json').read_bytes()
            _gcs_upload(api_endpoint, bucket, f'out/{job_id}/result.json', result_bytes, 'application/json; charset=utf-8', token)
        return 0
    except Exception as exc:
        print(f'ERROR: infrastructure failure in compile_entry: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
