"""Interface languages: dictionary coverage, negotiation and page routes."""
import http.client
import json
import re
import tempfile
import threading
import unittest
from pathlib import Path

from eksmu import i18n
from eksmu.server import make_server

STATIC = Path(__file__).resolve().parents[1] / 'eksmu/static'
LITERAL = re.compile(r'\btr\(\s*"((?:[^"\\]|\\.)*)"')
PLACEHOLDER = re.compile(r'\{(\w+)\}')


class DictionaryTests(unittest.TestCase):
    def test_every_page_and_script_message_has_english(self):
        english = i18n.strings('en')
        missing = [m for page in STATIC.glob('*.html') for m in i18n.html_messages(page.read_text(encoding='utf-8'))
                   if m not in english]
        for script in STATIC.glob('*.js'):
            missing += [k for k in (json.loads('"' + m.group(1) + '"') for m in LITERAL.finditer(script.read_text(encoding='utf-8')))
                        if k not in english]
        self.assertEqual(missing, [])

    def test_server_messages_have_english(self):
        english = i18n.strings('en')
        root = Path(__file__).resolve().parents[1] / 'eksmu'
        messages = []
        for source in root.glob('*.py'):
            text = source.read_text(encoding='utf-8')
            messages += [m.group(2) for m in re.finditer(r"""Problem\(\s*(['"])((?:(?!\1).)*)\1""", text)
                         if i18n.CJK.search(m.group(2))]
        from eksmu.modern_accounts import MAIL_SUBJECTS, mail_text
        from eksmu.research import MISSING
        messages += list(MAIL_SUBJECTS.values()) + list(MISSING.values())
        self.assertEqual([m for m in messages if m not in english], [])
        for purpose in MAIL_SUBJECTS:
            with self.subTest(purpose=purpose):
                subject, body = mail_text(purpose, '123456', 'en')
                self.assertIsNone(i18n.CJK.search(subject + body))
                if purpose != 'notice':
                    self.assertIn('123456', body)

    def test_placeholders_survive_translation(self):
        bad = [k for k, v in i18n.strings('en').items()
               if sorted(PLACEHOLDER.findall(k)) != sorted(PLACEHOLDER.findall(v))]
        self.assertEqual(bad, [])

    def test_english_pages_leave_no_chinese_interface_text(self):
        for page in ('index.html', 'founding.html', 'participation.html', 'publication.html', 'openness.html', 'author.html'):
            with self.subTest(page=page):
                markup = i18n.render(page, 'en', None).decode('utf-8')
                markup = markup.replace('>中文<', '><')
                self.assertEqual(i18n.html_messages(markup), [])
                self.assertIn('<html lang="en">', markup)

    def test_tag_context_overrides_plain_message(self):
        markup = i18n.render('index.html', 'en', '').decode('utf-8')
        self.assertIn('<a href="/en/" aria-current="page"', markup)
        self.assertIn('Discover', markup)
        self.assertIn('<option value="finding">Finding</option>', markup)

    def test_chinese_render_keeps_source_and_localizes_links(self):
        markup = i18n.render('founding.html', 'zh', 'founding.html').decode('utf-8')
        self.assertIn('把证明写下来', markup)
        self.assertIn('href="/zh/participation.html"', markup)
        self.assertIn('hreflang="en" href="/en/founding.html"', markup)
        self.assertIn('class="lang-switch" href="/en/founding.html?lang=switch"', markup)


