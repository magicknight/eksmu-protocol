"""The manuscript text an AI analysis reads: title, abstract, body and the full text of the attachments.

Many works keep the paper itself in an attachment (a LaTeX source package, or the HTML version the site
compiled from it) and use the body only for a note. The analysis must read the attachment, otherwise
the model sees a bibliographic record and nothing to review.

Order of preference for the attachment text:
1. the author's LaTeX source package: the main file with \\input/\\include/\\subfile expanded inside the
   archive, comments removed, and the .bbl bibliography appended;
2. the HTML version compiled by the site, as plain text with TeX from the MathML alttext;
3. PDF only: not read. The web host does not run a parser over untrusted PDFs; the report says so.

Archives are read in memory with the same validation as uploads (inspect_source_archive). Everything
returned is untrusted author data; callers keep it inside <paper> tags.
"""
from __future__ import annotations

from html.parser import HTMLParser
from io import BytesIO
from pathlib import PurePosixPath
import posixpath
import re
import tarfile
import zipfile

from .research import (archive_ext, inspect_source_archive, is_source_archive_filename,
                       select_main_tex, _strip_tex_comments, _validate_archive_rel_path)

MAX_MANUSCRIPT_CHARS = 300_000
MAX_INCLUDE_DEPTH = 20
# Repeated (non-cyclic) includes can multiply exponentially: A includes B twice, B includes C twice, …
# Cap the work, not just the depth; the web server calls this on uploads (/api/research/inspect-source).
MAX_INCLUDE_EXPANSIONS = 500
MAX_EXPANDED_CHARS = 2_000_000
MAX_BBL_BYTES = 5 * 1024 * 1024
TRUNCATED = '\n\n[truncated at {n} characters]\n'

_INCLUDE = re.compile(r'\\(input|include|subfile)\s*\{([^{}]+)\}|\\(?:sub)?import\*?\s*\{([^{}]*)\}\s*\{([^{}]+)\}')


def _resolve(tex_sources: dict[str, str], name: str, base_dir: str = '') -> str | None:
    name = name.strip().strip('"')
    if not name:
        return None
    for root in ([base_dir, ''] if base_dir else ['']):
        joined = posixpath.normpath(posixpath.join(root, name))
        try:
            rel = _validate_archive_rel_path(joined)
        except Exception:
            return None
        for cand in (rel, rel + '.tex'):
            if cand in tex_sources:
                return cand
    return None


def flatten_latex(tex_sources: dict[str, str], main_file: str) -> tuple[str, list[str]]:
    """Expand \\input, \\include, \\subfile and \\import inside the archive. Returns (text, files used)."""
    used: list[str] = []
    base = posixpath.dirname(main_file)   # TeX resolves \input relative to the directory it runs in
    budget = {'calls': 0, 'chars': 0}

    def expand(path: str, stack: tuple[str, ...]) -> str:
        if path not in used:
            used.append(path)
        content = _strip_tex_comments(tex_sources[path])
        budget['calls'] += 1
        budget['chars'] += len(content)

        def replace(m: re.Match) -> str:
            if m.group(1):
                target = _resolve(tex_sources, m.group(2), base)
            else:
                target = _resolve(tex_sources, posixpath.join(m.group(3) or '', m.group(4)), base)
            if target is None:
                return m.group(0)
            if target in stack or len(stack) >= MAX_INCLUDE_DEPTH:
                return f'% [Eksmu: {target} not expanded again]'
            if budget['calls'] >= MAX_INCLUDE_EXPANSIONS or budget['chars'] >= MAX_EXPANDED_CHARS:
                return f'% [Eksmu: {target} not expanded: include limit reached]'
            return '\n' + expand(target, stack + (target,)) + '\n'

        return _INCLUDE.sub(replace, content)

    return expand(main_file, (main_file,)), used


