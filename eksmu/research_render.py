"""Local Markdown/LaTeX reading. No remote assets, scripts or document execution."""
from __future__ import annotations

from collections import OrderedDict
import functools
from html import escape
from .i18n import current, localize, t
from html.parser import HTMLParser
import re
import shutil
import subprocess
import threading
from urllib.parse import urlsplit

from .store import text

RENDERER_VERSION = "eksmu.markdown-mathml.v1"
SLOTS = threading.BoundedSemaphore(2)
TAGS = set("p br hr h1 h2 h3 h4 h5 h6 strong em del sup sub ul ol li blockquote pre code span div table thead tbody tr th td caption figure figcaption a img dl dt dd math semantics annotation mrow mi mo mn mtext mspace mfrac msup msub msubsup msqrt mroot munder mover munderover mtable mtr mtd mstyle mpadded mphantom menclose".split())
VOID = {"br", "hr", "img"}
# Layout-only tags pandoc emits for wide tables; dropped silently (escaping them showed raw <colgroup> text).
DROP = {"colgroup", "col"}
MATH_ATTRS = {"display", "mathvariant", "stretchy", "accent", "accentunder", "columnalign", "rowalign", "columnspacing", "rowspacing", "linethickness", "fence", "separator", "form", "encoding"}
IMAGE = re.compile(r"/api/research/preview-file\?attachment_id=[0-9a-f-]{36}\Z")