class NegotiationTests(unittest.TestCase):
    def test_browser_preferences(self):
        cases = {
            None: 'en', '': 'en', 'fr-FR,fr;q=0.9': 'en',
            'zh-CN,zh;q=0.9,en;q=0.8': 'zh', 'zh-TW': 'zh',
            'en-US,en;q=0.9,zh-CN;q=0.8': 'en', 'fr;q=1,zh;q=0.5,en;q=0.4': 'zh',
            'zh;q=0,en;q=0.1': 'en', 'zh;q=bad,en': 'en',
        }
        for header, expected in cases.items():
            with self.subTest(header=header):
                self.assertEqual(i18n.negotiate(header, None), expected)

    def test_saved_choice_wins(self):
        self.assertEqual(i18n.negotiate('en-US', 'a=1; eksmu_lang=zh'), 'zh')
        self.assertEqual(i18n.negotiate('zh-CN', 'eksmu_lang=xx'), 'zh')


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = make_server(Path(self.temp.name) / 'local.sqlite', 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.temp.cleanup()

    def get(self, path, **headers):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            conn.request('GET', path, headers=headers)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def test_root_follows_browser_language(self):
        code, headers, body = self.get('/', **{'Accept-Language': 'zh-CN,zh;q=0.9'})
        self.assertEqual(code, 200)
        self.assertIn(b'<html lang="zh-CN">', body)
        self.assertIn('Accept-Language', headers['Vary'])
        code, _, body = self.get('/', **{'Accept-Language': 'en-US'})
        self.assertIn(b'<html lang="en">', body)
        self.assertIn(b'Where curiosity meets,', body)
        code, _, body = self.get('/founding.html', **{'Accept-Language': 'en', 'Cookie': 'eksmu_lang=zh'})
        self.assertIn(b'<html lang="zh-CN">', body)

    def test_language_paths_and_switch_cookie(self):
        for path, marker in (('/en/', b'lang="en"'), ('/zh/participation.html', b'lang="zh-CN"')):
            with self.subTest(path=path):
                code, _, body = self.get(path)
                self.assertEqual(code, 200)
                self.assertIn(marker, body)
        code, headers, _ = self.get('/zh/founding.html?lang=switch')
        self.assertEqual(code, 302)
        self.assertEqual(headers['Location'], '/zh/founding.html')
        self.assertIn('eksmu_lang=zh', headers['Set-Cookie'])
        self.assertIn('SameSite=Lax', headers['Set-Cookie'])
        self.assertNotIn('Secure', headers['Set-Cookie'])
        for path in ('/fr/', '/en/index.html', '/en/app.js', '/en'):
            with self.subTest(path=path):
                self.assertEqual(self.get(path)[0], 404)

    def test_split_views_share_the_home_template(self):
        for path, view, title in (('/en/', 'discover', 'Eksmu · Open Inquiry Community'), ('/en/publish', 'publish', 'Publish · Eksmu'),
                                  ('/zh/account', 'account', '账户 · Eksmu'), ('/activity', 'activity', 'My activity · Eksmu'),
                                  ('/en/workspace', 'workspace', 'Workspace · Eksmu')):
            with self.subTest(path=path):
                code, _, body = self.get(path, **{'Accept-Language': 'en'})
                self.assertEqual(code, 200)
                html = body.decode('utf-8')
                self.assertIn(f'<body data-view="{view}">', html)
                self.assertIn(f'<title>{title}</title>', html)
                self.assertIn('id="email-register"', html)  # every view keeps every form for the shared scripts
        nav = {'Sec-Fetch-Site': 'cross-site', 'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document', 'Sec-Fetch-User': '?1'}
        self.assertEqual(self.get('/publish', **nav)[0], 200)
        self.assertEqual(self.get('/en/publish.html')[0], 404)
        code, headers, _ = self.get('/en/workspace?lang=switch')
        self.assertEqual((code, headers['Location']), (302, '/en/workspace'))

    def test_browser_dictionary_script(self):
        code, headers, body = self.get('/i18n/en.js')
        self.assertEqual(code, 200)
        self.assertIn('javascript', headers['Content-Type'])
        self.assertIn('window.tr='.encode(), body)
        self.assertIn('"操作过于频繁，请稍后重试":"Too many requests. Please try again later."'.encode(), body)
        self.assertNotIn('把复核接起来'.encode(), body)  # page-only text stays on the server
        self.assertEqual(self.get('/i18n/zh.js')[0], 200)
        self.assertEqual(self.get('/i18n/fr.js')[0], 404)

    def test_cross_site_links_may_open_language_pages_only(self):
        nav = {'Sec-Fetch-Site': 'cross-site', 'Sec-Fetch-Mode': 'navigate',
               'Sec-Fetch-Dest': 'document', 'Sec-Fetch-User': '?1'}
        for path in ('/en/', '/zh/founding.html', '/participation.html'):
            with self.subTest(path=path):
                self.assertEqual(self.get(path, **nav)[0], 200)
        for path in ('/i18n/en.js', '/en/app.js'):
            with self.subTest(path=path):
                self.assertEqual(self.get(path, **nav)[0], 403)

    def test_api_errors_use_the_page_language_header(self):
        missing = '/api/research/workspace?revision_id=00000000-0000-4000-8000-000000000000'
        for headers, expected in (({'X-Eksmu-Lang': 'en'}, 'Version not found'), ({}, '找不到对应的版本'),
                                  ({'Accept-Language': 'en'}, '找不到对应的版本')):
            with self.subTest(headers=headers):
                code, _, body = self.get(missing, **headers)
                self.assertEqual((code, json.loads(body)['error']), (404, expected))

    def test_snapshot_view_uses_requested_language(self):
        code, _, body = self.get('/en/?snapshot=' + 'a' * 32)
        self.assertIn(code, {200, 400})
        if code == 200:
            self.assertIn(b'Publication snapshot', body)


if __name__ == '__main__':
    unittest.main()