def _bibliography(raw: bytes, filename: str, main_file: str) -> tuple[str, str] | None:
    """The .bbl next to the main file (or the only .bbl), as (name, text)."""
    kind = archive_ext(filename)
    want = str(PurePosixPath(main_file).with_suffix('.bbl'))
    found: dict[str, bytes] = {}
    try:
        if kind == '.zip':
            with zipfile.ZipFile(BytesIO(raw)) as zf:
                for info in zf.infolist():
                    if not info.is_dir() and info.filename.lower().endswith('.bbl') and info.file_size <= MAX_BBL_BYTES:
                        found[_validate_archive_rel_path(info.filename)] = zf.read(info)
        else:
            with tarfile.open(fileobj=BytesIO(raw), mode='r:gz' if kind in {'.tar.gz', '.tgz'} else 'r:') as tf:
                for member in tf:
                    if member.isfile() and member.name.lower().endswith('.bbl') and member.size <= MAX_BBL_BYTES:
                        fobj = tf.extractfile(member)
                        if fobj is not None:
                            found[_validate_archive_rel_path(member.name)] = fobj.read(MAX_BBL_BYTES + 1)[:MAX_BBL_BYTES]
    except Exception:
        return None
    name = want if want in found else (next(iter(found)) if len(found) == 1 else None)
    return (name, found[name].decode('utf-8', errors='replace')) if name else None


def latex_text(raw: bytes, filename: str, main_file: str | None = None) -> dict:
    """Full text of a LaTeX source package; raises Problem for archives the upload check would reject."""
    tex_sources = inspect_source_archive(raw, filename)
    main = main_file if main_file in tex_sources else select_main_tex(tex_sources, filename)
    body, used = flatten_latex(tex_sources, main)
    bib = _bibliography(raw, filename, main)
    if bib is not None:
        body += f'\n\n% ---- bibliography ({bib[0]}) ----\n' + bib[1]
    return {'text': body, 'main_file': main, 'files': used, 'bibliography': bib[0] if bib else None}


