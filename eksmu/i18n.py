"""Interface languages for the static pages and browser scripts.

Chinese source text is the message key; other languages map it through
``eksmu/locales/<lang>.json``. A key ``"tag§text"`` overrides ``"text"`` for
page text directly inside that HTML tag (``"a§发现"`` is the navigation
"Discover", while plain ``"发现"`` is the work type "Finding"). Only the interface is translated: author
content, discussion and uploaded material are never translated here.
"""
from __future__ import annotations

import contextvars
import html
import json
import re
from contextlib import contextmanager
from pathlib import Path

LANGS = ('zh', 'en')
DEFAULT = 'en'  # browsers that prefer neither language get English
LOCALES = Path(__file__).parent / 'locales'
STATIC = Path(__file__).parent / 'static'
PAGES = {'': 'index.html', 'publish': 'index.html', 'account': 'index.html', 'activity': 'index.html',
         'workspace': 'index.html', 'founding.html': 'founding.html', 'participation.html': 'participation.html',
         'openness.html': 'openness.html'}
# The home template holds every view; the server marks one and CSS shows it.
VIEWS = {'': 'discover', 'publish': 'publish', 'account': 'account', 'activity': 'activity', 'workspace': 'workspace'}
VIEW_TITLES = {'publish': '发表 · Eksmu', 'account': '账户 · Eksmu', 'activity': '我的动态 · Eksmu', 'workspace': '工作区 · Eksmu'}
HTML_LANG = {'zh': 'zh-CN', 'en': 'en'}
SWITCH_LABEL = {'zh': '中文', 'en': 'English'}
COOKIE = 'eksmu_lang'
CJK = re.compile(r'[㐀-鿿　-〿＀-￯]')
_TOKEN = re.compile(r'(<!--.*?-->|<[^>]+>)', re.S)
_ATTR = re.compile(r'(\s(?:placeholder|aria-label|title|content|value|alt)=")([^"]*)(")')
_cache: dict[str, tuple[float, dict[str, str]]] = {}
# Language of the current request (API errors, emails, renderer notes). API
# clients that do not send X-Eksmu-Lang keep the original Chinese messages.
LANG: contextvars.ContextVar[str] = contextvars.ContextVar('eksmu_lang', default='zh')


def current() -> str:
    return LANG.get()


@contextmanager
def use(lang: str):
    token = LANG.set(lang if lang in LANGS else 'zh')
    try:
        yield
    finally:
        LANG.reset(token)


def localize(message: str) -> str:
    """Translate a fixed message into the current request language."""
    return t(current(), message)


def strings(lang: str) -> dict[str, str]:
    """Load one dictionary; re-read when the file changes (static hot updates)."""
    if lang == 'zh':
        return {}
    path = LOCALES / f'{lang}.json'
    mtime = path.stat().st_mtime
    cached = _cache.get(lang)
    if cached is None or cached[0] != mtime:
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
            raise ValueError(f'Invalid locale file: {lang}')
        cached = (mtime, data)
        _cache[lang] = cached
    return cached[1]


def negotiate(accept_language: str | None, cookie: str | None) -> str:
    """Saved choice first, then the browser's ordered preferences."""
    for part in (cookie or '').split(';'):
        name, _, value = part.strip().partition('=')
        if name == COOKIE and value in LANGS:
            return value
    ranked = []
    for index, item in enumerate((accept_language or '')[:512].split(',')):
        tag, _, params = item.strip().partition(';')
        quality = 1.0
        match = re.search(r'q=([^;\s]*)', params)
        if match:
            if not re.fullmatch(r'(?:0(?:\.\d{0,3})?|1(?:\.0{0,3})?)', match.group(1)):
                continue
            quality = float(match.group(1))
        primary = tag.strip().lower().split('-')[0]
        if quality > 0 and primary in LANGS:
            ranked.append((-quality, index, primary))
    return min(ranked)[2] if ranked else DEFAULT


WORK_PATH = re.compile(r'w/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
AUTHOR_PATH = re.compile(r'u/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')


def split_page_path(path: str) -> tuple[str, str] | None:
    """Map '/en/founding.html' to ('en', 'founding.html') and '/zh/w/<id>' to
    ('zh', 'w/<id>'); None if not a page."""
    parts = path.split('/', 2)
    if len(parts) == 3 and parts[0] == '' and parts[1] in LANGS and (
        parts[2] in PAGES or WORK_PATH.fullmatch(parts[2]) or AUTHOR_PATH.fullmatch(parts[2])
    ):
        return parts[1], parts[2]
    return None


def t(lang: str, message: str, **params) -> str:
    """Server-side counterpart of the browser's tr()."""
    value = strings(lang).get(message, message)
    return re.sub(r'\{(\w+)\}', lambda m: str(params[m.group(1)]) if m.group(1) in params else m.group(0), value)


def translate(text: str, lang: str, tag: str = '') -> str:
    if lang == 'zh' or not CJK.search(text):
        return text
    core = text.strip()
    if not core:
        return text
    lead = text[:len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()):]
    table = strings(lang)
    return lead + table.get(f'{tag}§{core}', table.get(core, core)) + trail


