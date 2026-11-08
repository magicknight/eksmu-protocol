"""Loopback application server; public ingress requires a separately reviewed proxy."""
from __future__ import annotations

import json
import ipaddress
import re
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs, quote, urlencode

from .store import Problem, Store, canonical, identifier
from . import i18n
from .i18n import is_page_path
from .maintenance import InstanceLock, RequestLock
from .accounts import Accounts
from .auth_limits import AuthLimiter, LIMITS
from .modern_accounts import ModernAccounts, MailDispatcher, smtp_sender_from_environment
from .blobstore import (
    MAX_CONCURRENT_LARGE_REQUESTS,
    MAX_IMPORT_MATERIALS_REQUEST,
    MAX_UPLOAD_REQUEST,
)
from http.cookies import SimpleCookie, CookieError
import threading
import time

MAX_REQUEST = 256 * 1024
LARGE_REQUEST_SEMAPHORE = threading.BoundedSemaphore(MAX_CONCURRENT_LARGE_REQUESTS)
STATIC = Path(__file__).parent / "static"
FONTS = {'chakra-petch-500.woff2', 'chakra-petch-700.woff2', 'jetbrains-mono-500.woff2'}
AUTH_POST_ROUTES = {'/api/auth/' + action for action in
                    set(LIMITS) | {'profile', 'logout', 'revoke', 'revoke-others'}}
_UUID_RE = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
CARD_SVG_PATH = re.compile(rf'/(?:(zh|en)/)?w/({_UUID_RE})/card\.svg')
WORK_PDF_PATH = re.compile(rf'/(?:(zh|en)/)?w/([^/]+)/pdf/([^/]+)')
WORK_HTML_PATH = re.compile(rf'/(?:(zh|en)/)?w/([^/]+)/html/([^/]+)')
BADGE_SVG_PATH = re.compile(rf'/api/badge/({_UUID_RE})\.svg')
PUBLIC_DISCOVERY_PATHS = {'/robots.txt', '/sitemap.xml', '/feed.xml'}


def load_json(raw: bytes) -> dict:
    def pairs(items):
        obj = {}
        for key, value in items:
            if key in obj:
                raise Problem("Duplicate JSON key")
            obj[key] = value
        return obj

    def constant(_):
        raise Problem("Non-finite JSON number")

    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise Problem("Invalid JSON document") from exc
    if not isinstance(value, dict):
        raise Problem("A JSON object is required")
    return value


class _InstanceServer(ThreadingHTTPServer):
    # The stdlib default backlog is 5; behind nginx a burst of new upstream connections would be dropped
    # and retried after the 3 s proxy_connect_timeout. Each accepted request gets its own thread anyway.
    request_queue_size = 128

    def server_close(self):
        try:
            super().server_close()
        finally:
            dispatcher = getattr(self, '_mail_dispatcher', None)
            if dispatcher is not None:
                dispatcher.close()
            lease = getattr(self, '_instance_lease', None)
            if lease is not None:
                lease.close()