class ReaderHTML(HTMLParser):
    """Rebuild only our reading vocabulary; never forward arbitrary HTML attributes."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.output = []
        self.stack = []

    def handle_starttag(self, tag, attrs):
        if tag in DROP:
            return
        attrs = dict(attrs)
        if tag not in TAGS:
            self.handle_data(self.get_starttag_text())
            return
        if tag == "img" and not IMAGE.fullmatch(attrs.get("src", "")):
            alt = attrs.get("alt") or localize("请使用本版附件")
            self.output.append('<span class="external-image-note">' + escape(t(current(), "[图片：{alt}]", alt=alt)) + '</span>')
            return
        clean = {}
        for key, value in attrs.items():
            if value is None:
                continue
            if key == "class" and re.fullmatch(r"[A-Za-z0-9_ -]{1,120}", value):
                clean[key] = value
            elif key == "id" and re.fullmatch(r"[\w.:-]{1,160}", value):
                clean[key] = "eksmu-doc-" + value
            elif key in {"alt", "title"}:
                clean[key] = value[:500]
            elif key == "href" and tag == "a":
                try:
                    url = urlsplit(value)
                    if not any(ord(c) < 32 for c in value) and (value.startswith('#') or (url.scheme in {'http', 'https', 'mailto'})):
                        clean[key] = "#eksmu-doc-" + value[1:] if value.startswith("#") else value
                except ValueError:
                    pass
            elif tag == "img" and key == "src":
                clean[key] = value
            elif (tag.startswith("m") or tag == "annotation") and key in MATH_ATTRS and re.fullmatch(r"[A-Za-z0-9 .,+%/_-]{1,100}", value):
                clean[key] = value
            elif tag in {"td", "th"} and key in {"colspan", "rowspan"} and value.isdecimal() and int(value) < 100:
                clean[key] = value
        if tag == "a":
            clean.update(rel="ugc nofollow noopener noreferrer")
            if not clean.get("href", "").startswith("#"):
                clean["target"] = "_blank"
        if tag == "math":
            clean["xmlns"] = "http://www.w3.org/1998/Math/MathML"
        if tag == "img":
            clean.update(loading="lazy", decoding="async")
        self.output.append('<' + tag + ''.join(' ' + k + '="' + escape(v, quote=True) + '"' for k, v in clean.items()) + '>')
        if tag not in VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in DROP:
            return
        if tag in self.stack:
            # Always balance our own nesting, regardless of parser output.
            while self.stack:
                item = self.stack.pop(); self.output.append('</' + item + '>')
                if item == tag:
                    break

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_data(self, data):
        self.output.append(escape(data))

    def finish(self):
        while self.stack:
            self.output.append('</' + self.stack.pop() + '>')
        return ''.join(self.output)


def sanitize(fragment: str) -> str:
    parser = ReaderHTML(); parser.feed(fragment); parser.close()
    return parser.finish()


def plain(source: str, reason: str) -> dict:
    return {"html": '<pre class="source-fallback">' + escape(source) + '</pre>',
            "mode": "plain", "renderer": RENDERER_VERSION, "notice": reason}


def _render(source: str, executable: str | None) -> dict:
    if not executable:
        return plain(source, "安装 Pandoc 后可启用公式、表格与代码排版；原文已完整保留。")
    if not SLOTS.acquire(blocking=False):
        return plain(source, "排版服务繁忙，当前显示原文。")
    try:
        result = subprocess.run([executable, '--sandbox', '--from=markdown-raw_html-raw_tex-yaml_metadata_block',
                                 '--to=html5', '--mathml', '--wrap=none'],
                                input=source, text=True, encoding='utf-8', capture_output=True,
                                timeout=4, env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}, check=False)
        if result.returncode or len(result.stdout) > 1_000_000:
            return plain(source, "本次排版未完成，当前显示原文。")
        return {"html": sanitize(result.stdout), "mode": "formatted", "renderer": RENDERER_VERSION,
                "notice": "部分公式语法未能排版，已保留其原文。" if "Could not convert" in result.stderr else ""}
    except (OSError, subprocess.TimeoutExpired):
        return plain(source, "本次排版未完成，当前显示原文。")
    finally:
        SLOTS.release()


_RE_FENCED = re.compile(r'(?ms)^\s*(```|~~~).*?^\s*\1\s*$')
_RE_DISPLAY_MATH = re.compile(r'\$\$.*?\$\$', flags=re.S)
_RE_IMAGE = re.compile(r'!\[([^\]]*)\]\([^)]*\)')
_RE_LINK = re.compile(r'\[([^\]]*)\]\([^)]*\)')
_RE_TAG = re.compile(r'<[^>\n]+>')
_RE_PREFIX = re.compile(r'(?m)^\s*(?:#+|>+|[-*+]|\d+[.)])\s+')
_RE_RULE = re.compile(r'(?m)^\s*(?:[-*_]\s*){3,}$|^\s*\|?[\s:|-]+\|[\s:|-]*$')
_RE_INLINE = re.compile(r'[*`$]+|~~|\\(?=[^A-Za-z])')


@functools.lru_cache(maxsize=4096)
def excerpt(source: str, limit: int = 180) -> str:
    """Plain-text teaser of Markdown source for cards, titles and meta tags.

    Cutting the source and rendering it would leave unbalanced syntax, so the
    teaser drops markup instead; the full text is rendered on the work page.
    """
    plain = _RE_FENCED.sub(' ', source)
    plain = _RE_DISPLAY_MATH.sub(' ', plain)
    plain = _RE_IMAGE.sub(r'\1', plain)
    plain = _RE_LINK.sub(r'\1', plain)
    plain = _RE_TAG.sub(' ', plain)
    plain = _RE_PREFIX.sub('', plain)
    plain = _RE_RULE.sub(' ', plain)
    plain = _RE_INLINE.sub('', plain).replace('|', ' ')
    plain = ' '.join(plain.split())
    return plain if len(plain) <= limit else plain[:limit - 1].rstrip() + '…'


def render(source: str) -> dict:
    text(source, 50000, True)
    return dict(_render(source, shutil.which('pandoc')))


_MATH_TOKEN_RE = re.compile(
    r'\\\$'
    r'|\$\$[\s\S]+?\$\$'
    r'|\$(?!\s)(?:\\.|[^$\\\n])+?(?<!\s)\$(?!\d)'
    r'|\\\[[\s\S]+?\\\]'
    r'|\\\([\s\S]+?\\\)'
)
_ASCII_PUNCT_RE = re.compile(r'([!"#$%&\'()*+,\-./:;<=>?@\[\\\]^_`{|}~])')
_ABSTRACT_CACHE_MAX = 1024
_ABSTRACT_CACHE: OrderedDict[tuple[str, str, str], dict] = OrderedDict()
_ABSTRACT_CACHE_LOCK = threading.Lock()


def _escape_non_math_markdown(source: str) -> str:
    """Escape Markdown syntax outside inline/display TeX math spans so only math is rendered."""
    normalized = re.sub(r'(?m)^[ \t]+', '', source)
    parts: list[str] = []
    last = 0
    for m in _MATH_TOKEN_RE.finditer(normalized):
        parts.append(_ASCII_PUNCT_RE.sub(r'\\\1', normalized[last:m.start()]))
        tok = m.group(0)
        if tok == r'\$':
            parts.append(r'\$')
        elif tok.startswith('$$') or tok.startswith(r'\['):
            inner = tok[2:-2].strip()
            parts.append(f'$${inner}$$' if inner else _ASCII_PUNCT_RE.sub(r'\\\1', tok))
        else:
            inner = (tok[1:-1] if tok.startswith('$') else tok[2:-2]).strip()
            parts.append(f'${inner}$' if inner else _ASCII_PUNCT_RE.sub(r'\\\1', tok))
        last = m.end()
    parts.append(_ASCII_PUNCT_RE.sub(r'\\\1', normalized[last:]))
    return ''.join(parts)


def plain_abstract(source: str, reason: str = '') -> dict:
    return {
        'html': '<p>' + escape(source) + '</p>',
        'mode': 'plain',
        'renderer': RENDERER_VERSION,
        'notice': reason,
    }


def _render_abstract(source: str, executable: str | None) -> dict:
    if not executable:
        return plain_abstract(source, '安装 Pandoc 后可启用公式、表格与代码排版；原文已完整保留。')
    cache_key = (RENDERER_VERSION, executable, source)
    with _ABSTRACT_CACHE_LOCK:
        cached = _ABSTRACT_CACHE.get(cache_key)
        if cached is not None:
            _ABSTRACT_CACHE.move_to_end(cache_key)
            return dict(cached)
    if not SLOTS.acquire(blocking=False):
        return plain_abstract(source, '排版服务繁忙，当前显示原文。')
    try:
        escaped_input = _escape_non_math_markdown(source)
        result = subprocess.run(
            [
                executable,
                '--sandbox',
                '--from=markdown-raw_html-raw_tex-yaml_metadata_block',
                '--to=html5',
                '--mathml',
                '--wrap=none',
            ],
            input=escaped_input,
            text=True,
            encoding='utf-8',
            capture_output=True,
            timeout=4,
            env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'},
            check=False,
        )
        if result.returncode or len(result.stdout) > 1_000_000:
            return plain_abstract(source, '本次排版未完成，当前显示原文。')
        out = {
            'html': sanitize(result.stdout),
            'mode': 'formatted',
            'renderer': RENDERER_VERSION,
            'notice': '部分公式语法未能排版，已保留其原文。' if 'Could not convert' in result.stderr else '',
        }
    except (OSError, subprocess.TimeoutExpired):
        return plain_abstract(source, '本次排版未完成，当前显示原文。')
    finally:
        SLOTS.release()
    with _ABSTRACT_CACHE_LOCK:
        _ABSTRACT_CACHE[cache_key] = out
        _ABSTRACT_CACHE.move_to_end(cache_key)
        while len(_ABSTRACT_CACHE) > _ABSTRACT_CACHE_MAX:
            _ABSTRACT_CACHE.popitem(last=False)
    return dict(out)


def render_abstract(source: str) -> dict:
    text(source, 6000, True)
    return dict(_render_abstract(source, shutil.which('pandoc')))
