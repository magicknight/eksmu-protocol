import http.client
import json
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

from eksmu.server import MAX_REQUEST, make_server
from eksmu.store import Store


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = make_server(Path(self.temp.name) / "local.sqlite", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.temp.cleanup()

    def request(self, path, data=None, token=None, headers=None, raw=None):
        default = {"Content-Type":"application/json", "X-Eksmu-Client":"local-prototype"}
        if token: default["Authorization"] = "Bearer " + token
        if headers: default.update(headers)
        body = raw if raw is not None else (None if data is None else json.dumps(data).encode())
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            conn.request("POST" if body is not None else "GET", path, body, default)
            response = conn.getresponse(); payload = response.read()
            return response.status, dict(response.getheaders()), payload
        finally:
            conn.close()

    def test_loopback_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_idle_preconnected_browser_socket_can_fetch_script(self):
        port = self.server.server_port
        with socket.create_connection(("127.0.0.1", port), timeout=10) as client:
            client.settimeout(10)
            time.sleep(6)
            client.sendall(f"GET /app.js HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n".encode())
            response = bytearray()
            while chunk := client.recv(65536):
                response.extend(chunk)
            self.assertTrue(response.startswith(b"HTTP/1.0 200"))

    def test_new_participant_http_journey(self):
        code, _, body = self.request("/api/actors", {"alias":"新人"})
        self.assertEqual(code, 201); identity = json.loads(body)
        code, _, body = self.request("/api/explorations", {"body":"暂时还不会写证明。"}, identity["token"])
        self.assertEqual(code, 201)
        revision = json.loads(body)["revision"]["id"]
        code, _, _ = self.request("/api/comments", {"body":"可以先提问。", "revision_id":revision}, identity["token"])
        self.assertEqual(code, 201)
        code, _, body = self.request("/api/export")
        self.assertEqual(code, 200); self.assertNotIn(identity["token"].encode(), body)
        self.assertEqual(len(json.loads(body)["records"]), 4)

    def test_no_key_cannot_impersonate_author(self):
        self.assertEqual(self.request("/api/explorations", {"body":"text"})[0], 401)

    def test_paper_http_publish_inherit_and_invalid_payload_atomicity(self):
        _, _, body = self.request('/api/actors', {'alias': '论文演示'})
        token = json.loads(body)['token']
        metadata = {'format':'eksmu.paper-metadata.v1', 'abstract':'演示摘要', 'authors':[],
                    'references':[], 'license':'', 'provenance':{'source':'', 'prior_publication_date':''}}
        code, _, body = self.request('/api/explorations', {'body':'演示正文', 'paper':metadata}, token)
        self.assertEqual(code, 201); work = json.loads(body)
        args = {'exploration_id':work['exploration']['id'], 'parent_revision_id':work['revision']['id'],
                'title':'第二版', 'body':'修订正文'}
        code, _, body = self.request('/api/revisions', args, token)
        self.assertEqual(code, 201); self.assertEqual(json.loads(body)['payload']['paper'], metadata)
        original = self.request('/api/export')[2]
        self.assertEqual(json.loads(original)['format'], 'eksmu.publication-bundle.v1')
        self.assertEqual(self.request('/api/explorations', {'body':'不应部分写入', 'paper':{}}, token)[0], 400)
        self.assertEqual(self.request('/api/export')[2], original)
        self.assertEqual(self.request('/papers.js')[0], 200)

    def test_cross_origin_request_denied(self):
        self.assertEqual(self.request("/api/actors", {}, headers={"Origin":"https://untrusted.example"})[0], 403)

    def test_rebinding_host_denied(self):
        self.assertEqual(self.request("/api/state", headers={"Host":"untrusted.example"})[0], 403)

    def test_opt_in_proxy_origin_keeps_loopback_and_blocks_legacy_registration(self):
        for invalid in ('http://alpha.example.test', 'https://alpha.example.test/path',
                        'https://alpha.example.test:443', 'https://ALPHA.example.test',
                        'https://localhost'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                make_server(Path(self.temp.name) / 'invalid.sqlite', 0, public_origin=invalid)
        proxy = make_server(Path(self.temp.name) / 'proxy.sqlite', 0,
                            public_origin='https://alpha.example.test')
        thread = threading.Thread(target=proxy.serve_forever, daemon=True); thread.start()
        try:
            self.assertEqual(proxy.server_address[0], '127.0.0.1')
            def send(path, payload=None, headers=None):
                connection = http.client.HTTPConnection('127.0.0.1', proxy.server_port, timeout=5)
                request_headers={'Host': 'alpha.example.test', 'Origin': 'https://alpha.example.test',
                                 'Content-Type': 'application/json', 'X-Eksmu-Client': 'local-prototype'}
                request_headers.update(headers or {})
                try:
                    connection.request('POST' if payload is not None else 'GET', path,
                                       json.dumps(payload).encode() if payload is not None else None,
                                       request_headers)
                    response=connection.getresponse(); response.read()
                    return response.status, dict(response.getheaders())
                finally: connection.close()
            self.assertEqual(send('/api/health')[0], 200)
            self.assertEqual(send('/api/health', headers={'Origin':'http://alpha.example.test'})[0], 403)
            self.assertEqual(send('/api/health', headers={'Host':'elsewhere.example.test'})[0], 403)
            registration={'operation_id':str(uuid.uuid4()), 'session_token':'ois1_'+secrets.token_urlsafe(32),
                          'recovery':'oir1_'+secrets.token_urlsafe(32), 'alias':'代理模式测试', 'label':'测试设备'}
            status, response_headers=send('/api/auth/register', registration)
            self.assertEqual(status, 403)
            self.assertNotIn('Set-Cookie', response_headers)
        finally:
            proxy.shutdown(); proxy.server_close(); thread.join()

    def test_brand_icon_is_served_as_svg(self):
        code, headers, body = self.request("/favicon.svg", headers={"Sec-Fetch-Site":"same-origin"})
        self.assertEqual(code, 200)
        self.assertIn("image/svg+xml", headers["Content-Type"])
        self.assertIn(b"<svg", body)

    def test_cross_site_get_denied(self):
        self.assertEqual(self.request("/api/state", headers={"Sec-Fetch-Site":"cross-site"})[0], 403)

    def navigation_headers(self, **overrides):
        return {"Sec-Fetch-Site":"cross-site", "Sec-Fetch-Mode":"navigate",
                "Sec-Fetch-Dest":"document", "Sec-Fetch-User":"?1", **overrides}

    def test_cross_site_user_navigation_opens_home(self):
        for path in ("/", "/?view=explore"):
            with self.subTest(path=path):
                code, headers, body = self.request(path, headers=self.navigation_headers())
                self.assertEqual(code, 200)
                self.assertIn("text/html", headers["Content-Type"])
                self.assertIn(b"<!doctype html>", body)
        origin = f"http://127.0.0.1:{self.server.server_port}"
        self.assertEqual(self.request("/api/actors", {"alias":"Link visitor"},
            headers={"Origin":origin, "Sec-Fetch-Site":"same-origin"})[0], 201)

    def test_cross_site_navigation_does_not_expose_data_or_assets(self):
        for path in ("/api/state", "/api/export", "/api/health", "/app.js"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path, headers=self.navigation_headers())[0], 403)

    def test_cross_site_home_requires_user_top_level_navigation(self):
        for override in ({"Sec-Fetch-Dest":"iframe"}, {"Sec-Fetch-Dest":"image"},
                         {"Sec-Fetch-Mode":"cors"}, {"Sec-Fetch-User":""},
                         {"Sec-Fetch-User":"?0"}):
            with self.subTest(override=override):
                self.assertEqual(self.request("/", headers=self.navigation_headers(**override))[0], 403)

    def test_cross_site_navigation_does_not_allow_post(self):
        self.assertEqual(self.request("/api/actors", {}, headers=self.navigation_headers())[0], 403)

    def test_navigation_still_checks_host_and_origin(self):
        for override in ({"Host":"untrusted.example"}, {"Origin":"https://untrusted.example"}):
            with self.subTest(override=override):
                self.assertEqual(self.request("/", headers=self.navigation_headers(**override))[0], 403)

    def test_client_header_required_for_writes(self):
        self.assertEqual(self.request("/api/actors", {}, headers={"X-Eksmu-Client":""})[0], 403)

    def test_json_content_type_required(self):
        self.assertEqual(self.request("/api/actors", {}, headers={"Content-Type":"text/plain"})[0], 415)

    def test_oversized_request_denied(self):
        self.assertEqual(self.request("/api/actors", {}, headers={"Content-Length":str(MAX_REQUEST+1)})[0], 413)

    def test_duplicate_json_keys_rejected(self):
        self.assertEqual(self.request("/api/actors", raw=b'{"alias":"one","alias":"two"}')[0], 400)

    def test_invalid_json_and_nonfinite_numbers_rejected(self):
        for raw in [b'[', b'[]', b'{"alias":NaN}']:
            with self.subTest(raw=raw):
                self.assertEqual(self.request("/api/actors", raw=raw)[0], 400)

    def test_cannot_set_authority_fields(self):
        self.assertEqual(self.request("/api/actors", {"alias":"someone", "admin":True})[0], 400)

    def test_no_http_import_or_file_browser(self):
        self.assertEqual(self.request("/api/import", {})[0], 404)
        self.assertEqual(self.request("/../../VISION.md")[0], 404)
        self.assertEqual(self.request("/data/local.sqlite")[0], 404)

    def test_static_security_headers_and_no_external_assets(self):
        code, headers, body = self.request("/")
        self.assertEqual(code, 200)
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn(b"https://", body)
        self.assertNotIn(b"http://", body)

    def test_health_does_not_claim_verification(self):
        code, _, body = self.request("/api/health")
        self.assertEqual(code, 200)
        self.assertIs(json.loads(body)["automatic_verification"], False)

    def test_author_page_routes_and_privacy(self):
        code, _, body = self.request(
            "/api/auth/email-register",
            {"alias": "作者甲", "email": "author-a@example.test", "password": "correct-horse-battery", "label": "local"},
        )
        self.assertEqual(code, 200)
        actor_id = json.loads(body)["actor"]["id"]
        for path in (f"/u/{actor_id}", f"/zh/u/{actor_id}", f"/en/u/{actor_id}"):
            with self.subTest(path=path):
                code, headers, html = self.request(path, headers=self.navigation_headers())
                self.assertEqual(code, 200)
                self.assertIn("text/html", headers["Content-Type"])
                self.assertIn("作者甲".encode("utf-8"), html)
                self.assertNotIn(b"author-a@example.test", html)
        for bad in ("/u/invalid-id", "/zh/u/invalid-id", "/en/u/00000000-0000-4000-8000-000000000000"):
            with self.subTest(bad=bad):
                code, headers, _ = self.request(bad)
                self.assertEqual(code, 404)
                self.assertIn("text/html", headers["Content-Type"])


class CLITests(unittest.TestCase):
    def test_export_import_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); first = root / "one.sqlite"; second = root / "two.sqlite"; bundle = root / "bundle.json"
            store = Store(first)
            identity = store.register(); store.publish(identity["token"], "通过命令行迁移")
            expected = store.export(); store.close()
            def run(db, action, file):
                return subprocess.run([sys.executable,"-m","eksmu","--db",str(db),action,str(file)], capture_output=True, text=True)
            self.assertEqual(run(first,"export",bundle).returncode, 0)
            self.assertNotEqual(run(first,"export",bundle).returncode, 0)
            self.assertEqual(run(second,"import",bundle).returncode, 0)
            self.assertNotEqual(run(second,"import",bundle).returncode, 0)
            migrated = Store(second)
            try:
                self.assertEqual(migrated.export(), expected)
                self.assertEqual(migrated.authenticate(identity["token"]), identity["actor"]["id"])
            finally:
                migrated.close()


if __name__ == "__main__":
    unittest.main()