def html_messages(markup: str) -> list[str]:
    """Every Chinese text node or user-visible attribute in a template."""
    found = []
    for token in _TOKEN.split(markup):
        if token.startswith('<!--'):
            continue
        if token.startswith('<'):
            found.extend(m.group(2).strip() for m in _ATTR.finditer(token) if CJK.search(m.group(2)))
        elif CJK.search(token):
            found.append(token.strip())
    return found


def _links(markup: str, lang: str) -> str:
    markup = re.sub(r'href="/(#[^"]*)?"', lambda m: f'href="/{lang}/{m.group(1) or ""}"', markup)
    markup = re.sub(r'href="/(publish|account|activity|workspace)(#[^"]*)?"',
                    lambda m: f'href="/{lang}/{m.group(1)}{m.group(2) or ""}"', markup)
    return re.sub(r'href="/(founding|participation|openness)\.html(#[^"]*)?"',
                  lambda m: f'href="/{lang}/{m.group(1)}.html{m.group(2) or ""}"', markup)


def render(template: str, lang: str, page: str | None) -> bytes:
    """Translate one static template. ``page`` is its language-neutral path, or
    None for pages without stable alternates (the snapshot view)."""
    markup = (STATIC / template).read_text(encoding='utf-8')
    out = []
    tag = ''
    for token in _TOKEN.split(markup):
        if token.startswith('<!--'):
            out.append(token)
        elif token.startswith('<'):
            out.append(_ATTR.sub(lambda m: m.group(1) + translate(m.group(2), lang) + m.group(3), token))
            tag = re.match(r'</?([a-zA-Z0-9]*)', token).group(1).lower()
        else:
            out.append(translate(token, lang, tag))
    markup = _links(''.join(out), lang)
    view = VIEWS.get(page) if template == 'index.html' and page is not None else None
    if view:
        markup = markup.replace('<body>', f'<body data-view="{view}">', 1)
        if page in VIEW_TITLES:
            markup = re.sub(r'<title>[^<]*</title>', f'<title>{html.escape(t(lang, VIEW_TITLES[page]))}</title>', markup, count=1)
        nav = {'': f'/{lang}/', 'publish': f'/{lang}/publish', 'activity': f'/{lang}/activity'}.get(page)
        if nav:
            markup = re.sub(rf'<a\s+href="{re.escape(nav)}"(?![^>]*aria-current)([^>]*)>',
                            rf'<a href="{nav}" aria-current="page"\1>', markup, count=1)
    markup = markup.replace('<html lang="zh-CN">', f'<html lang="{HTML_LANG[lang]}">', 1)
    head = [f'<script src="/i18n/{lang}.js" defer></script>']
    if page is not None:
        stable = page.split('?', 1)[0]
        head += [f'<link rel="alternate" hreflang="{HTML_LANG[other]}" href="/{other}/{stable}">' for other in LANGS]
        head.append(f'<link rel="alternate" hreflang="x-default" href="/{stable}">')
    markup = markup.replace('<!--i18n:head-->', ''.join(head), 1)
    target = html.escape(page or '', quote=True)
    joiner = '&amp;' if '?' in (page or '') else '?'
    switch = ''.join(
        f'<a class="lang-switch" href="/{other}/{target}{joiner}lang=switch" hreflang="{HTML_LANG[other]}" lang="{HTML_LANG[other]}">{SWITCH_LABEL[other]}</a>'
        for other in LANGS if other != lang)
    markup = markup.replace('<!--i18n:switch-->', switch, 1)
    return markup.encode('utf-8')


def script(lang: str) -> bytes:
    """Browser dictionary: only messages that browser scripts actually use."""
    source = ''.join(p.read_text(encoding='utf-8') for p in sorted(STATIC.glob('*.js')))
    table = {k: v for k, v in strings(lang).items() if k in source or not page_only(k)}
    body = json.dumps(table, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return (
        '"use strict";(()=>{const S=' + body + ',has=Object.prototype.hasOwnProperty;'
        'window.EKSMU_LANG=' + json.dumps(lang) + ';'
        'window.tr=(s,p)=>{let r=has.call(S,s)?S[s]:s;'
        'if(p)r=r.replace(/\\{(\\w+)\\}/g,(m,k)=>has.call(p,k)?String(p[k]):m);return r;};'
        'const sync=()=>{for(const a of document.querySelectorAll("a.lang-switch")){'
        'const u=new URL(a.href);u.hash=location.hash;a.href=u.href;}};'
        'document.addEventListener("DOMContentLoaded",sync);window.addEventListener("hashchange",sync);})();\n'
    ).encode('utf-8')


_page_only: set[str] | None = None


def page_only(message: str) -> bool:
    """True for template text; such messages are translated on the server."""
    global _page_only
    if _page_only is None:
        _page_only = {m for p in STATIC.glob('*.html') for m in html_messages(p.read_text(encoding='utf-8'))}
    return message in _page_only


def is_page_path(path: str) -> bool:
    """Top-level pages a cross-site link may open (never APIs or assets)."""
    return (path[1:] in PAGES or split_page_path(path) is not None
            or bool(WORK_PATH.fullmatch(path[1:]) or AUTHOR_PATH.fullmatch(path[1:])))