class _HtmlText(HTMLParser):
    BLOCK = {'p', 'div', 'section', 'article', 'li', 'tr', 'br', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
             'figcaption', 'table', 'blockquote', 'pre', 'dt', 'dd', 'header', 'footer'}
    SKIP = {'script', 'style', 'head', 'title', 'template', 'noscript'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0
        self.math = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag == 'math':
            if self.math == 0 and not self.skip:
                alt = dict(attrs).get('alttext') or ''
                display = dict(attrs).get('display') == 'block'
                self.out.append(f'\n$${alt}$$\n' if display else f'${alt}$')
            self.math += 1
        elif tag in self.BLOCK and not self.skip and not self.math:
            self.out.append('\n')

    def handle_startendtag(self, tag, attrs):
        if tag == 'br' and not self.skip and not self.math:
            self.out.append('\n')

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag == 'math':
            self.math = max(0, self.math - 1)
        elif tag in self.BLOCK and not self.skip and not self.math:
            self.out.append('\n')

    def handle_data(self, data):
        if not self.skip and not self.math:
            self.out.append(data)


def html_text(raw: bytes) -> str:
    parser = _HtmlText()
    parser.feed(raw.decode('utf-8', errors='replace'))
    parser.close()
    text = ''.join(parser.out)
    text = re.sub(r'[ \t\r\f\v]+', ' ', text)
    return re.sub(r'\n\s*\n\s*(\n\s*)+', '\n\n', text).strip()


def _head(revision: dict) -> str:
    p = revision['payload']
    paper = p.get('paper') or {}
    parts = [f"# {p['title']}"]
    authors = [a.get('name', '') for a in paper.get('authors') or [] if isinstance(a, dict) and a.get('name')]
    if authors:
        parts.append('Authors: ' + ', '.join(authors))
    if paper.get('abstract'):
        parts.append('## Abstract\n\n' + paper['abstract'])
    parts.append(p['body'])
    return '\n\n'.join(parts)


def _describe(inputs: list[dict], extra: dict) -> str:
    said = ['title, abstract and body of this revision' if extra.get('abstract') else 'title and full body of this revision']
    for item in inputs:
        if item['kind'] == 'source' and item['read']:
            files = extra.get('files') or []
            more = f" and {len(files) - 1} included files" if len(files) > 1 else ''
            bib = f", bibliography {extra['bibliography']}" if extra.get('bibliography') else ''
            said.append(f"LaTeX source {item['filename']} ({extra.get('main_file')}{more}{bib}; {item['chars']} characters"
                        f"{', truncated' if item['truncated'] else ''})")
        elif item['kind'] == 'html' and item['read']:
            said.append(f"HTML version compiled by Eksmu {item['filename']} ({item['chars']} characters"
                        f"{', truncated' if item['truncated'] else ''})")
        elif item['kind'] == 'pdf':
            said.append(f"PDF {item['filename']} not read")
    return '; '.join(said)[:1000]


def build_manuscript(revision: dict, research=None, limit: int = MAX_MANUSCRIPT_CHARS) -> dict:
    """{'text', 'inputs', 'read'}: what the model receives and how the report describes it."""
    head = _head(revision)
    if len(head) > limit:
        head = head[:limit] + TRUNCATED.format(n=limit)
    inputs = [{'kind': 'body', 'filename': '', 'sha256': revision['sha256'], 'chars': len(head),
               'truncated': head.endswith(TRUNCATED.format(n=limit)), 'read': True}]
    extra = {'abstract': bool((revision['payload'].get('paper') or {}).get('abstract'))}
    rows = []
    if research is not None:
        rows = research.db.execute('SELECT * FROM research_files WHERE revision_id=? ORDER BY rowid',
                                   (revision['id'],)).fetchall()
    sources = [r for r in rows if r['origin'] == 'upload' and is_source_archive_filename(r['filename'])]
    htmls = [r for r in rows if r['origin'] == 'compiled' and r['mime'] == 'text/html']
    pdfs = [r for r in rows if r['mime'] == 'application/pdf']
    attachment, label = '', ''
    for row in sources:
        job = research.db.execute("SELECT main_file FROM compile_jobs WHERE source_attachment_id=? "
                                  "ORDER BY (state='succeeded') DESC, rowid DESC LIMIT 1", (row['id'],)).fetchone()
        try:
            got = latex_text(research._read_file_bytes(row), row['filename'], job['main_file'] if job else None)
        except Exception:
            inputs.append({'kind': 'source', 'filename': row['filename'], 'sha256': row['sha256'], 'chars': 0,
                           'truncated': False, 'read': False})
            continue
        attachment = got['text']
        label = f"LaTeX source package {row['filename']}, main file {got['main_file']}"
        extra.update(main_file=got['main_file'], files=got['files'], bibliography=got['bibliography'])
        chosen = ('source', row)
        break
    else:
        chosen = None
        for row in htmls:
            try:
                attachment = html_text(research._read_file_bytes(row))
            except Exception:
                continue
            label = f"HTML version {row['filename']} compiled by Eksmu from the author's source"
            chosen = ('html', row)
            break
    text = head
    if chosen is not None:
        room = max(0, limit - len(head))
        truncated = len(attachment) > room
        if truncated:
            attachment = attachment[:room] + TRUNCATED.format(n=room)
        text = f"{head}\n\n---\n\n## Full text from the attachment ({label})\n\n{attachment}"
        kind, row = chosen
        inputs.append({'kind': kind, 'filename': row['filename'], 'sha256': row['sha256'], 'chars': len(attachment),
                       'truncated': truncated, 'read': True})
    elif pdfs:
        for row in pdfs:
            inputs.append({'kind': 'pdf', 'filename': row['filename'], 'sha256': row['sha256'], 'chars': 0,
                           'truncated': False, 'read': False})
    return {'text': text, 'inputs': inputs[:10], 'read': _describe(inputs, extra)}