def _proxy_origin(value: str | None) -> tuple[str, str] | None:
    """Optional HTTPS origin for a separately approved loopback reverse proxy."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError('Proxy origin must be an HTTPS domain')
    parsed = urlsplit(value)
    host = parsed.hostname or ''
    labels = host.split('.')
    if (parsed.scheme != 'https' or parsed.netloc != host or parsed.path or parsed.query
            or parsed.fragment or len(labels) < 2 or len(value) > 253
            or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                   for label in labels)):
        raise ValueError('Proxy origin must be a bare lowercase HTTPS domain')
    return value, host


def make_server(database: str | Path, port: int = 8765, *, verification: bool = False,
                public_origin: str | None = None, mail_sender=None) -> ThreadingHTTPServer:
    proxy = _proxy_origin(public_origin)
    lease = InstanceLock(database)
    try:
        server = _make_server(database, port, verification=verification, proxy=proxy,
                              mail_sender=mail_sender if mail_sender is not None else smtp_sender_from_environment())
        server._instance_lease = lease
        return server
    except BaseException:
        lease.close()
        raise


def _make_server(database: str | Path, port: int, *, verification: bool,
                 proxy: tuple[str, str] | None, mail_sender) -> ThreadingHTTPServer:
    database = str(database)
    init = Store(database)
    try:
        ModernAccounts(init, mail_sender, require_verified_email=bool(proxy))
    finally:
        init.close()
    # Hash the runtime files once at start-up (the code Python has just loaded); see source_attestation.
    from .source_attestation import attestation
    attestation()
    # Optional component: a missing dependency or unavailable verification DB
    # must not turn into a publication prerequisite.
    bridge_factory = None
    signer = None
    if verification:
        try:
            from eksmu_bridge.service import Tasks, load_key
            key_path = Path(database + ".receipt-signer.key")
            if Path(database + ".receipt-tasks.sqlite").exists() and not key_path.exists():
                raise ValueError("Existing verification identity requires key recovery")
            signer = load_key(key_path)
            check = Tasks(database + ".receipt-tasks.sqlite", signer)
            check.close()
            bridge_factory = Tasks
        except (ImportError, OSError, ValueError, sqlite3.Error):
            pass

    auth_limiter = AuthLimiter(database=database if proxy else None)
    dispatcher = MailDispatcher(mail_sender) if proxy and mail_sender is not None else None
    if dispatcher is not None:
        mail_sender = dispatcher

    class Handler(BaseHTTPRequestHandler):
        server_version = "EKSMU-Local/0.2"
        sys_version = ""

        def setup(self):
            super().setup()
            # A browser may open a socket through an SSH tunnel before it
            # sends a deferred asset request. Five seconds closed those idle
            # sockets without an HTTP response, leaving scripts half loaded.
            self.connection.settimeout(30)

        def log_message(self, *_):
            pass  # Do not log identity keys, request bodies or private research.

        def guard(self):
            allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            host = self.headers.get("Host", "")
            if host in allowed:
                expected_origin = f"http://{host}"
            elif proxy and host == proxy[1]:
                expected_origin = proxy[0]
            else:
                raise Problem("Local Host header required", 403)
            origin = self.headers.get("Origin")
            if origin is not None and origin != expected_origin:
                raise Problem("Cross-origin access is disabled", 403)
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                # A clicked link may be cross-site even through an SSH tunnel.
                # Permit only user-activated top-level navigation to public pages
                # or embedded read-only credit badge/card SVGs.
                req_path = urlsplit(self.path).path
                home_navigation = (
                    self.command in {"GET", "HEAD"}
                    and (is_page_path(req_path) or req_path in PUBLIC_DISCOVERY_PATHS or bool(CARD_SVG_PATH.fullmatch(req_path)) or bool(WORK_PDF_PATH.fullmatch(req_path)) or bool(WORK_HTML_PATH.fullmatch(req_path)))
                    and self.headers.get("Sec-Fetch-Mode") == "navigate"
                    and self.headers.get("Sec-Fetch-Dest") == "document"
                    and self.headers.get("Sec-Fetch-User") == "?1"
                )
                public_svg_image = (
                    self.command == "GET"
                    and bool(CARD_SVG_PATH.fullmatch(req_path) or BADGE_SVG_PATH.fullmatch(req_path))
                    and self.headers.get("Sec-Fetch-Dest") == "image"
                )
                if not (home_navigation or public_svg_image):
                    raise Problem("Cross-site access is disabled", 403)

        def send(self, status: int, value, content_type: str = "application/json; charset=utf-8", download: bool = False, filename: str | None = None):
            data = value if isinstance(value, bytes) else canonical(value).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            if content_type.startswith("text/html"):
                self.send_header("Vary", "Accept-Language, Cookie")
            if getattr(self, 'response_cookie', None):
                self.send_header('Set-Cookie', self.response_cookie)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
            if filename is not None:
                self.send_header("Content-Disposition", "attachment; filename=\"eksmu-attachment\"; filename*=UTF-8''" + quote(filename, safe=''))
            elif download:
                self.send_header("Content-Disposition", 'attachment; filename="eksmu-bundle.json"')
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def send_stream(self, status: int, total_size: int, chunks, content_type: str, filename: str | None = None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(total_size))
            self.send_header("Cache-Control", "no-store")
            if getattr(self, 'response_cookie', None):
                self.send_header('Set-Cookie', self.response_cookie)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
            if filename is not None:
                self.send_header("Content-Disposition", "attachment; filename=\"eksmu-attachment\"; filename*=UTF-8''" + quote(filename, safe=''))
            self.end_headers()
            if self.command != "HEAD":
                for chunk in chunks:
                    if chunk:
                        self.wfile.write(chunk)

        def work_pdf(self, exploration_id: str, attachment_id: str, *, head_only: bool = False):
            from . import work_page
            if urlsplit(self.path).query:
                raise Problem('附件不存在', 404)
            store = Store(database)
            try:
                rows = store.rows()
            finally:
                store.close()
            try:
                total_size, chunks, filename, distribute = work_page.load_pdf(str(database), rows, exploration_id, attachment_id)
            except Problem as exc:
                if exc.status in {400, 404}:
                    raise Problem('附件不存在', 404) from None
                raise
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Length", str(total_size))
                self.send_header("Content-Disposition", "inline; filename*=UTF-8''" + quote(filename, safe=''))
                self.send_header("Cache-Control", "public, max-age=3600")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                # No sandbox/default-src here: browsers' built-in PDF viewers are blocked in sandboxed documents.
                # Bytes are checked to start with %PDF- and nosniff keeps them from being read as HTML.
                self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
                if not distribute:
                    self.send_header("X-Robots-Tag", "noindex")
                self.end_headers()
                if not head_only:
                    for chunk in chunks:
                        if chunk:
                            self.wfile.write(chunk)
            finally:
                close_fn = getattr(chunks, 'close', None)
                if callable(close_fn):
                    close_fn()

        def work_html(self, exploration_id: str, attachment_id: str, *, head_only: bool = False):
            from . import work_page
            if urlsplit(self.path).query:
                raise Problem('附件不存在', 404)
            store = Store(database)
            try:
                rows = store.rows()
            finally:
                store.close()
            try:
                total_size, chunks, filename, distribute = work_page.load_html(str(database), rows, exploration_id, attachment_id)
            except Problem as exc:
                if exc.status in {400, 404}:
                    raise Problem('附件不存在', 404) from None
                raise
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(total_size))
                self.send_header("Content-Disposition", "inline; filename*=UTF-8''" + quote(filename, safe=''))
                self.send_header("Cache-Control", "public, max-age=3600")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header(
                    "Content-Security-Policy",
                    "sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
                )
                if not distribute:
                    self.send_header("X-Robots-Tag", "noindex")
                self.end_headers()
                if not head_only:
                    for chunk in chunks:
                        if chunk:
                            self.wfile.write(chunk)
            finally:
                close_fn = getattr(chunks, 'close', None)
                if callable(close_fn):
                    close_fn()

        def redirect(self, location: str, cookie: str | None = None):
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Vary", "Accept-Language, Cookie")
            self.send_header("Referrer-Policy", "no-referrer")
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()

        def chain(self, store, path: str):
            """Public, read-only record chain: digests and anchor proofs, never private payloads."""
            from . import record_chain
            params = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
            def one(name, allowed):
                if set(params) != allowed or len(params[name]) != 1:
                    raise Problem('参数不正确')
                return params[name][0]
            if path == '/api/chain':
                if params:
                    raise Problem('参数不正确')
                self.send(200, record_chain.status(store.db))
            elif path == '/api/chain/entries':
                start = params.get('from', ['1'])[0]; limit = params.get('limit', ['500'])[0]
                if set(params) - {'from', 'limit'} or not start.isdecimal() or not limit.isdecimal():
                    raise Problem('分页参数不正确')
                self.send(200, record_chain.entries(store.db, int(start), int(limit)))
            elif path == '/api/chain/proof':
                record_id = one('record', {'record'}); identifier(record_id)
                self.send(200, record_chain.record_proof(store.db, record_id),
                          filename=f'eksmu-record-proof-{record_id}.json')
            elif path in {'/api/chain/anchor-proof', '/api/chain/anchor-file'}:
                value = one('id', {'id'})
                row = store.db.execute('SELECT seq, head, proof FROM chain_anchors WHERE id=?',
                                       (int(value),) if value.isdecimal() else (-1,)).fetchone()
                if row is None:
                    raise Problem('找不到对应的时间锚定', 404)
                if path.endswith('-proof'):
                    self.send(200, bytes(row[2]), 'application/octet-stream', filename=f'eksmu-chain-{row[0]}.bin.ots')
                else:
                    self.send(200, bytes.fromhex(row[1]), 'application/octet-stream', filename=f'eksmu-chain-{row[0]}.bin')
            else:
                raise Problem('Not found', 404)

        def personal_analysis(self, store, path):
            """Analysis notices and report counts for community endpoints, if analysis has been used."""
            if path not in {'/api/community/feed', '/api/community/notifications', '/api/community/read', '/api/community/import'}:
                return None
            if not Path(database + '.analysis.sqlite').is_file():
                return None
            from .analysis import Analysis
            return Analysis(database + '.analysis.sqlite', store.rows())

        def work_page(self, lang: str, exploration_id: str):
            from . import work_page
            params = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
            revision = params.get('revision')
            if set(params) - {'revision'} or (revision is not None and len(revision) != 1):
                self.send(404, work_page.not_found(lang), 'text/html; charset=utf-8')
                return
            store = Store(database)
            try:
                viewer_id = None
                try:
                    value = self.headers.get("Authorization", "")
                    cookie = self.cookie_token()
                    raw_token = cookie if (not value and cookie) else (value[7:] if value.startswith("Bearer ") and not cookie else "")
                    if raw_token:
                        viewer_id = store.authenticate(raw_token)
                except Exception:
                    viewer_id = None
                data = work_page.load(str(database), store.rows(), ModernAccounts(store, mail_sender).public_profiles(),
                                      exploration_id, revision[0] if revision else None, chain_db=store.db,
                                      viewer_id=viewer_id)
            except Problem as exc:
                if exc.status not in {400, 404}:
                    raise
                self.send(404, work_page.not_found(lang), 'text/html; charset=utf-8')
                return
            finally:
                store.close()
            origin = proxy[0] if proxy else f"http://{self.headers.get('Host')}"
            with i18n.use(lang):
                self.send(200, work_page.page(data, lang, origin), 'text/html; charset=utf-8')

        def author_page(self, lang: str, actor_id: str):
            from . import work_page
            if urlsplit(self.path).query:
                self.send(404, work_page.not_found(lang), 'text/html; charset=utf-8')
                return
            store = Store(database)
            try:
                data = work_page.load_author(str(database), store.rows(),
                                             ModernAccounts(store, mail_sender).public_profiles(), actor_id)
            except Problem as exc:
                if exc.status not in {400, 404}:
                    raise
                self.send(404, work_page.not_found(lang), 'text/html; charset=utf-8')
                return
            finally:
                store.close()
            origin = proxy[0] if proxy else f"http://{self.headers.get('Host')}"
            with i18n.use(lang):
                self.send(200, work_page.author_page(data, lang, origin), 'text/html; charset=utf-8')

        def language(self) -> str:
            return i18n.negotiate(self.headers.get("Accept-Language"), self.headers.get("Cookie"))

        def token(self):
            value = self.headers.get("Authorization", "")
            cookie = self.cookie_token()
            if value and cookie:
                raise Problem('不能同时使用两种会话凭证', 400)
            if not value and cookie:
                token = cookie
            elif value.startswith("Bearer "):
                token = value[7:]
            else:
                raise Problem("An identity key is required to contribute", 401)
            if proxy:
                if not token.startswith('ois1_'):
                    raise Problem('请使用已验证邮箱登录', 401)
                from .accounts import active_session
                actor = active_session(self.request_store.db, token)[1]
                verified = self.request_store.db.execute(
                    'SELECT verified FROM auth_passwords WHERE actor=?', (actor,)).fetchone()
                if not verified or not verified[0]:
                    raise Problem('请使用已验证邮箱登录', 401)
            return token

        def session_for_logout(self):
            try:
                return self.token()
            except Problem as exc:
                if proxy and exc.status == 401:
                    return ''
                raise

        def client_source(self):
            if not proxy:
                return self.client_address[0]
            # The listener is loopback-only. The ingress must remove every
            # client-supplied copy and set exactly one canonical IP header.
            values = self.headers.get_all('X-Eksmu-Client-IP', [])
            if (self.headers.get('Host') != proxy[1] or len(values) != 1
                    or not ipaddress.ip_address(self.client_address[0]).is_loopback):
                raise Problem('受信代理客户端来源缺失', 403)
            try:
                return str(ipaddress.ip_address(values[0]))
            except ValueError:
                raise Problem('受信代理客户端来源无效', 403) from None

        def cookie_token(self):
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get('Cookie', ''))
            except CookieError:
                raise Problem('Invalid session cookie') from None
            found = cookie.get('eksmu_session_' + str(self.server.server_port))
            return found.value if found else ''

        def set_session(self, value, expires):
            self.response_cookie = (f'eksmu_session_{self.server.server_port}={value}; '
                                    f'Path=/api; HttpOnly; SameSite=Strict; Max-Age={max(0, expires-int(time.time()))}'
                                    + ('; Secure' if proxy else ''))

        def handle_request(self, writing: bool):
            lang = self.headers.get('X-Eksmu-Lang')
            with i18n.use(lang if lang in i18n.LANGS else 'zh'):
                self._handle_request(writing)

        def _handle_request(self, writing: bool):
            store = None
            bridge = None
            hub = None
            research = None
            publications = None
            write_lock = None
            large_request_acquired = False
            self.response_cookie = None
            self.request_store = None
            try:
                self.guard()
                path = urlsplit(self.path).path
                page = i18n.split_page_path(path)
                if not writing and (path == '/' or page and page[1] == '') and 'snapshot' in parse_qs(urlsplit(self.path).query, keep_blank_values=True):
                    from .research_http import query
                    from .store import identifier
                    identifier(query(self.path, {'snapshot'})['snapshot'])
                    self.send(200, i18n.render('publication.html', page[0] if page else self.language(), None), 'text/html; charset=utf-8')
                    return
                if not writing and page:
                    lang, name = page
                    params = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                    if params.get('lang') == ['switch']:
                        rest = urlencode([(k, v) for k, values in params.items() if k != 'lang' for v in values])
                        self.redirect(f'/{lang}/{name}' + (f'?{rest}' if rest else ''),
                                      f'{i18n.COOKIE}={lang}; Path=/; Max-Age=31536000; SameSite=Lax' + ('; Secure' if proxy else ''))
                        return
                    if name.startswith('w/'):
                        self.work_page(lang, name[2:])
                        return
                    if name.startswith('u/'):
                        self.author_page(lang, name[2:])
                        return
                    self.send(200, i18n.render(i18n.PAGES[name], lang, name), 'text/html; charset=utf-8')
                    return
                if not writing and i18n.WORK_PATH.fullmatch(path[1:]):
                    self.work_page(self.language(), path[3:])
                    return
                if not writing and i18n.AUTHOR_PATH.fullmatch(path[1:]):
                    self.author_page(self.language(), path[3:])
                    return
                if not writing and re.fullmatch(r'/(?:(zh|en)/)?u(?:/.*)?', path):
                    m = re.match(r'^/(?:(zh|en)/)?u/?(.*)$', path)
                    self.author_page(m.group(1) or self.language(), m.group(2))
                    return
                if not writing and path[1:] in i18n.PAGES:
                    self.send(200, i18n.render(i18n.PAGES[path[1:]], self.language(), path[1:]), 'text/html; charset=utf-8')
                    return
                if not writing and path.startswith('/i18n/') and path[6:-3] in i18n.LANGS and path.endswith('.js'):
                    self.send(200, i18n.script(path[6:-3]), 'text/javascript; charset=utf-8')
                    return
                if not writing and path in {'/publications.js', '/publication-view.js', '/analysis.js', '/work-page.js'}:
                    self.send(200, (STATIC / path[1:]).read_bytes(), 'text/javascript; charset=utf-8')
                    return
                if not writing and path == '/papers.js':
                    self.send(200, (STATIC / 'papers.js').read_bytes(), 'text/javascript; charset=utf-8')
                    return
                if not writing and path.startswith('/fonts/') and path.endswith('.woff2'):
                    font_file = path[7:]
                    if font_file in FONTS:
                        self.send(200, (STATIC / 'fonts' / font_file).read_bytes(), 'font/woff2')
                        return
                if not writing and path in {"/app.js", "/accounts.js", "/bridge.js", "/community.js", "/research.js", "/shell.js", "/style.css", "/research.css"}:
                    file, mime = {"/app.js": ("app.js", "text/javascript"), '/accounts.js': ('accounts.js', 'text/javascript'), "/bridge.js": ("bridge.js", "text/javascript"), "/community.js": ("community.js", "text/javascript"), "/research.js": ("research.js", "text/javascript"), "/shell.js": ("shell.js", "text/javascript"), "/research.css": ("research.css", "text/css"), "/style.css": ("style.css", "text/css")}[path]
                    self.send(200, (STATIC / file).read_bytes(), mime + "; charset=utf-8")
                    return
                if not writing and path == '/favicon.svg':
                    self.send(200, (STATIC / 'favicon.svg').read_bytes(), 'image/svg+xml; charset=utf-8')
                    return
                if not writing and path == '/robots.txt':
                    from . import work_page
                    origin = proxy[0] if proxy else f"http://{self.headers.get('Host')}"
                    self.send(200, work_page.robots_txt(origin), 'text/plain; charset=utf-8')
                    return
                if not writing and path in {'/sitemap.xml', '/feed.xml'}:
                    from . import work_page
                    origin = proxy[0] if proxy else f"http://{self.headers.get('Host')}"
                    store = Store(database)
                    try:
                        rows = store.rows()
                        if path == '/sitemap.xml':
                            self.send(200, work_page.sitemap_xml(str(database), rows, origin), 'application/xml; charset=utf-8')
                        else:
                            profiles = ModernAccounts(store, mail_sender).public_profiles()
                            self.send(200, work_page.atom_feed(str(database), rows, profiles, origin), 'application/atom+xml; charset=utf-8')
                    finally:
                        store.close()
                    return
                pdf_match = WORK_PDF_PATH.fullmatch(path) if not writing else None
                if pdf_match:
                    self.work_pdf(pdf_match.group(2), pdf_match.group(3), head_only=(self.command == "HEAD"))
                    return
                html_match = WORK_HTML_PATH.fullmatch(path) if not writing else None
                if html_match:
                    self.work_html(html_match.group(2), html_match.group(3), head_only=(self.command == "HEAD"))
                    return
                card_match = CARD_SVG_PATH.fullmatch(path) if not writing else None
                badge_match = BADGE_SVG_PATH.fullmatch(path) if not writing else None
                if card_match or badge_match:
                    from . import work_page
                    if urlsplit(self.path).query:
                        raise Problem('参数不正确')
                    svg_lang = (card_match.group(1) or self.language()) if card_match else self.language()
                    wid = card_match.group(2) if card_match else badge_match.group(1)
                    origin = proxy[0] if proxy else f"http://{self.headers.get('Host')}"
                    store = Store(database)
                    try:
                        data = work_page.load(str(database), store.rows(), ModernAccounts(store, mail_sender).public_profiles(),
                                              wid, None, chain_db=store.db)
                    finally:
                        store.close()
                    with i18n.use(svg_lang):
                        payload_svg = work_page.card_svg(data, svg_lang, origin) if card_match else work_page.badge_svg(data, svg_lang)
                    self.send(200, payload_svg, 'image/svg+xml; charset=utf-8')
                    return
                if not writing and path == "/api/health":
                    self.send(200, {"version": "0.2.0", "mode": "public-alpha" if proxy else "local-private-prototype",
                        "automatic_verification": False, 'public_registration': bool(proxy),
                        'email_recovery_available': mail_sender is not None})
                    return
                if not writing and path == '/api/source':
                    if urlsplit(self.path).query:
                        raise Problem('源码接口不接受URL参数')
                    from .source_attestation import attestation
                    self.send(200, attestation())
                    return
                if path == "/api/verification/health" and not writing:
                    self.send(200, {"enabled": bridge_factory is not None,
                        "public_key": signer.public_key().public_bytes_raw().hex() if bridge_factory else None,
                        "checker": "rational-equality/v1", "source_semantics_verified": False,
                        "publication_authority": False})
                    return
                store = Store(database)
                self.request_store = store
                if not writing and path.startswith('/api/chain'):
                    self.chain(store, path)
                    return
                if not writing and path == '/api/analysis':
                    from .analysis import Analysis
                    from .research_http import query
                    work = Analysis(database + '.analysis.sqlite', store.rows())
                    try:
                        self.send(200, work.view(**query(self.path, {'revision_id'})))
                    finally:
                        work.close()
                    return
                if not writing and path == '/api/mirror':
                    if urlsplit(self.path).query:
                        raise Problem('镜像接口不接受URL参数')
                    from .mirror import manifest
                    self.send(200, manifest(store, database, proxy[0] if proxy else None))
                    return
                if not writing and path == '/api/profiles':
                    if urlsplit(self.path).query:
                        raise Problem('公开资料接口不接受URL参数')
                    self.send(200, {'profiles': ModernAccounts(store, mail_sender).public_profiles()})
                    return
                if not writing and path.startswith('/api/auth/'):
                    if urlsplit(self.path).query:
                        raise Problem('账户接口不接受URL参数')
                    account = Accounts(store)
                    if path == '/api/auth/status':
                        supplied = self.headers.get('Authorization') or self.cookie_token()
                        try:
                            token = self.token() if supplied else ''
                        except Problem as exc:
                            if not proxy or exc.status != 401:
                                raise
                            token = ''
                        result = account.status(token)
                        result['email_recovery_available'] = mail_sender is not None
                        result['public_registration'] = bool(proxy)
                        result['require_verified_email'] = bool(proxy)
                        if self.cookie_token() and result['actor'] is None:
                            self.set_session('', 0)
                    elif path == '/api/auth/sessions':
                        result = account.sessions(self.token())
                    elif path == '/api/auth/profile':
                        result = ModernAccounts(store, mail_sender).profile(self.token())
                    else:
                        raise Problem('Not found', 404)
                    self.send(200, result)
                    return
                if path.startswith("/api/verification/"):
                    if bridge_factory is None:
                        raise Problem("Local verification is disabled or unavailable; publication remains available", 503)
                    bridge = bridge_factory(database + ".receipt-tasks.sqlite", signer)
                if path.startswith("/api/community/"):
                    from .community import Community
                    hub = Community(database + ".community.sqlite", store.rows(),
                                    ModernAccounts(store, mail_sender).public_profiles(),
                                    chain_db=store.db)
                    if path in {'/api/community/notifications', '/api/community/read', '/api/community/import',
                                '/api/community/behavior-report'}:
                        from .research import Research
                        research = Research(database + '.research.sqlite', store.rows())
                if path.startswith("/api/research/") and path != "/api/research/preview":
                    from .research import Research
                    research = Research(database + ".research.sqlite", store.rows())
                if not writing:
                    if path.startswith('/api/publications/'):
                        from .publications import Publications
                        from .publication_http import get
                        from .research_http import query
                        publications = Publications(database + '.publications.sqlite', store.rows())
                        if path == '/api/publications/file':
                            total_size, chunks, mime, name = publications.file_stream(**query(self.path, {'snapshot_id', 'attachment_id'}))
                            self.send_stream(200, total_size, chunks, mime, filename=name)
                        else:
                            self.send(200, get(publications, self.path), filename='eksmu-publication.json' if path.endswith('/export') else None)
                        return
                    if path.startswith('/api/research/') and research is not None:
                        from .research_http import get, query
                        if path in {"/api/research/file", "/api/research/preview-file"}:
                            aid = query(self.path, {"attachment_id"})["attachment_id"]
                            preview = path.endswith("/preview-file")
                            total_size, chunks, mime, filename = research.download_stream(aid, preview=preview)
                            self.send_stream(200, total_size, chunks, mime, filename=None if preview else filename)
                        else:
                            if path in {'/api/research/lean-enrollments', '/api/research/compile-log'}:
                                actor = store.authenticate(self.token())
                            elif path == '/api/research/workspace' and (self.headers.get('Authorization') or self.cookie_token()):
                                try:
                                    actor = store.authenticate(self.token())
                                except Problem:
                                    actor = None
                            else:
                                actor = None
                            self.send(200, get(research, self.path, actor))
                        return
                    if hub is not None:
                        from .community_http import get
                        public = path in {"/api/community/feed", "/api/community/topics", "/api/community/details"}
                        actor = (store.authenticate(self.token()) if not public or self.headers.get("Authorization") or self.cookie_token() else None)
                        work = self.personal_analysis(store, path)
                        try:
                            self.send(200, get(hub, self.path, actor, research, work))
                        finally:
                            if work is not None: work.close()
                        return
                    if path in {"/api/verification/handoffs", "/api/verification/capsule"}:
                        from eksmu_bridge.handoff import Handoffs
                        actor = store.authenticate(self.token())
                        query = parse_qs(urlsplit(self.path).query)
                        field = "task_id" if path.endswith("/handoffs") else "dispatch_id"
                        if set(query) != {field} or len(query[field]) != 1:
                            raise Problem("One handoff identifier is required")
                        handoffs = Handoffs(bridge)
                        result = ({"handoffs": handoffs.list(query[field][0], actor)} if field == "task_id"
                                  else handoffs.view(query[field][0], actor)["dispatch"]["capsule"])
                    elif path == "/api/verification/tasks":
                        query = parse_qs(urlsplit(self.path).query)
                        if set(query) != {"revision_id"} or len(query["revision_id"]) != 1:
                            raise Problem("One revision_id is required")
                        result = {"tasks": bridge.list(query["revision_id"][0])}
                    elif path == "/api/verification/export":
                        query = parse_qs(urlsplit(self.path).query)
                        if set(query) != {"task_id"} or len(query["task_id"]) != 1:
                            raise Problem("One task_id is required")
                        result = bridge.export(query["task_id"][0])
                    elif path == "/api/state":
                        result = {"records": store.rows(), "automatic_verification": False}
                    elif path == "/api/export":
                        result = store.export()
                    elif path == "/api/me":
                        aid = store.authenticate(self.token())
                        result = next(r for r in store.rows() if r["id"] == aid)
                    else:
                        raise Problem("Not found", 404)
                    self.send(200, result, download=path == "/api/export")
                    return
                if self.headers.get("X-Eksmu-Client") != "local-prototype":
                    raise Problem("Local client header required", 403)
                if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                    raise Problem("application/json required", 415)
                if self.headers.get("Transfer-Encoding") is not None:
                    raise Problem("Transfer encoding is unsupported", 400)
                length = self.headers.get("Content-Length", "")
                if not length.isdecimal():
                    raise Problem("A valid Content-Length is required", 411)
                size = int(length)
                request_limit = {"/api/community/import": 2 * 1024 * 1024,
                                 "/api/research/upload": MAX_UPLOAD_REQUEST,
                                 "/api/research/inspect-source": MAX_UPLOAD_REQUEST,
                                 "/api/research/import-materials": MAX_IMPORT_MATERIALS_REQUEST}.get(path, MAX_REQUEST)
                if not 0 < size <= request_limit:
                    self.close_connection = True
                    raise Problem("Request size limit exceeded", 413)
                if path in {"/api/research/upload", "/api/research/inspect-source", "/api/research/import-materials"} or size > MAX_REQUEST:
                    large_request_acquired = LARGE_REQUEST_SEMAPHORE.acquire(blocking=False)
                    if not large_request_acquired:
                        self.close_connection = True
                        raise Problem("当前大文件请求较多，请稍后重试；文字发表不受影响", 503)
                raw = self.rfile.read(size)
                if len(raw) != size:
                    raise Problem("Incomplete request body")
                payload = load_json(raw)
                action = path.rsplit('/', 1)[-1]
                if path.startswith('/api/auth/') and path not in AUTH_POST_ROUTES:
                    raise Problem('Not found', 404)
                if proxy and path in {'/api/actors', '/api/auth/register', '/api/auth/login', '/api/auth/rotate'}:
                    raise Problem('本站使用注册前邮箱确认，请使用邮箱账户入口', 403)
                if path.startswith('/api/auth/') and action in LIMITS:
                    identity = (payload.get('email') if 'email' in payload
                                else payload.get('credential') if 'credential' in payload
                                else payload.get('recovery') if 'recovery' in payload
                                else self.headers.get('Authorization') or self.cookie_token())
                    auth_limiter.check(action, self.client_source(), identity)
                write_lock = RequestLock(database)
                expected_actor = self.headers.get('X-Eksmu-Actor')
                if expected_actor and path not in {'/api/auth/register', '/api/auth/login', '/api/auth/rotate',
                        '/api/auth/email-register', '/api/auth/confirm-registration', '/api/auth/password-login', '/api/auth/request-reset',
                        '/api/auth/reset-password'}:
                    from .store import identifier
                    identifier(expected_actor)
                    if path == '/api/auth/logout':
                        supplied = self.headers.get('Authorization') or self.cookie_token()
                        actual = Accounts(store).status(self.session_for_logout())['actor'] if supplied else None
                        actual_id = actual['id'] if actual else None
                    else:
                        actual_id = store.authenticate(self.token())
                    if actual_id is not None and expected_actor != actual_id:
                        raise Problem('此浏览器的登录身份已改变，请刷新后重新确认操作', 409)
                if path.startswith('/api/auth/'):
                    if urlsplit(self.path).query:
                        raise Problem('账户接口不接受URL参数')
                    from .store import exact
                    account = Accounts(store)
                    action = path.rsplit('/', 1)[-1]
                    if action in {'register', 'login', 'rotate'}:
                        result = account.issue(action, payload)
                        self.set_session(payload['session_token'], result['expires_at'])
                    elif action in {'email-register', 'confirm-registration', 'password-login', 'attach-email', 'profile', 'change-password',
                                    'request-verify', 'verify-email', 'request-reset', 'reset-password'}:
                        modern = ModernAccounts(store, mail_sender, require_verified_email=bool(proxy))
                        required = {
                            'email-register': {'alias', 'email', 'password', 'label'},
                            'confirm-registration': {'email', 'code', 'alias', 'password', 'label'},
                            'password-login': {'email', 'password', 'label'},
                            'attach-email': {'credential', 'email', 'password'},
                            'profile': {'display_name', 'affiliation', 'expected_version'},
                            'change-password': {'current_password', 'new_password'},
                            'request-verify': set(), 'verify-email': {'code'},
                            'request-reset': {'email'},
                            'reset-password': {'email', 'code', 'new_password'},
                        }
                        if action == 'email-register' and proxy:
                            if set(payload) not in ({'email'}, {'email', 'label'}, required[action]):
                                raise Problem('注册请求字段不正确')
                        else:
                            exact(payload, required[action])
                        if action == 'email-register':
                            if proxy:
                                result, token = modern.request_registration(payload['email']), None
                            else:
                                result, token = modern.register(**payload)
                            if token is not None:
                                self.set_session(token, result['expires_at'])
                        elif action == 'confirm-registration':
                            result, token = modern.confirm_registration(**payload)
                            self.set_session(token, result['expires_at'])
                        elif action == 'password-login':
                            result, token = modern.login(**payload)
                            self.set_session(token, result['expires_at'])
                        elif action == 'attach-email':
                            result = modern.attach(self.token(), **payload)
                        elif action == 'profile':
                            result = modern.update_profile(self.token(), **payload)
                        elif action == 'change-password':
                            result = modern.change_password(self.token(), **payload)
                        elif action == 'request-verify':
                            result = modern.request_code('verify', token=self.token())
                        elif action == 'verify-email':
                            result = modern.verify_email(self.token(), payload['code'])
                        elif action == 'request-reset':
                            result = modern.request_code('reset', email=payload['email'])
                        else:
                            result = modern.reset_password(**payload)
                    elif action == 'logout':
                        exact(payload, set())
                        token = self.session_for_logout() if self.headers.get('Authorization') or self.cookie_token() else ''
                        result = account.logout(token) if token else {'logged_out': True}
                        self.set_session('', 0)
                    elif action == 'revoke':
                        exact(payload, {'session_id'})
                        result = account.revoke(self.token(), session_id=payload['session_id'])
                    elif action == 'revoke-others':
                        exact(payload, set())
                        result = account.revoke(self.token(), others=True)
                    else:
                        raise Problem('Not found', 404)
                    self.send(200, result)
                    return
                if path == "/api/research/preview":
                    from .research_render import render, render_abstract
                    from .store import exact
                    allowed = {"body", "abstract"} if isinstance(payload, dict) and "abstract" in payload else {"body"}
                    exact(payload, allowed)
                    res = render(payload["body"])
                    if payload.get("abstract"):
                        res["rendered_abstract"] = render_abstract(payload["abstract"])
                    self.send(200, res)
                    return
                if path in {'/api/analysis/request', '/api/analysis/respond'}:
                    from .analysis import Analysis
                    from .store import exact
                    actor = store.authenticate(self.token())
                    work = Analysis(database + '.analysis.sqlite', store.rows())
                    try:
                        if path.endswith('/request'):
                            if set(payload) not in ({'revision_id'}, {'revision_id', 'model', 'api_key'}):
                                raise Problem('Missing or unexpected fields')
                            result = work.request(actor, payload['revision_id'], payload.get('model'), payload.get('api_key'))
                        else:
                            exact(payload, {'report_id', 'finding_id', 'stance', 'body', 'revision_ref'})
                            result = work.respond(actor, **payload)
                    finally:
                        work.close()
                    self.send(200, result)
                    return
                if path.startswith('/api/publications/'):
                    from .publications import Publications
                    from .publication_http import post
                    from .research import Research
                    actor = store.authenticate(self.token())
                    publications = Publications(database + '.publications.sqlite', store.rows())
                    if path == '/api/publications/snapshot':
                        research = Research(database + '.research.sqlite', store.rows())
                    self.send(200, post(publications, research, actor, path, payload))
                    return
                if path.startswith('/api/research/') and research is not None:
                    from .research_http import post
                    actor = store.authenticate(self.token())
                    self.send(200, post(research, path, actor, payload))
                    return
                if hub is not None:
                    from .community_http import post
                    actor = store.authenticate(self.token())
                    work = self.personal_analysis(store, path)
                    try:
                        self.send(200, post(hub, path, actor, payload, research, work))
                    finally:
                        if work is not None: work.close()
                    return
                if path.startswith("/api/verification/"):
                    actor = store.authenticate(self.token())
                    required = {
                        "/api/verification/prepare": {"revision_id", "scope", "problem"},
                        "/api/verification/dispatch": {"task_id", "operation_id", "expected_public_key", "consent", "ttl_seconds"},
                        "/api/verification/collect": {"dispatch_id", "submission"},
                        "/api/verification/execute": {"task_id", "operation_id", "consent"},
                        "/api/verification/cancel": {"task_id"},
                        "/api/verification/recheck": {"task_id", "expected_public_key"},
                    }
                    if path not in required:
                        raise Problem("Not found", 404)
                    if set(payload) != required[path]:
                        raise Problem("Missing or unexpected verification fields")
                    if path.endswith("/dispatch") or path.endswith("/collect"):
                        from eksmu_bridge.handoff import Handoffs
                        action = Handoffs(bridge).dispatch if path.endswith("/dispatch") else Handoffs(bridge).collect
                        result = action(actor_id=actor, **payload)
                    elif path.endswith("/prepare"):
                        revision = next((r for r in store.rows()
                            if r["id"] == payload["revision_id"] and r["type"] == "revision"), None)
                        if revision is None:
                            raise Problem("Unknown source revision", 404)
                        result = bridge.prepare(actor, revision, payload["scope"], payload["problem"])
                    elif path.endswith("/execute"):
                        result = bridge.execute(actor_id=actor, **payload)
                    elif path.endswith("/cancel"):
                        result = bridge.cancel(actor_id=actor, **payload)
                    else:
                        result = bridge.recheck(**payload)
                    self.send(200, result)
                    return
                routes = {
                    "/api/actors": (store.register, set(), {"alias"}),
                    "/api/explorations": (store.publish, {"body"}, {"title", "kind", "paper"}),
                    "/api/revisions": (store.revise, {"exploration_id", "parent_revision_id", "title", "body"}, {"note", "paper"}),
                    "/api/comments": (store.comment, {"revision_id", "body"}, {"reply_to"}),
                    "/api/evidence": (store.evidence, {"revision_id", "source", "scope", "method", "result"}, {"status", "tool", "limitations"}),
                }
                if path not in routes:
                    raise Problem("Not found", 404)
                if path == '/api/actors':
                    Accounts(store)._ready()
                action, required, optional = routes[path]
                if not required <= set(payload) or set(payload) - required - optional:
                    raise Problem("Missing or unexpected request fields")
                args = {} if path == "/api/actors" else {"token": self.token()}
                self.send(201, action(**args, **payload))
            except Problem as exc:
                req_path = urlsplit(self.path).path
                if exc.status == 404 and not writing and not req_path.startswith('/api/'):
                    # A mistyped page address gets the 404 page, not a JSON error; API clients keep JSON.
                    from . import work_page
                    lang = 'en' if req_path == '/en' or req_path.startswith('/en/') else 'zh'
                    self.send(404, work_page.not_found(lang), 'text/html; charset=utf-8')
                else:
                    self.send(exc.status, {"error": i18n.localize(str(exc))})
            except ValueError as exc:
                # Domain exceptions contain no keys or input bodies.
                self.send(getattr(exc, "status", 400), {"error": i18n.localize(str(exc))})
            except (TypeError, RecursionError):
                self.send(400, {"error": "Invalid input"})
            except TimeoutError:
                self.send(408, {"error": "Request timed out"})
            except (OSError, RuntimeError):
                self.send(503, {"error": "Local service unavailable; no verification success is confirmed"})
            except sqlite3.Error:
                self.send(503, {"error": "Storage unavailable; no successful write is confirmed"})
            finally:
                if publications is not None:
                    publications.close()
                if research is not None:
                    research.close()
                if hub is not None:
                    hub.close()
                if bridge is not None:
                    bridge.close()
                if store is not None:
                    store.close()
                if write_lock is not None:
                    write_lock.close()
                if large_request_acquired:
                    LARGE_REQUEST_SEMAPHORE.release()

        def do_GET(self):
            self.handle_request(False)

        def do_HEAD(self):
            self.handle_request(False)

        def do_POST(self):
            self.handle_request(True)

    try:
        server = _InstanceServer(("127.0.0.1", port), Handler)
    except BaseException:
        if dispatcher is not None:
            dispatcher.close()
        raise
    server._mail_dispatcher = dispatcher
    return server
