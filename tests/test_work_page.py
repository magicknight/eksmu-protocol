"""Standalone, server-rendered work pages."""
import http.client
import json
import re
import tempfile
import threading
import unittest
from pathlib import Path

from eksmu import i18n
from eksmu.server import make_server

NAV = {'Sec-Fetch-Site': 'cross-site', 'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document', 'Sec-Fetch-User': '?1'}


class WorkPageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = make_server(Path(self.temp.name) / 'local.sqlite', 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        _, _, body = self.call('/api/actors', {'alias': '问题'})
        created_actor = json.loads(body)
        self.token = created_actor['token']
        self.actor_id = created_actor['actor']['id']
        paper = {'format': 'eksmu.paper-metadata.v1', 'abstract': '一段 <b>摘要</b>', 'authors': [{'name': '甲 & 乙', 'contribution': '计算'}],
                 'references': ['Erdős 1950'], 'license': 'CC BY 4.0', 'provenance': {'source': '', 'prior_publication_date': ''}}
        code, _, body = self.call('/api/explorations', {'title': '问题', 'body': '正文 @@title@@ 与 $x^2$\n\n<script>alert(1)</script>', 'paper': paper}, self.token)
        self.assertEqual(code, 201)
        created = json.loads(body)
        self.work, self.first = created['exploration']['id'], created['revision']['id']
        code, _, body = self.call('/api/revisions', {'exploration_id': self.work, 'parent_revision_id': self.first,
                                                     'title': '第二版 <i>', 'body': '修订后的正文'}, self.token)
        self.assertEqual(code, 201)
        self.second = json.loads(body)['id']
        self.assertEqual(self.call('/api/comments', {'body': '回应 <img src=x>', 'revision_id': self.second}, self.token)[0], 201)
        _, _, peer_body = self.call('/api/actors', {'alias': 'Peer'})
        self.peer_token = json.loads(peer_body)['token']
        self.assertEqual(
            self.call('/api/evidence', {'revision_id': self.first, 'source': 'human', 'scope': 's', 'method': 'm', 'result': 'r'}, self.peer_token)[0],
            201,
        )

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.temp.cleanup()

    def call(self, path, data=None, token=None, headers=None):
        default = {'Content-Type': 'application/json', 'X-Eksmu-Client': 'local-prototype'}
        if token: default['Authorization'] = 'Bearer ' + token
        default.update(headers or {})
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        try:
            conn.request('POST' if data is not None else 'GET', path, None if data is None else json.dumps(data).encode(), default)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def page(self, path, **headers):
        code, response_headers, body = self.call(path, headers=headers)
        return code, response_headers, body.decode('utf-8')

    def test_latest_version_in_english_with_escaped_author_content(self):
        code, headers, html = self.page(f'/en/w/{self.work}')
        self.assertEqual(code, 200)
        self.assertIn('text/html', headers['Content-Type'])
        self.assertIn('<html lang="en">', html)
        self.assertIn('<h1 class="work-title">第二版 &lt;i&gt;</h1>', html)
        self.assertIn(f'<a class="author-link" href="/en/u/{self.actor_id}">问题</a>', html)
        self.assertIn('Version 2 of 2', html)
        self.assertIn('回应 &lt;img src=x&gt;', html)
        self.assertNotIn('<img src=x>', html)
        self.assertIn('Comment, respond and collaborate', html)
        self.assertIn(f'href="/en/workspace#exploration={self.work}&amp;revision={self.second}"', html)
        port = self.server.server_port
        self.assertIn(f'<link rel="canonical" href="http://127.0.0.1:{port}/en/w/{self.work}">', html)
        self.assertIn('<meta property="og:title" content="第二版 &lt;i&gt;">', html)
        self.assertIn('<meta name="citation_author" content="甲 &amp; 乙">', html)
        self.assertIn(f'hreflang="zh-CN" href="/zh/w/{self.work}"', html)
        body = re.sub(r'<[^>]+>', ' ', html.split('<main', 1)[1])
        self.assertNotRegex(body.replace('第二版', '').replace('修订后的正文', '').replace('回应', '')
                            .replace('甲 &amp; 乙', '').replace(r'甲 \&amp; 乙', '').replace('计算', '')
                            .replace('一段', '').replace('摘要', '').replace('问题', ''),
                            r'[一-鿿]')

    def test_author_text_is_never_translated_or_expanded(self):
        _, _, html = self.page(f'/en/w/{self.work}?revision={self.first}')
        self.assertIn('<h1 class="work-title">问题</h1>', html)  # a dictionary key, kept as written
        self.assertIn('You are reading an earlier version (1).', html)
        self.assertIn('一段 &lt;b&gt;摘要&lt;/b&gt;', html)
        body = html.split('<section class="work-body', 1)[1].split('</section>', 1)[0]
        self.assertIn('@title', body)
        self.assertNotIn('work-title', body)
        self.assertNotIn('<script>alert(1)</script>', html)
        self.assertIn('Erdős 1950', html)

    def test_negotiated_path_and_language_switch_keep_revision(self):
        code, _, html = self.page(f'/w/{self.work}', **{'Accept-Language': 'zh-CN'})
        self.assertEqual(code, 200)
        self.assertIn('<html lang="zh-CN">', html)
        self.assertIn('评论、回应与协作', html)
        self.assertIn(f'href="/en/w/{self.work}?revision={self.first}&amp;lang=switch"',
                      self.page(f'/zh/w/{self.work}?revision={self.first}')[2])
        code, headers, _ = self.call(f'/en/w/{self.work}?revision={self.first}&lang=switch')
        self.assertEqual(code, 302)
        self.assertEqual(headers['Location'], f'/en/w/{self.work}?revision={self.first}')

    def test_unknown_or_malformed_links_are_html_404(self):
        other = '00000000-0000-4000-8000-000000000000'
        for path in (f'/en/w/{other}', f'/w/{self.work}?revision={other}', f'/en/w/{self.work}?revision=x',
                     f'/en/w/{self.work}?revision={self.first}&revision={self.second}', f'/en/w/{self.work}?x=1',
                     f'/en/w/{self.first}'):
            with self.subTest(path=path):
                code, headers, html = self.page(path)
                self.assertEqual(code, 404)
                self.assertIn('text/html', headers['Content-Type'])
                self.assertIn('Work not found', html)
        self.assertEqual(self.call('/en/w/not-a-uuid')[0], 404)

    def test_cross_site_links_open_work_pages(self):
        self.assertEqual(self.call(f'/w/{self.work}', headers=NAV)[0], 200)
        self.assertEqual(self.call(f'/zh/w/{self.work}', headers=NAV)[0], 200)

    def test_ai_analysis_is_shown_on_the_work_page(self):
        from eksmu.analysis import Analysis, build_report
        from eksmu.store import Store
        db = str(Path(self.temp.name) / 'local.sqlite')
        store = Store(db); rows = store.rows(); store.close()
        rev = next(r for r in rows if r['id'] == self.second)
        work = Analysis(db + '.analysis.sqlite', rows)
        req = work.request(rev['actor_id'], self.second)
        status, _, page = self.call(f'/zh/w/{self.work}', headers=NAV)
        page = page.decode()
        self.assertIn('id="work-analysis" data-analysis-state="queued"', page)
        # No worker heartbeat yet: the page must not promise a quick result.
        self.assertIn('分析服务目前没有在运行', page)
        self.assertNotIn('通常 1–2 分钟内完成', page)
        work.heartbeat('gpt-6-luna')
        page = self.call(f'/zh/w/{self.work}', headers=NAV)[2].decode()
        self.assertIn('通常 1–2 分钟内完成', page)
        raw = {'summary': '本文讨论一个问题。', 'claims': [{'claim': '主张甲', 'section': '1', 'assessment': 'partially_supported',
                                                          'reasoning': '缺少一步'}],
               'strengths': ['例子清楚'], 'questions': ['定义是什么？'], 'suggestions': ['补充证明'], 'overall': '尚需补充。',
               'findings': [{'section': '1', 'quote': '修订后的 正文', 'category': 'clarity', 'severity': 'minor',
                             'confidence': 'low', 'statement': '<b>请</b>说明'}], 'not_checked': [], 'limitations': '',
               'suspicious_instructions': False}
        analyzer = {'kind': 'model', 'name': 'gpt-6-luna', 'requested_model': 'gpt-6-luna', 'prompt_template': 't',
                    'operator': 'eksmu.com', 'cost_borne_by': 'Eksmu operator'}
        report = build_report(rev, analyzer, raw, {'input_tokens': 1, 'output_tokens': 1, 'cost_usd': 0})
        work.finish(req['id'], 'done', '', report)
        work.respond(rev['actor_id'], report['id'], 'F1', 'dispute', '补充证明见定理 2', revision_ref=self.first)
        work.close()
        page = self.call(f'/zh/w/{self.work}', headers=NAV)[2].decode()
        self.assertIn('平台提供的基础分析', page)
        for section in ('概要', '主要论断评估', '部分成立，仍有缺口', '优点', '次要问题', '给作者的问题', '改进建议', '总体意见'):
            self.assertIn(section, page)
        # S6: verdict chip carries the words and a class for colour; severity shown as a chip too.
        self.assertIn('class="claim-item claim-partially_supported"', page)
        self.assertIn('<span class="badge claim-chip">部分成立，仍有缺口</span>', page)
        self.assertIn('<span class="badge sev-badge">次要</span>', page)
        # S6: in-page section nav with plain anchors and the AI report count.
        nav = page.split('<nav class="work-toc" id="work-toc"', 1)[1].split('</nav>', 1)[0]
        self.assertIn('href="#work-body"', nav)
        self.assertIn('href="#work-analysis" data-target="work-analysis">AI 审阅 <span class="mono n">1</span>', nav)
        self.assertIn('href="#work-discussion"', nav)
        self.assertIn('href="#work-abstract"', nav)                    # each link has a matching target
        for target in ('work-abstract', 'work-body', 'work-references'):
            self.assertIn(f'id="{target}"', page)
        # S6: analysis and discussion sit in the reading column, before the sidebar.
        self.assertLess(page.index('id="work-analysis"'), page.index('<aside class="work-sidebar"'))
        self.assertLess(page.index('id="work-discussion"'), page.index('<aside class="work-sidebar"'))
        self.assertIn('&lt;b&gt;请&lt;/b&gt;说明', page)              # model output is escaped
        self.assertNotIn('正文中找不到这段引文', page)                  # whitespace-insensitive quote check
        self.assertIn('补充证明见定理 2', page)                         # existing response rendered inline
        self.assertIn('关联版本 1', page)                               # revision_ref link rendered
        self.assertIn('name="revision_ref"', page)                      # revision_ref selector in reply form
        self.assertIn('class="finding-reply"', page)                   # inline finding reply form present
        feed = json.loads(self.call('/api/community/feed')[2].decode())
        self.assertEqual(feed['items'][0]['ai_reports'], 1)
        self.assertIn('/zh/openness.html', page)                       # openness verification guide linked
        self.assertIn('section=analysis', page)
        self.assertIn(f'data-author="{rev["actor_id"]}"', page)
        self.assertIn('写评论或回应', page)
        self.assertNotIn('在工作区回应', page)
        work = Analysis(db + '.analysis.sqlite', rows)
        work.db.execute("INSERT INTO analysis_requests VALUES('r2', ?, ?, '2026-10-11T00:00:00+00:00', 'queued', '', NULL, "
                        "'2026-10-11T00:00:00+00:00', '', 'platform')", (self.second, rev['actor_id']))
        work.finish('r2', 'done', '', build_report(rev, analyzer, dict(raw, summary='第二份概要'), {'input_tokens': 1, 'output_tokens': 1, 'cost_usd': 0}))
        work.close()
        page = self.call(f'/zh/w/{self.work}', headers=NAV)[2].decode()
        self.assertIn('早先的分析（1 份）', page)
        self.assertLess(page.index('第二份概要'), page.index('早先的分析'))     # newest first, older folded below
        self.assertIn('id="copy-credit-share"', page)
        self.assertIn('【Eksmu 记录链】', page)
        self.assertIn('附件不在此记录的覆盖范围内', page)
        english = self.call(f'/en/w/{self.work}', headers=NAV)[2].decode()
        self.assertIn('Basic analysis provided by the platform', english)
        self.assertIn('/en/openness.html', english)
        self.assertIn('Copy timestamp proof text', english)
        self.assertIn('[Eksmu record chain]', english)
        self.assertIn('Attachments are not covered by this record', english)

    def test_page_messages_have_english(self):
        english = i18n.strings('en')
        source = (Path(__file__).resolve().parents[1] / 'eksmu/work_page.py').read_text(encoding='utf-8')
        keys = [m.group(2) for m in re.finditer(r'''T\(\s*(['"])((?:(?!\1).)*)\1''', source)]
        from eksmu.work_page import KINDS, MODERATION_LABELS
        self.assertEqual([k for k in keys + list(KINDS.values()) + list(MODERATION_LABELS.values()) if k not in english], [])

    def test_seo_sitemap_feed_and_credit_certificate_svg(self):
        img_headers = {'Sec-Fetch-Site': 'cross-site', 'Sec-Fetch-Mode': 'no-cors', 'Sec-Fetch-Dest': 'image'}
        code, headers, body = self.call('/robots.txt')
        self.assertEqual(code, 200)
        self.assertIn('text/plain', headers['Content-Type'])
        self.assertIn(b'/sitemap.xml', body)

        code, headers, body = self.call('/sitemap.xml')
        self.assertEqual(code, 200)
        self.assertIn('application/xml', headers['Content-Type'])
        xml = body.decode('utf-8')
        self.assertIn(f'/zh/w/{self.work}</loc>', xml)
        self.assertIn(f'/en/w/{self.work}</loc>', xml)
        self.assertIn(f'/zh/u/{self.actor_id}</loc>', xml)
        self.assertIn(f'/en/u/{self.actor_id}</loc>', xml)
        self.assertIn('/zh/openness.html</loc>', xml)

        code, headers, body = self.call('/feed.xml')
        self.assertEqual(code, 200)
        self.assertIn('application/atom+xml', headers['Content-Type'])
        feed = body.decode('utf-8')
        self.assertIn('第二版 &lt;i&gt;', feed)
        self.assertIn(f'urn:uuid:{self.work}', feed)

        for card_path in (f'/zh/w/{self.work}/card.svg', f'/en/w/{self.work}/card.svg', f'/w/{self.work}/card.svg'):
            code, headers, body = self.call(card_path, headers=img_headers)
            self.assertEqual(code, 200)
            self.assertIn('image/svg+xml', headers['Content-Type'])
            svg = body.decode('utf-8')
            self.assertIn('第二版 &lt;i&gt;', svg)
            self.assertNotIn('<i>', svg)

        code, headers, body = self.call(f'/api/badge/{self.work}.svg', headers=img_headers)
        self.assertEqual(code, 200)
        self.assertIn('image/svg+xml', headers['Content-Type'])
        self.assertIn(b'Eksmu Credit', body)
        self.assertIn(b'SHA-256 #', body)

        _, _, html = self.page(f'/en/w/{self.work}')
        self.assertIn('"@type": "ScholarlyArticle"', html)
        self.assertIn(f'/en/w/{self.work}/card.svg', html)
        self.assertIn('id="copy-credit-badge"', html)
        self.assertIn('Download credit certificate (SVG)', html)

    def test_bibtex_citation_escape_canonical_url_and_withdrawn_in_both_languages(self):
        paper = {
            'format': 'eksmu.paper-metadata.v1',
            'abstract': '摘要',
            'authors': [{'name': '张_三 & {李}', 'contribution': ''}, {'name': 'Alice 100%', 'contribution': ''}],
            'references': [],
            'license': 'CC BY 4.0',
            'provenance': {'source': '', 'prior_publication_date': ''},
        }
        code, _, raw = self.call(
            '/api/explorations',
            {'title': '标题含 { } % & _ 和中文', 'body': '正文', 'paper': paper},
            self.token,
        )
        self.assertEqual(code, 201)
        created = json.loads(raw)
        wid = created['exploration']['id']
        rev1 = created['revision']['id']
        sha1 = created['revision']['sha256']
        year1, month1 = created['revision']['created_at'][:4], created['revision']['created_at'][5:7]

        code, _, raw2 = self.call(
            '/api/revisions',
            {'exploration_id': wid, 'parent_revision_id': rev1, 'title': 'V2 {x}%&_中文', 'body': '第二版正文'},
            self.token,
        )
        self.assertEqual(code, 201)
        rev2_doc = json.loads(raw2)
        rev2 = rev2_doc['id']
        sha2 = rev2_doc['sha256']
        port = self.server.server_port

        for lang, copy_text_label, copy_bib_label, dl_label in (
            ('zh', '复制纯文本引用', '复制 BibTeX', '下载 .bib'),
            ('en', 'Copy plain-text citation', 'Copy BibTeX', 'Download .bib'),
        ):
            with self.subTest(lang=lang, version='v2'):
                _, _, html = self.page(f'/{lang}/w/{wid}')
                canonical_v2 = f'http://127.0.0.1:{port}/{lang}/w/{wid}'
                self.assertIn(f'<meta name="citation_public_url" content="{canonical_v2}">', html)
                self.assertIn(f'@misc{{eksmu-{wid[:8]}-v2,', html)
                self.assertIn(r'author = {张\_三 \&amp; \char123{}李\char125{} and Alice 100\%},', html)
                self.assertIn(r'title = {{V2 \char123{}x\char125{}\%\&amp;\_中文}},', html)
                self.assertIn(f'year = {{{year1}}},', html)
                self.assertIn(f'month = {{{month1}}},', html)
                self.assertIn('howpublished = {Eksmu},', html)
                self.assertIn(f'url = {{{canonical_v2}}},', html)
                self.assertIn(f'note = {{Version 2; revision {rev2}; SHA-256 {sha2}}}', html)
                self.assertIn(f'download="eksmu-{wid[:8]}-v2.bib"', html)
                self.assertIn(copy_text_label, html)
                self.assertIn(copy_bib_label, html)
                self.assertIn(dl_label, html)
                self.assertIn('id="cite-copy-state" class="muted" role="status"', html)

            with self.subTest(lang=lang, version='v1'):
                _, _, html_v1 = self.page(f'/{lang}/w/{wid}?revision={rev1}')
                canonical_v1 = f'http://127.0.0.1:{port}/{lang}/w/{wid}?revision={rev1}'
                self.assertIn(f'<meta name="citation_public_url" content="{canonical_v1}">', html_v1)
                self.assertIn(f'@misc{{eksmu-{wid[:8]}-v1,', html_v1)
                self.assertIn(r'title = {{标题含 \char123{} \char125{} \% \&amp; \_ 和中文}},', html_v1)
                self.assertIn(f'url = {{{canonical_v1}}},', html_v1)
                self.assertIn(f'note = {{Version 1; revision {rev1}; SHA-256 {sha1}}}', html_v1)
                self.assertIn(f'download="eksmu-{wid[:8]}-v1.bib"', html_v1)

        code, _, _ = self.call(
            '/api/publications/event',
            {'revision_id': rev2, 'kind': 'withdraw', 'body': '发现一处算术疏漏', 'expected_previous_id': None,
             'operation_id': '00000000-0000-4000-8000-000000000099'},
            self.token,
        )
        self.assertEqual(code, 200)
        _, _, withdrawn_zh = self.page(f'/zh/w/{wid}')
        self.assertIn('作者已撤回这项作品', withdrawn_zh)
        self.assertIn('发现一处算术疏漏', withdrawn_zh)
        self.assertIn(f'@misc{{eksmu-{wid[:8]}-v2,', withdrawn_zh)
        _, _, withdrawn_en = self.page(f'/en/w/{wid}')
        self.assertIn('The author has withdrawn this work.', withdrawn_en)
        self.assertIn(f'@misc{{eksmu-{wid[:8]}-v2,', withdrawn_en)

    def test_comment_tree_nesting_orphans_deep_indent_anchors_and_escaping(self):
        from eksmu import work_page
        from eksmu.store import Store
        _, _, raw_actor = self.call('/api/actors', {'alias': '乙 <script>'})
        token2 = json.loads(raw_actor)['token']

        def post_comment(body_text, reply_to=None, tok=self.token):
            code, _, resp = self.call('/api/comments', {'revision_id': self.first, 'body': body_text, 'reply_to': reply_to}, tok)
            self.assertEqual(code, 201)
            return json.loads(resp)['id']

        c0 = post_comment('顶层甲 <b>1</b>', None, self.token)
        c_root2 = post_comment('顶层乙', None, token2)
        c1 = post_comment('一层回复 <img src=x onerror=1>', c0, token2)
        c1_sib = post_comment('同级后发回复', c0, self.token)
        c2 = post_comment('二层回复', c1, self.token)
        c3 = post_comment('三层回复', c2, token2)
        c4 = post_comment('四层回复', c3, self.token)
        c5 = post_comment('五层深回复', c4, token2)
        c6 = post_comment('六层深回复', c5, self.token)

        code, _, ev_raw = self.call(
            '/api/evidence',
            {'revision_id': self.first, 'source': 'human', 'scope': '定理1', 'method': '验算', 'result': '一致'},
            self.token,
        )
        self.assertEqual(code, 201)
        ev_id = json.loads(ev_raw)['id']
        c_orphan_ev = post_comment('孤儿回复（指向证据）', ev_id, token2)

        _, _, zh_html = self.page(f'/zh/w/{self.work}?revision={self.first}')
        _, _, en_html = self.page(f'/en/w/{self.work}?revision={self.first}')

        # Escaping
        self.assertIn('乙 &lt;script&gt;', zh_html)
        self.assertNotIn('<script>alert', zh_html)
        self.assertNotIn('乙 <script>', zh_html)
        self.assertIn('一层回复 &lt;img src=x onerror=1&gt;', zh_html)
        self.assertNotIn('<img src=x onerror=1>', zh_html)

        # Anchors and time links
        for cid in (c0, c_root2, c1, c1_sib, c2, c3, c4, c5, c6, c_orphan_ev):
            self.assertIn(f'id="c-{cid}"', zh_html)
            self.assertIn(f'href="#c-{cid}"', zh_html)
            self.assertIn(f'data-comment-id="{cid}"', zh_html)

        # Depth and visual indent cap at 4
        for cid, expected_depth in (
            (c0, 0), (c_root2, 0), (c_orphan_ev, 0),
            (c1, 1), (c1_sib, 1), (c2, 2), (c3, 3), (c4, 4), (c5, 4), (c6, 4),
        ):
            self.assertIn(f'id="c-{cid}" data-comment-id="{cid}" data-depth="{expected_depth}"', zh_html)
        self.assertIn('comment-children-flat', zh_html)

        # Parent-child visible hints preserved even at depth > 4
        self.assertIn(f'<a class="comment-parent-link" href="#c-{c0}">回复 问题</a>', zh_html)
        self.assertIn(f'<a class="comment-parent-link" href="#c-{c4}">回复 问题</a>', zh_html)
        self.assertIn(f'<a class="comment-parent-link" href="#c-{c5}">回复 乙 &lt;script&gt;</a>', zh_html)
        self.assertIn(f'<a class="comment-parent-link" href="#c-{c0}">Reply to 问题</a>', en_html)
        self.assertIn(f'<a class="comment-parent-link" href="#c-{c5}">Reply to 乙 &lt;script&gt;</a>', en_html)

        # Tree order: c1 and its subtree appear under c0 before c1_sib, and both before c_root2
        self.assertLess(zh_html.index(f'id="c-{c0}"'), zh_html.index(f'id="c-{c1}"'))
        self.assertLess(zh_html.index(f'id="c-{c1}"'), zh_html.index(f'id="c-{c6}"'))
        self.assertLess(zh_html.index(f'id="c-{c6}"'), zh_html.index(f'id="c-{c1_sib}"'))
        self.assertLess(zh_html.index(f'id="c-{c1_sib}"'), zh_html.index(f'id="c-{c_root2}"'))

        # Updated bottom discussion CTA text
        self.assertIn('提交证据或参与协作', zh_html)
        self.assertNotIn('回复某条评论、提交证据或参与协作', zh_html)
        self.assertIn('Submit evidence or collaborate', en_html)
        self.assertNotIn('Reply to a comment, submit evidence or collaborate', en_html)

        # Direct unit check for synthetic orphan reply whose parent comment ID does not exist
        db_path = str(Path(self.temp.name) / 'local.sqlite')
        store = Store(db_path)
        try:
            data = work_page.load(db_path, store.rows(), {}, self.work, self.first)
        finally:
            store.close()
        data['comments'].append({
            'id': '00000000-0000-4000-8000-000000000888',
            'type': 'comment',
            'actor_id': '00000000-0000-4000-8000-000000000777',
            'created_at': '2026-10-31T02:00:00+00:00',
            'payload': {
                'revision_id': self.first,
                'body': '缺失父评论的孤儿回复',
                'reply_to': '00000000-0000-4000-8000-000000000999',
            },
        })
        rendered_orphan = work_page.page(data, 'zh', 'http://127.0.0.1:8765').decode('utf-8')
        self.assertIn(
            'id="c-00000000-0000-4000-8000-000000000888" '
            'data-comment-id="00000000-0000-4000-8000-000000000888" data-depth="0"',
            rendered_orphan,
        )
        self.assertIn('缺失父评论的孤儿回复', rendered_orphan)

    def test_author_page_works_profile_withdrawn_empty_escape_privacy_and_404(self):
        code, headers, body = self.call(
            '/api/auth/email-register',
            {'alias': '张 <b>三</b>', 'email': 'secret-author@example.test', 'password': 'correct-horse-battery', 'label': 'dev'},
        )
        self.assertEqual(code, 200)
        reg1 = json.loads(body)
        aid1 = reg1['actor']['id']
        cookie1 = headers['Set-Cookie'].split(';', 1)[0]
        auth_headers1 = {'Cookie': cookie1}

        code, _, _ = self.call(
            '/api/auth/profile',
            {'display_name': '张 <b>三</b> @@title@@', 'affiliation': '自由研究所 <script>alert(1)</script>', 'expected_version': 1},
            headers=auth_headers1,
        )
        self.assertEqual(code, 200)

        # Publish work 1 (with 2 revisions, topic tags, and then withdraw it)
        code, _, raw_w1 = self.call(
            '/api/explorations',
            {'title': '第一篇 <img src=x>', 'body': '第一篇初稿正文', 'kind': 'conjecture'},
            headers=auth_headers1,
        )
        self.assertEqual(code, 201)
        w1_doc = json.loads(raw_w1)
        w1_id, w1_rev1 = w1_doc['exploration']['id'], w1_doc['revision']['id']
        code, _, raw_w1_v2 = self.call(
            '/api/revisions',
            {'exploration_id': w1_id, 'parent_revision_id': w1_rev1, 'title': '第一篇修订 <b>v2</b>', 'body': '第一篇修订正文'},
            headers=auth_headers1,
        )
        self.assertEqual(code, 201)
        w1_rev2 = json.loads(raw_w1_v2)['id']
        self.assertEqual(
            self.call(
                '/api/community/details',
                {'exploration_id': w1_id, 'topics': ['数论', '实验 <i>'], 'help': '', 'expected_version': 0},
                headers=auth_headers1,
            )[0],
            200,
        )
        self.assertEqual(
            self.call(
                '/api/publications/event',
                {'revision_id': w1_rev2, 'kind': 'withdraw', 'body': '撤回重算', 'expected_previous_id': None,
                 'operation_id': '00000000-0000-4000-8000-000000000123'},
                headers=auth_headers1,
            )[0],
            200,
        )

        # Publish work 2 (more recent, active, 1 version)
        code, _, raw_w2 = self.call(
            '/api/explorations',
            {'title': '第二篇新发现', 'body': '第二篇摘要与正文内容', 'kind': 'finding'},
            headers=auth_headers1,
        )
        self.assertEqual(code, 201)
        w2_doc = json.loads(raw_w2)
        w2_id, w2_rev1 = w2_doc['exploration']['id'], w2_doc['revision']['id']
        self.assertEqual(
            self.call(
                '/api/community/details',
                {'exploration_id': w2_id, 'topics': ['组合数学'], 'help': '', 'expected_version': 0},
                headers=auth_headers1,
            )[0],
            200,
        )

        # Author participates in 1 discussion comment
        self.assertEqual(
            self.call('/api/comments', {'revision_id': w2_rev1, 'body': '作者补充讨论'}, headers=auth_headers1)[0],
            201,
        )
        # Another participant responds on w2 so it passes the new-author cooling-off period
        self.assertEqual(
            self.call(
                '/api/evidence',
                {'revision_id': w2_rev1, 'source': 'human', 'scope': '命题', 'method': '验算', 'result': '通过'},
                self.token,
            )[0],
            201,
        )

        # Register a second author with no works
        code, headers2, body2 = self.call(
            '/api/auth/email-register',
            {'alias': '李四', 'email': 'noworks-private@example.test', 'password': 'correct-horse-battery', 'label': 'dev2'},
        )
        self.assertEqual(code, 200)
        aid2 = json.loads(body2)['actor']['id']

        port = self.server.server_port
        code, zh_headers, zh_html = self.page(f'/zh/u/{aid1}')
        self.assertEqual(code, 200)
        self.assertIn('text/html', zh_headers['Content-Type'])
        self.assertIn('<html lang="zh-CN">', zh_html)
        self.assertIn('<h1 class="work-title">张 &lt;b&gt;三&lt;/b&gt; @@title@@</h1>', zh_html)
        self.assertIn('自由研究所 &lt;script&gt;alert(1)&lt;/script&gt;（作者自述）', zh_html)
        self.assertNotIn('<script>alert(1)</script>', zh_html)
        self.assertNotIn('张 <b>三</b>', zh_html)
        self.assertIn('共发表 2 项作品 · 参与 1 条讨论', zh_html)
        self.assertIn(f'href="/zh/w/{w2_id}">第二篇新发现</a>', zh_html)
        self.assertIn(f'href="/zh/w/{w1_id}">第一篇修订 &lt;b&gt;v2&lt;/b&gt;</a>', zh_html)
        # Ordered by most recently published first (w2 before w1)
        self.assertLess(zh_html.index(w2_id), zh_html.index(w1_id))
        # Types, version numbers, dates, topics, withdrawn badge
        self.assertIn('发现', zh_html)
        self.assertIn('猜想', zh_html)
        self.assertIn('版本 2', zh_html)
        self.assertIn('版本 1', zh_html)
        self.assertIn(w1_doc['revision']['created_at'][:10], zh_html)
        self.assertIn('# 数论', zh_html)
        self.assertIn('# 实验 &lt;i&gt;', zh_html)
        self.assertIn('# 组合数学', zh_html)
        self.assertIn('<span class="badge badge-withdrawn">已撤回</span>', zh_html)
        # Never leak private email
        self.assertNotIn('secret-author@example.test', zh_html)
        self.assertNotIn('example.test', zh_html)
        # SEO & Person JSON-LD
        canonical_zh = f'http://127.0.0.1:{port}/zh/u/{aid1}'
        self.assertIn(f'<link rel="canonical" href="{canonical_zh}">', zh_html)
        self.assertIn('<meta property="og:type" content="profile">', zh_html)
        self.assertIn(f'<meta property="og:url" content="{canonical_zh}">', zh_html)
        ld_match = re.search(r'<script type="application/ld\+json">([^<]+)</script>', zh_html)
        self.assertIsNotNone(ld_match)
        ld_doc = json.loads(ld_match.group(1))
        self.assertEqual(
            ld_doc,
            {
                '@context': 'https://schema.org',
                '@type': 'Person',
                'name': '张 <b>三</b> @@title@@',
                'url': canonical_zh,
            },
        )

        # English author page
        code, _, en_html = self.page(f'/en/u/{aid1}')
        self.assertEqual(code, 200)
        self.assertIn('<html lang="en">', en_html)
        self.assertIn('自由研究所 &lt;script&gt;alert(1)&lt;/script&gt; (self-reported)', en_html)
        self.assertIn('Published works: 2 · Discussion comments: 1', en_html)
        self.assertIn(f'href="/en/w/{w2_id}">第二篇新发现</a>', en_html)
        self.assertIn(f'href="/en/w/{w1_id}">第一篇修订 &lt;b&gt;v2&lt;/b&gt;</a>', en_html)
        self.assertIn('<span class="badge badge-withdrawn">Withdrawn</span>', en_html)
        self.assertIn('Version 2', en_html)
        self.assertIn('first published', en_html)
        self.assertNotIn('secret-author@example.test', en_html)
        body_en = re.sub(r'<[^>]+>', ' ', en_html.split('<main', 1)[1])
        for author_str in ('张', '三', '自由研究所', '第二篇新发现', '第二篇摘要与正文内容', '第一篇修订正文', '第一篇修订', '数论', '实验', '组合数学'):
            body_en = body_en.replace(author_str, '')
        self.assertNotRegex(body_en, r'[一-鿿]')

        # Negotiated /u/<id>, language switch, and cross-site navigation
        code, _, neg_html = self.page(f'/u/{aid1}', **{'Accept-Language': 'zh-CN'})
        self.assertEqual(code, 200)
        self.assertIn('<html lang="zh-CN">', neg_html)
        self.assertEqual(self.call(f'/u/{aid1}', headers=NAV)[0], 200)
        self.assertEqual(self.call(f'/zh/u/{aid1}', headers=NAV)[0], 200)
        code, sw_headers, _ = self.call(f'/en/u/{aid1}?lang=switch')
        self.assertEqual((code, sw_headers['Location']), (302, f'/en/u/{aid1}'))

        # Author with no works: friendly empty state, no email leak, not in sitemap
        code, _, empty_zh = self.page(f'/zh/u/{aid2}')
        self.assertEqual(code, 200)
        self.assertIn('李四', empty_zh)
        self.assertIn('这位作者还没有公开发表的作品。', empty_zh)
        self.assertNotIn('noworks-private@example.test', empty_zh)
        code, _, empty_en = self.page(f'/en/u/{aid2}')
        self.assertEqual(code, 200)
        self.assertIn('This author has not published any works yet.', empty_en)
        self.assertNotIn('noworks-private@example.test', empty_en)

        _, _, sitemap = self.call('/sitemap.xml')
        sitemap_text = sitemap.decode('utf-8')
        self.assertIn(f'/zh/u/{aid1}</loc>', sitemap_text)
        self.assertIn(f'/en/u/{aid1}</loc>', sitemap_text)
        self.assertNotIn(aid2, sitemap_text)

        # Invalid actor_id, non-existent actor, non-actor UUID, or unexpected query params -> 404 HTML page
        unknown_uuid = '00000000-0000-4000-8000-000000000000'
        for bad_path in (
            '/u/not-a-uuid',
            '/zh/u/not-a-uuid',
            '/en/u/not-a-uuid',
            '/en/u/12345',
            f'/u/{unknown_uuid}',
            f'/zh/u/{unknown_uuid}',
            f'/en/u/{unknown_uuid}',
            f'/en/u/{self.work}',
            f'/en/u/{aid1}?foo=bar',
        ):
            with self.subTest(bad_path=bad_path):
                code, bad_headers, bad_html = self.page(bad_path)
                self.assertEqual(code, 404)
                self.assertIn('text/html', bad_headers['Content-Type'])
                self.assertTrue('找不到这项作品' in bad_html or 'Work not found' in bad_html)

    def test_moderation_labels_commercial_promotion_and_pending_review_across_all_outlets(self):
        from eksmu.community import Community
        from eksmu.store import Store

        db_path = Path(self.temp.name) / 'local.sqlite'
        # Register a dedicated author for labeled works
        _, _, promo_actor_raw = self.call('/api/actors', {'alias': '推广账户'})
        promo_actor = json.loads(promo_actor_raw)
        promo_token = promo_actor['token']
        promo_aid = promo_actor['actor']['id']

        code, _, raw_w = self.call(
            '/api/explorations',
            {'title': '伪学术商品推广文', 'body': '正文含外链 [购买链接](https://shop.example.com/item)', 'kind': 'finding'},
            promo_token,
        )
        self.assertEqual(code, 201)
        w_doc = json.loads(raw_w)
        wid = w_doc['exploration']['id']
        rev_id = w_doc['revision']['id']
        # Peer comment so cooling-off is passed and only the label controls distribution
        self.assertEqual(
            self.call('/api/comments', {'revision_id': rev_id, 'body': '这是一条同行评论'}, self.peer_token)[0],
            201,
        )
        self.assertEqual(
            self.call('/api/community/details', {'exploration_id': wid, 'topics': ['推广测试主题'], 'help': '寻人协作', 'expected_version': 0}, promo_token)[0],
            200,
        )

        # Before labeling: present in sitemap, feed.xml, feed views, topics, and has citation_* / JSON-LD, no noindex
        _, _, html_before = self.page(f'/zh/w/{wid}')
        self.assertNotIn('<meta name="robots" content="noindex">', html_before)
        self.assertIn('name="citation_title"', html_before)
        self.assertIn('"@type": "ScholarlyArticle"', html_before)
        self.assertIn('rel="ugc nofollow noopener noreferrer"', html_before)
        self.assertIn(wid, self.call('/sitemap.xml')[2].decode('utf-8'))
        self.assertIn(promo_aid, self.call('/sitemap.xml')[2].decode('utf-8'))
        self.assertIn(wid, self.call('/feed.xml')[2].decode('utf-8'))

        for label_kind, zh_label, en_label, source in (
            ('commercial_promotion', '商业推广', 'Commercial promotion', 'maintainer'),
            ('pending_review', '待复核', 'Pending review', 'maintainer'),
            ('pending_review', '待复核', 'Pending review', 'ai'),
        ):
            with self.subTest(label_kind=label_kind, source=source):
                store = Store(db_path)
                try:
                    hub = Community(str(db_path) + '.community.sqlite', store.rows())
                    try:
                        lbl = hub.label_work(wid, label_kind, '附带商品转化链接', 'op_maintainer_1', source=source)
                    finally:
                        hub.close()
                finally:
                    store.close()

                # Work page (guest): noindex, no citation_*, no ScholarlyArticle, top notice shown, no private appeal form
                _, _, zh_html = self.page(f'/zh/w/{wid}')
                self.assertIn('<meta name="robots" content="noindex">', zh_html)
                self.assertNotIn('name="citation_', zh_html)
                self.assertNotIn('ScholarlyArticle', zh_html)
                if source == 'ai':
                    self.assertIn(
                        '系统初筛认为此作品可能以推广为主要目的，正在等待人工复核；复核前不进入首页与搜索引擎收录。作品页、版本与时间证据不受影响。理由：附带商品转化链接',
                        zh_html,
                    )
                else:
                    self.assertIn(
                        f'此作品被标记为：{zh_label}。不进入首页、站点地图与搜索引擎收录；作品页、版本与时间证据保留。理由：附带商品转化链接',
                        zh_html,
                    )
                self.assertIn('id="work-time"', zh_html)
                self.assertNotIn('id="work-label-appeal-form"', zh_html)

                _, _, en_html = self.page(f'/en/w/{wid}')
                self.assertIn('<meta name="robots" content="noindex">', en_html)
                self.assertNotIn('name="citation_', en_html)
                self.assertNotIn('ScholarlyArticle', en_html)
                if source == 'ai':
                    self.assertIn(
                        'Preliminary screening indicates this work may have promotion as its primary purpose and is awaiting human review; until reviewed, it is excluded from the home feed and search engine indexing. The work page, versions, and time evidence are unaffected. Reason: 附带商品转化链接',
                        en_html,
                    )
                else:
                    self.assertIn(
                        f'This work is labeled as: {en_label}. It is excluded from the home feed, sitemap, and search engine indexing; the work page, versions, and time evidence are preserved. Reason: 附带商品转化链接',
                        en_html,
                    )

                # Work page (author logged in): appeal form shown
                _, _, author_zh = self.page(f'/zh/w/{wid}', Authorization='Bearer ' + promo_token)
                self.assertIn('id="work-label-appeal-form"', author_zh)
                self.assertIn('作者申诉', author_zh)

                # Sitemap and Atom feed exclude the labeled work; author has no other distributable work -> author excluded from sitemap & author page has noindex
                sitemap_xml = self.call('/sitemap.xml')[2].decode('utf-8')
                self.assertNotIn(wid, sitemap_xml)
                self.assertNotIn(promo_aid, sitemap_xml)
                feed_xml = self.call('/feed.xml')[2].decode('utf-8')
                self.assertNotIn(wid, feed_xml)

                _, _, author_page_zh = self.page(f'/zh/u/{promo_aid}')
                self.assertIn('<meta name="robots" content="noindex">', author_page_zh)
                self.assertIn(f'<span class="badge badge-moderation">{zh_label}</span>', author_page_zh)
                self.assertIn('不予分发理由：附带商品转化链接', author_page_zh)
                self.assertIn('伪学术商品推广文', author_page_zh)

                # Feed views: excluded from all/unanswered/collaborate, present in labeled and mine/following/bookmarks, excluded from topics
                for v in ('all', 'unanswered', 'collaborate'):
                    items = json.loads(self.call(f'/api/community/feed?view={v}')[2].decode())['items']
                    self.assertNotIn(wid, [x['id'] for x in items])
                labeled_items = json.loads(self.call('/api/community/feed?view=labeled')[2].decode())['items']
                self.assertIn(wid, [x['id'] for x in labeled_items])
                labeled_card = next(x for x in labeled_items if x['id'] == wid)
                self.assertEqual(labeled_card['moderation_label'], label_kind)
                self.assertEqual(labeled_card['moderation_reason'], '附带商品转化链接')
                self.assertIsNone(labeled_card.get('chain_seq'))
                self.assertIsNone(labeled_card.get('bitcoin_height'))
                self.assertEqual(labeled_card.get('ai_reports'), 0)

                mine_items = json.loads(self.call('/api/community/feed?view=mine', token=promo_token)[2].decode())['items']
                self.assertIn(wid, [x['id'] for x in mine_items])
                topics = json.loads(self.call('/api/community/topics')[2].decode())['items']
                self.assertNotIn('推广测试主题', [t['name'] for t in topics])

                # /api/mirror and record chain are unaffected
                mirror_manifest = json.loads(self.call('/api/mirror')[2].decode())
                self.assertIn(wid, [r['id'] for r in mirror_manifest['records']['records']])
                proof = json.loads(self.call(f'/api/chain/proof?record={rev_id}')[2].decode())
                self.assertEqual(proof['record']['id'], rev_id)

                # Retract immediately restores distribution
                store = Store(db_path)
                try:
                    hub = Community(str(db_path) + '.community.sqlite', store.rows())
                    try:
                        hub.retract_label(lbl['id'], '核对后撤回', 'op_maintainer_2')
                    finally:
                        hub.close()
                finally:
                    store.close()
                self.assertIn(wid, self.call('/sitemap.xml')[2].decode('utf-8'))
                self.assertIn(promo_aid, self.call('/sitemap.xml')[2].decode('utf-8'))
                self.assertIn(wid, self.call('/feed.xml')[2].decode('utf-8'))
                _, _, zh_restored = self.page(f'/zh/w/{wid}')
                self.assertNotIn('<meta name="robots" content="noindex">', zh_restored)
                self.assertIn('name="citation_title"', zh_restored)
                self.assertIn('"@type": "ScholarlyArticle"', zh_restored)

    def test_cooling_off_period_trusted_author_and_peer_response(self):
        from datetime import datetime, timedelta, timezone
        from eksmu import work_page
        from eksmu.community import Community, distribution
        from eksmu.store import Store

        db_path = Path(self.temp.name) / 'local.sqlite'
        _, _, new_actor_raw = self.call('/api/actors', {'alias': '新作者'})
        new_actor = json.loads(new_actor_raw)
        new_token = new_actor['token']
        new_aid = new_actor['actor']['id']

        code, _, raw_w1 = self.call(
            '/api/explorations',
            {'title': '新作者首篇作品', 'body': '探索正文内容', 'kind': 'question'},
            new_token,
        )
        self.assertEqual(code, 201)
        w1_doc = json.loads(raw_w1)
        w1_id = w1_doc['exploration']['id']
        w1_rev = w1_doc['revision']['id']
        created_dt = datetime.fromisoformat(w1_doc['exploration']['created_at'])

        # 1. During 72h cooling-off (with only self-comment):
        # - Home feed STILL lists w1
        # - Work page has noindex, no citation_*, no ScholarlyArticle
        # - Sitemap and feed.xml exclude w1 and new_aid; author page has noindex
        self.assertEqual(self.call('/api/comments', {'revision_id': w1_rev, 'body': '作者自己的评论'}, new_token)[0], 201)
        feed_all = json.loads(self.call('/api/community/feed?view=all')[2].decode())['items']
        self.assertIn(w1_id, [x['id'] for x in feed_all])

        _, _, w1_html = self.page(f'/zh/w/{w1_id}')
        self.assertIn('<meta name="robots" content="noindex">', w1_html)
        self.assertNotIn('name="citation_', w1_html)
        self.assertNotIn('ScholarlyArticle', w1_html)
        self.assertNotIn(w1_id, self.call('/sitemap.xml')[2].decode('utf-8'))
        self.assertNotIn(new_aid, self.call('/sitemap.xml')[2].decode('utf-8'))
        self.assertNotIn(w1_id, self.call('/feed.xml')[2].decode('utf-8'))
        _, _, author_html = self.page(f'/zh/u/{new_aid}')
        self.assertIn('<meta name="robots" content="noindex">', author_html)

        # 2. Clock injection: at +71h still cooling_off; at +72h passes cooling_off without sleeping
        store = Store(db_path)
        try:
            rows = store.rows()
        finally:
            store.close()
        t_71h = created_dt + timedelta(hours=71, minutes=59)
        t_72h = created_dt + timedelta(hours=72, seconds=1)
        self.assertFalse(distribution(w1_id, rows=rows, db_path=db_path, now=t_71h)['distribute'])
        self.assertTrue(distribution(w1_id, rows=rows, db_path=db_path, now=t_71h)['cooling_off'])
        self.assertTrue(distribution(w1_id, rows=rows, db_path=db_path, now=t_72h)['distribute'])
        self.assertFalse(distribution(w1_id, rows=rows, db_path=db_path, now=t_72h)['cooling_off'])

        data_72h = work_page.load(str(db_path), rows, {}, w1_id, None, now=t_72h)
        html_72h = work_page.page(data_72h, 'zh', 'http://127.0.0.1').decode('utf-8')
        self.assertNotIn('<meta name="robots" content="noindex">', html_72h)
        self.assertIn('name="citation_title"', html_72h)
        self.assertIn('ScholarlyArticle', html_72h)
        self.assertIn(w1_id, work_page.sitemap_xml(str(db_path), rows, 'http://127.0.0.1', now=t_72h).decode('utf-8'))
        self.assertIn(w1_id, work_page.atom_feed(str(db_path), rows, {}, 'http://127.0.0.1', now=t_72h).decode('utf-8'))

        # 3. Peer response before 72h immediately ends cooling-off AND establishes trust for the author's subsequent works
        self.assertEqual(self.call('/api/comments', {'revision_id': w1_rev, 'body': '同行回应解除冷静期'}, self.peer_token)[0], 201)
        _, _, w1_after_peer = self.page(f'/zh/w/{w1_id}')
        self.assertNotIn('<meta name="robots" content="noindex">', w1_after_peer)
        self.assertIn('name="citation_title"', w1_after_peer)
        self.assertIn(w1_id, self.call('/sitemap.xml')[2].decode('utf-8'))
        self.assertIn(new_aid, self.call('/sitemap.xml')[2].decode('utf-8'))
        self.assertIn(w1_id, self.call('/feed.xml')[2].decode('utf-8'))
        _, _, author_after_peer = self.page(f'/zh/u/{new_aid}')
        self.assertNotIn('<meta name="robots" content="noindex">', author_after_peer)

        # 4. Subsequent work w2 by the now-trusted author is immediately distributed (< 72h, 0 responses)
        code, _, raw_w2 = self.call(
            '/api/explorations',
            {'title': '已建立信任作者的第二篇', 'body': '直接分发正文', 'kind': 'finding'},
            new_token,
        )
        self.assertEqual(code, 201)
        w2_id = json.loads(raw_w2)['exploration']['id']
        _, _, w2_html = self.page(f'/zh/w/{w2_id}')
        self.assertNotIn('<meta name="robots" content="noindex">', w2_html)
        self.assertIn('name="citation_title"', w2_html)
        self.assertIn('ScholarlyArticle', w2_html)
        self.assertIn(w2_id, self.call('/sitemap.xml')[2].decode('utf-8'))
        self.assertIn(w2_id, self.call('/feed.xml')[2].decode('utf-8'))

        # 5. If the prior work w1 is labeled, it no longer grants trusted-author status to w2
        store = Store(db_path)
        try:
            hub = Community(str(db_path) + '.community.sqlite', store.rows())
            try:
                lbl = hub.label_work(w1_id, 'commercial_promotion', '推广链接', 'op_maintainer_1')
            finally:
                hub.close()
        finally:
            store.close()
        _, _, w2_untrusted_html = self.page(f'/zh/w/{w2_id}')
        self.assertIn('<meta name="robots" content="noindex">', w2_untrusted_html)
        self.assertNotIn('name="citation_', w2_untrusted_html)
        store = Store(db_path)
        try:
            hub = Community(str(db_path) + '.community.sqlite', store.rows())
            try:
                hub.retract_label(lbl['id'], '恢复', 'op_maintainer_2')
            finally:
                hub.close()
        finally:
            store.close()

        # 6. Manual maintainer trust (trust_author / untrust_author) and work_number order (including withdrawn works)
        _, _, brand_new_raw = self.call('/api/actors', {'alias': '受信任新作者'})
        brand_new = json.loads(brand_new_raw)
        bn_token = brand_new['token']
        bn_aid = brand_new['actor']['id']
        code, _, raw_w3 = self.call(
            '/api/explorations',
            {'title': '手动信任作者首作', 'body': '无需等待 72 小时', 'kind': 'conjecture'},
            bn_token,
        )
        self.assertEqual(code, 201)
        w3_id = json.loads(raw_w3)['exploration']['id']
        # Before manual trust: in cooling-off (noindex, not in sitemap/feed.xml)
        _, _, w3_before = self.page(f'/zh/w/{w3_id}')
        self.assertIn('<meta name="robots" content="noindex">', w3_before)
        self.assertNotIn('name="citation_', w3_before)
        self.assertIn('本站第 4 篇作品', w3_before)
        _, _, w3_before_en = self.page(f'/en/w/{w3_id}')
        self.assertIn('Work #4 on this site', w3_before_en)
        self.assertNotIn(w3_id, self.call('/sitemap.xml')[2].decode('utf-8'))
        self.assertNotIn(w3_id, self.call('/feed.xml')[2].decode('utf-8'))

        # Maintainer trusts bn_aid -> immediately enters sitemap, feed.xml, Scholar metadata
        store = Store(db_path)
        try:
            hub = Community(str(db_path) + '.community.sqlite', store.rows())
            try:
                with self.assertRaises( Exception ):
                    hub.trust_author('00000000-0000-4000-8000-000000000000', '不存在', 'op_maintainer_1')
                hub.trust_author(bn_aid, '已核实身份的研究者', 'op_maintainer_1')
            finally:
                hub.close()
        finally:
            store.close()

        _, _, w3_trusted = self.page(f'/zh/w/{w3_id}')
        self.assertNotIn('<meta name="robots" content="noindex">', w3_trusted)
        self.assertIn('name="citation_title"', w3_trusted)
        self.assertIn('ScholarlyArticle', w3_trusted)
        self.assertIn(w3_id, self.call('/sitemap.xml')[2].decode('utf-8'))
        self.assertIn(bn_aid, self.call('/sitemap.xml')[2].decode('utf-8'))
        self.assertIn(w3_id, self.call('/feed.xml')[2].decode('utf-8'))

        # Labels still take precedence over manual trust
        store = Store(db_path)
        try:
            hub = Community(str(db_path) + '.community.sqlite', store.rows())
            try:
                lbl3 = hub.label_work(w3_id, 'pending_review', '待人工复核', 'op_maintainer_1')
            finally:
                hub.close()
        finally:
            store.close()
        _, _, w3_labeled = self.page(f'/zh/w/{w3_id}')
        self.assertIn('<meta name="robots" content="noindex">', w3_labeled)
        self.assertNotIn('name="citation_', w3_labeled)
        self.assertNotIn(w3_id, self.call('/sitemap.xml')[2].decode('utf-8'))
        self.assertNotIn(w3_id, self.call('/feed.xml')[2].decode('utf-8'))

        # Retract label and untrust author -> returns to cooling-off judgement
        store = Store(db_path)
        try:
            hub = Community(str(db_path) + '.community.sqlite', store.rows())
            try:
                hub.retract_label(lbl3['id'], '复核通过', 'op_maintainer_2')
                hub.untrust_author(bn_aid, '撤销手动信任', 'op_maintainer_2')
            finally:
                hub.close()
        finally:
            store.close()
        _, _, w3_untrusted = self.page(f'/zh/w/{w3_id}')
        self.assertIn('<meta name="robots" content="noindex">', w3_untrusted)
        self.assertNotIn('name="citation_', w3_untrusted)
        self.assertNotIn(w3_id, self.call('/sitemap.xml')[2].decode('utf-8'))
        self.assertNotIn(w3_id, self.call('/feed.xml')[2].decode('utf-8'))

    def test_format_size_boundaries(self):
        from eksmu.work_page import format_size, format_size_title
        cases = (
            (0, '0 B', '0 字节', '0 bytes'),
            (1023, '1023 B', '1,023 字节', '1,023 bytes'),
            (1024, '1.0 KiB', '1,024 字节', '1,024 bytes'),
            (1536, '1.5 KiB', '1,536 字节', '1,536 bytes'),
            (6784541, '6.5 MiB', '6,784,541 字节', '6,784,541 bytes'),
            (1024 ** 3, '1.0 GiB', '1,073,741,824 字节', '1,073,741,824 bytes'),
        )
        for size, expected_str, expected_zh_title, expected_en_title in cases:
            with self.subTest(size=size):
                self.assertEqual(format_size(size), expected_str)
                self.assertEqual(format_size_title(size, lambda m, **p: i18n.t('zh', m, **p)), expected_zh_title)
                self.assertEqual(format_size_title(size, lambda m, **p: i18n.t('en', m, **p)), expected_en_title)

    def test_citation_pdf_url_and_work_pdf_route(self):
        import base64
        from eksmu import work_page
        from eksmu.community import Community
        from eksmu.research import Research
        from eksmu.store import Store

        db_path = Path(self.temp.name) / 'local.sqlite'
        port = self.server.server_port

        # 1. No PDF attachment -> no citation_pdf_url
        _, _, html_no_pdf = self.page(f'/zh/w/{self.work}')
        self.assertIn('name="citation_title"', html_no_pdf)
        self.assertNotIn('citation_pdf_url', html_no_pdf)

        # Upload a non-PDF attachment (CSV) and two PDF attachments by the author on self.second
        csv_bytes = b'col1,col2\n1,2\n'
        code, _, raw_csv = self.call(
            '/api/research/upload',
            {'revision_id': self.second, 'filename': 'data.csv', 'data_b64': base64.b64encode(csv_bytes).decode()},
            self.token,
        )
        self.assertEqual(code, 200)
        csv_id = json.loads(raw_csv)['id']

        pdf1_bytes = b'%PDF-1.4\nfirst-author-pdf-' + b'x' * 1510
        code, _, raw_pdf1 = self.call(
            '/api/research/upload',
            {'revision_id': self.second, 'filename': '论文初稿 v2.pdf', 'data_b64': base64.b64encode(pdf1_bytes).decode()},
            self.token,
        )
        self.assertEqual(code, 200)
        pdf1_id = json.loads(raw_pdf1)['id']

        pdf2_bytes = b'%PDF-1.7\nsecond-author-pdf'
        code, _, raw_pdf2 = self.call(
            '/api/research/upload',
            {'revision_id': self.second, 'filename': 'appendix.pdf', 'data_b64': base64.b64encode(pdf2_bytes).decode()},
            self.token,
        )
        self.assertEqual(code, 200)
        pdf2_id = json.loads(raw_pdf2)['id']

        # 2. Distributable work with author PDFs -> citation_pdf_url points to the first author PDF (absolute URL)
        _, _, zh_html = self.page(f'/zh/w/{self.work}')
        expected_zh_pdf_url = f'http://127.0.0.1:{port}/w/{self.work}/pdf/{pdf1_id}'
        self.assertIn(f'<meta name="citation_pdf_url" content="{expected_zh_pdf_url}">', zh_html)
        self.assertNotIn(f'/pdf/{pdf2_id}">', zh_html.split('</head>', 1)[0])
        # Materials section has "在线阅读 PDF" for PDFs, not for CSV, and human-readable size with exact bytes in title
        self.assertIn(f'href="/w/{self.work}/pdf/{pdf1_id}">在线阅读 PDF</a>', zh_html)
        self.assertIn(f'href="/w/{self.work}/pdf/{pdf2_id}">在线阅读 PDF</a>', zh_html)
        self.assertNotIn(f'/pdf/{csv_id}', zh_html)
        self.assertIn(f'<span title="{len(pdf1_bytes):,} 字节">1.5 KiB</span>', zh_html)
        self.assertIn(f'<span title="{len(csv_bytes)} 字节">{len(csv_bytes)} B</span>', zh_html)

        # PDF-only version: publishable, with a note that the site does not read the PDF
        self.assertIn('本版只有 PDF，没有源码：本站不读取 PDF 内容', zh_html)
        self.assertNotIn('本版只有 PDF', v1_first := self.page(f'/zh/w/{self.work}?revision={self.first}')[2])

        _, _, en_html = self.page(f'/en/w/{self.work}')
        self.assertIn('This version has only a PDF and no source', en_html)
        expected_en_pdf_url = f'http://127.0.0.1:{port}/w/{self.work}/pdf/{pdf1_id}'
        self.assertIn(f'<meta name="citation_pdf_url" content="{expected_en_pdf_url}">', en_html)
        self.assertIn(f'href="/w/{self.work}/pdf/{pdf1_id}">Read PDF online</a>', en_html)
        self.assertIn(f'<span title="{len(pdf1_bytes):,} bytes">1.5 KiB</span>', en_html)

        # Earlier version self.first has no PDF -> viewing ?revision=self.first has no citation_pdf_url
        _, _, v1_html = self.page(f'/zh/w/{self.work}?revision={self.first}')
        self.assertIn('name="citation_title"', v1_html)
        self.assertNotIn('citation_pdf_url', v1_html)

        # 3. Non-author PDF attachment is never used as citation_pdf_url
        store = Store(db_path)
        try:
            data_foreign = work_page.load(str(db_path), store.rows(), {}, self.work, self.first)
        finally:
            store.close()
        data_foreign['materials'] = [{
            'id': '00000000-0000-4000-8000-000000000555',
            'revision_id': self.first,
            'actor_id': '00000000-0000-4000-8000-000000000666',
            'filename': 'peer.pdf',
            'mime': 'application/pdf',
            'sha256': 'a' * 64,
            'created_at': '2026-10-01T00:00:00+00:00',
            'size': 6784541,
            'has_preview': False,
        }]
        rendered_foreign = work_page.page(data_foreign, 'zh', f'http://127.0.0.1:{port}').decode('utf-8')
        self.assertIn('name="citation_title"', rendered_foreign)
        self.assertNotIn('citation_pdf_url', rendered_foreign)
        self.assertIn('<span title="6,784,541 字节">6.5 MiB</span>', rendered_foreign)

        # 4. PDF route GET and HEAD on /w/..., /zh/w/..., /en/w/... (including PDF from an earlier version)
        v1_pdf_bytes = b'%PDF-1.5\nv1-pdf-content'
        code, _, raw_v1_pdf = self.call(
            '/api/research/upload',
            {'revision_id': self.first, 'filename': 'v1.pdf', 'data_b64': base64.b64encode(v1_pdf_bytes).decode()},
            self.token,
        )
        self.assertEqual(code, 200)
        v1_pdf_id = json.loads(raw_v1_pdf)['id']

        for route in (
            f'/w/{self.work}/pdf/{pdf1_id}',
            f'/zh/w/{self.work}/pdf/{pdf1_id}',
            f'/en/w/{self.work}/pdf/{pdf1_id}',
        ):
            code, pdf_headers, pdf_body = self.call(route)
            self.assertEqual(code, 200)
            self.assertEqual(pdf_body, pdf1_bytes)
            self.assertEqual(pdf_headers['Content-Type'], 'application/pdf')
            self.assertEqual(pdf_headers['Content-Length'], str(len(pdf1_bytes)))
            self.assertEqual(
                pdf_headers['Content-Disposition'],
                "inline; filename*=UTF-8''%E8%AE%BA%E6%96%87%E5%88%9D%E7%A8%BF%20v2.pdf",
            )
            self.assertEqual(pdf_headers['X-Content-Type-Options'], 'nosniff')
            self.assertEqual(pdf_headers['Cache-Control'], 'public, max-age=3600')
            # Built-in PDF viewers do not run in sandboxed documents, so the PDF route only forbids framing.
            self.assertEqual(pdf_headers['Content-Security-Policy'], "frame-ancestors 'none'")
            self.assertNotIn('X-Robots-Tag', pdf_headers)

            # HEAD request
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
            try:
                conn.request('HEAD', route)
                h_resp = conn.getresponse()
                h_body = h_resp.read()
                h_headers = dict(h_resp.getheaders())
                self.assertEqual(h_resp.status, 200)
                self.assertEqual(h_body, b'')
                self.assertEqual(h_headers['Content-Type'], 'application/pdf')
                self.assertEqual(h_headers['Content-Length'], str(len(pdf1_bytes)))
                self.assertIn('inline;', h_headers['Content-Disposition'])
            finally:
                conn.close()

        # Cross-site top-level navigation to PDF is permitted
        self.assertEqual(self.call(f'/zh/w/{self.work}/pdf/{pdf1_id}', headers=NAV)[0], 200)
        # Earlier version PDF is also served under the same work
        code, _, v1_body = self.call(f'/w/{self.work}/pdf/{v1_pdf_id}')
        self.assertEqual((code, v1_body), (200, v1_pdf_bytes))

        # 5. Non-distributable states (labeled, cooling-off, withdrawn):
        #    - citation_pdf_url is NOT emitted on work page
        #    - PDF route STILL returns 200 with full bytes, plus X-Robots-Tag: noindex
        # (a) Labeled
        store = Store(db_path)
        try:
            hub = Community(str(db_path) + '.community.sqlite', store.rows())
            try:
                lbl = hub.label_work(self.work, 'pending_review', '测试待复核', 'op_m1')
            finally:
                hub.close()
        finally:
            store.close()
        _, _, labeled_html = self.page(f'/zh/w/{self.work}')
        self.assertNotIn('citation_pdf_url', labeled_html)
        code, lbl_pdf_headers, lbl_pdf_body = self.call(f'/zh/w/{self.work}/pdf/{pdf1_id}')
        self.assertEqual(code, 200)
        self.assertEqual(lbl_pdf_body, pdf1_bytes)
        self.assertEqual(lbl_pdf_headers.get('X-Robots-Tag'), 'noindex')

        store = Store(db_path)
        try:
            hub = Community(str(db_path) + '.community.sqlite', store.rows())
            try:
                hub.retract_label(lbl['id'], '恢复', 'op_m2')
            finally:
                hub.close()
        finally:
            store.close()

        # (b) Withdrawn
        self.assertEqual(
            self.call(
                '/api/publications/event',
                {'revision_id': self.second, 'kind': 'withdraw', 'body': '测试撤回', 'expected_previous_id': None,
                 'operation_id': '00000000-0000-4000-8000-000000000701'},
                self.token,
            )[0],
            200,
        )
        _, _, wd_html = self.page(f'/zh/w/{self.work}')
        self.assertNotIn('citation_pdf_url', wd_html)
        code, wd_pdf_headers, wd_pdf_body = self.call(f'/zh/w/{self.work}/pdf/{pdf1_id}')
        self.assertEqual(code, 200)
        self.assertEqual(wd_pdf_body, pdf1_bytes)
        self.assertEqual(wd_pdf_headers.get('X-Robots-Tag'), 'noindex')
        # Reinstate
        wd_head = json.loads(self.call(f'/api/publications/workspace?revision_id={self.second}')[2])['status']['head']
        self.assertEqual(
            self.call(
                '/api/publications/event',
                {'revision_id': self.second, 'kind': 'reinstate', 'body': '恢复作品', 'expected_previous_id': wd_head,
                 'operation_id': '00000000-0000-4000-8000-000000000702'},
                self.token,
            )[0],
            200,
        )

        # (c) Cooling-off new author
        _, _, cool_actor = self.call('/api/actors', {'alias': '冷静期作者'})
        cool_tok = json.loads(cool_actor)['token']
        _, _, cool_w_raw = self.call('/api/explorations', {'title': '冷静期论文', 'body': '正文'}, cool_tok)
        cool_w = json.loads(cool_w_raw)
        cool_wid, cool_rev = cool_w['exploration']['id'], cool_w['revision']['id']
        _, _, cool_pdf_raw = self.call(
            '/api/research/upload',
            {'revision_id': cool_rev, 'filename': 'cool.pdf', 'data_b64': base64.b64encode(pdf1_bytes).decode()},
            cool_tok,
        )
        cool_pdf_id = json.loads(cool_pdf_raw)['id']
        _, _, cool_html = self.page(f'/zh/w/{cool_wid}')
        self.assertNotIn('citation_pdf_url', cool_html)
        code, cool_headers, cool_body = self.call(f'/w/{cool_wid}/pdf/{cool_pdf_id}')
        self.assertEqual(code, 200)
        self.assertEqual(cool_body, pdf1_bytes)
        self.assertEqual(cool_headers.get('X-Robots-Tag'), 'noindex')

        # 6. 404 cases: wrong work_id/attachment combination, non-PDF attachment, non-%PDF- bytes, unknown UUIDs
        other_uuid = '00000000-0000-4000-8000-000000000000'
        # Corrupt a PDF blob in DB so its bytes no longer start with %PDF-
        bad_pdf_bytes = b'%PDF-1.4\nwill-be-replaced'
        _, _, bad_pdf_raw = self.call(
            '/api/research/upload',
            {'revision_id': self.second, 'filename': 'corrupt_header.pdf', 'data_b64': base64.b64encode(bad_pdf_bytes).decode()},
            self.token,
        )
        bad_pdf_id = json.loads(bad_pdf_raw)['id']
        store = Store(db_path)
        try:
            res = Research(str(db_path) + '.research.sqlite', store.rows())
            try:
                fake_bytes = b'NOTPDF-data'
                fake_sha = res.blobstore.put(fake_bytes)
                res.db.execute('UPDATE research_files SET sha256=?, size=? WHERE id=?', (fake_sha, len(fake_bytes), bad_pdf_id))
            finally:
                res.close()
        finally:
            store.close()

        for bad_route in (
            f'/w/{self.work}/pdf/{csv_id}',             # non-PDF attachment (.csv)
            f'/w/{self.work}/pdf/{cool_pdf_id}',        # attachment belongs to another work
            f'/w/{cool_wid}/pdf/{pdf1_id}',             # attachment belongs to another work
            f'/w/{self.work}/pdf/{other_uuid}',         # non-existent attachment
            f'/w/{other_uuid}/pdf/{pdf1_id}',           # non-existent work
            f'/w/not-a-uuid/pdf/{pdf1_id}',             # invalid work UUID
            f'/w/{self.work}/pdf/not-a-uuid',           # invalid attachment UUID
            f'/w/{self.work}/pdf/{pdf1_id}?extra=1',    # unexpected query string
            f'/w/{self.work}/pdf/{bad_pdf_id}',         # .pdf filename but bytes do not start with %PDF-
        ):
            with self.subTest(bad_route=bad_route):
                self.assertEqual(self.call(bad_route)[0], 404)

    def test_abstract_priority_in_feed_notifications_search_and_catalog_plus_math_rendering_and_fallback(self):
        from unittest.mock import patch
        from eksmu import research_render
        from eksmu.analysis import build_report, validate_report
        from eksmu.analysis_worker import _catalog_paper

        paper_with_math = {
            'format': 'eksmu.paper-metadata.v1',
            'abstract': (
                'Abstract unique_abs_keyword with *literal_star*, _literal_under_, #literal_hash, '
                '[literal_link](https://example.org), <script>alert(9)</script>, '
                'inline $E = mc^2$ and \\(a_1\\), display $$\\sum_{k=1}^n k$$.'
            ),
            'authors': [{'name': 'Author One', 'contribution': 'Theory'}],
            'references': ['Ref 1'],
            'license': 'CC BY 4.0',
            'provenance': {'source': 'arXiv:2607.17146', 'prior_publication_date': '2026-07-01'},
        }
        code, _, raw = self.call(
            '/api/explorations',
            {
                'title': 'Math Abstract Paper',
                'body': 'Short bibliographic stub body without the abstract keyword.',
                'paper': paper_with_math,
            },
            self.token,
        )
        self.assertEqual(code, 201)
        created = json.loads(raw)
        wid = created['exploration']['id']
        rev = created['revision']

        # 1. Discovery feed uses paper.abstract for excerpt, and search matches unique_abs_keyword in abstract
        code_f, _, feed_raw = self.call('/api/community/feed?q=unique_abs_keyword&topic=&view=all&order=latest&offset=0&limit=12')
        self.assertEqual(code_f, 200)
        feed_items = json.loads(feed_raw)['items']
        matched = [it for it in feed_items if it['id'] == wid]
        self.assertEqual(len(matched), 1)
        self.assertIn('unique_abs_keyword', matched[0]['excerpt'])
        self.assertNotIn('Short bibliographic stub body', matched[0]['excerpt'])
        self.assertNotIn('<math', matched[0]['excerpt'])

        # 2. Peer follows the work; author revises with updated abstract -> notification excerpt uses abstract
        self.assertEqual(
            self.call('/api/community/mark', {'exploration_id': wid, 'kind': 'follow', 'enabled': True}, self.peer_token)[0],
            200,
        )
        paper_rev2 = dict(paper_with_math, abstract='Revised abstract notice_abs_text with $x^2 + y^2 = z^2$.')
        code_r, _, rev2_raw = self.call(
            '/api/revisions',
            {
                'exploration_id': wid,
                'parent_revision_id': rev['id'],
                'title': 'Math Abstract Paper v2',
                'body': 'Body of v2 stub',
                'paper': paper_rev2,
            },
            self.token,
        )
        self.assertEqual(code_r, 201)
        rev2 = json.loads(rev2_raw)
        code_n, _, notif_raw = self.call('/api/community/notifications', token=self.peer_token)
        self.assertEqual(code_n, 200)
        notifs = [n for n in json.loads(notif_raw)['items'] if n['revision_id'] == rev2['id']]
        self.assertEqual(len(notifs), 1)
        self.assertIn('Revised abstract notice_abs_text', notifs[0]['excerpt'])

        # 3. AI catalog prompt includes abstract
        cat_prompt = _catalog_paper(rev)
        self.assertIn('unique_abs_keyword', cat_prompt)
        self.assertIn('Short bibliographic stub body', cat_prompt)

        # 4. Work page abstract box renders inline/display math to MathML, keeps *, _, #, [], <script> as literal text,
        #    and keeps citation_abstract / og:description as plain text
        _, _, page_html = self.page(f'/en/w/{wid}?revision={rev["id"]}')
        abs_section = page_html.split('<section class="work-abstract" id="work-abstract">', 1)[1].split('</section>', 1)[0]
        self.assertIn('<math', abs_section)
        self.assertIn('xmlns="http://www.w3.org/1998/Math/MathML"', abs_section)
        self.assertIn('*literal_star*', abs_section)
        self.assertIn('_literal_under_', abs_section)
        self.assertIn('#literal_hash', abs_section)
        self.assertIn('[literal_link](https://example.org)', abs_section)
        self.assertIn('&lt;script&gt;alert(9)&lt;/script&gt;', abs_section)
        self.assertNotIn('<em>', abs_section)
        self.assertNotIn('<h1', abs_section)
        self.assertNotIn('<a ', abs_section)
        self.assertNotIn('<script>', abs_section)
        # citation_abstract and og:description remain plain text without <math>
        self.assertRegex(page_html, r'<meta name="citation_abstract" content="[^"]*unique_abs_keyword[^"]*">')
        self.assertNotRegex(page_html, r'<meta name="citation_abstract" content="[^"]*<math')
        self.assertRegex(page_html, r'<meta property="og:description" content="[^"]*unique_abs_keyword[^"]*">')

        # 5. Cache hit and fallback when pandoc is missing or fails
        with research_render._ABSTRACT_CACHE_LOCK:
            research_render._ABSTRACT_CACHE.clear()
        sample_abs = 'Cache test *plain* with $\\alpha + \\beta$.'
        first_res = research_render.render_abstract(sample_abs)
        self.assertEqual(first_res['mode'], 'formatted')
        self.assertIn('<math', first_res['html'])
        with patch.object(research_render.subprocess, 'run', side_effect=AssertionError('should hit cache')):
            cached_res = research_render.render_abstract(sample_abs)
        self.assertEqual(cached_res, first_res)

        # Fallback when pandoc is unavailable or returns non-zero
        with patch.object(research_render.shutil, 'which', return_value=None):
            fb_none = research_render.render_abstract('Fallback $x_1$ & <tag>')
        self.assertEqual(fb_none['mode'], 'plain')
        self.assertEqual(fb_none['html'], '<p>Fallback $x_1$ &amp; &lt;tag&gt;</p>')

        with patch.object(research_render. subprocess, 'run', side_effect=OSError('pandoc failed')):
            fb_err = research_render.render_abstract('Uncached error $y_2$ <x>')
        self.assertEqual(fb_err['mode'], 'plain')
        self.assertEqual(fb_err['html'], '<p>Uncached error $y_2$ &lt;x&gt;</p>')

        # 6. Backward compatibility with legacy analysis reports (without scope.inputs)
        legacy_rep = build_report(
            rev,
            {
                'kind': 'model',
                'name': 'claude-haiku-5-5',
                'requested_model': 'claude-haiku-5-5',
                'prompt_template': 'eksmu-analysis-v2',
                'operator': 'Eksmu operator',
                'cost_borne_by': 'Eksmu operator',
            },
            {
                'summary': 'Legacy summary',
                'claims': [],
                'strengths': [],
                'findings': [],
                'questions': [],
                'suggestions': [],
                'overall': 'Overall',
                'not_checked': [],
                'limitations': '',
                'suspicious_instructions': False,
            },
            {'input_tokens': 10, 'output_tokens': 10, 'cost_usd': 0.001},
            manuscript=None,
        )
        self.assertNotIn('inputs', legacy_rep['scope'])
        self.assertEqual(validate_report(legacy_rep)['id'], legacy_rep['id'])


if __name__ == '__main__':
    unittest.main()


class RenderTableTests(unittest.TestCase):
    def test_wide_tables_do_not_leak_colgroup_markup(self):
        from eksmu.research_render import sanitize
        html = sanitize('<table><colgroup><col style="width: 16%" /><col style="width: 84%" /></colgroup>'
                        '<thead><tr><th>a</th><th>b</th></tr></thead><tbody><tr><td>1</td><td>2</td></tr></tbody></table>')
        self.assertNotIn('colgroup', html)
        self.assertNotIn('&lt;col', html)
        self.assertIn('<th>a</th>', html)
        self.assertIn('<td>2</td>', html)


class NotFoundPageTests(unittest.TestCase):
    def test_unknown_pages_get_the_html_404_but_api_keeps_json(self):
        import tempfile, threading, urllib.request, urllib.error
        from eksmu.server import make_server
        with tempfile.TemporaryDirectory() as d:
            server = make_server(str(Path(d) / 'c.sqlite'), 0)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                base = f'http://127.0.0.1:{server.server_port}'
                for path, lang in (('/zh/no-such-page', 'zh-CN'), ('/en/no-such-page', 'en')):
                    with self.assertRaises(urllib.error.HTTPError) as ctx:
                        urllib.request.urlopen(base + path)
                    self.assertEqual(ctx.exception.code, 404)
                    self.assertIn('text/html', ctx.exception.headers['Content-Type'])
                    self.assertIn(f'lang="{lang}"', ctx.exception.read().decode())
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(base + '/api/no-such-endpoint')
                self.assertEqual(ctx.exception.code, 404)
                self.assertIn('application/json', ctx.exception.headers['Content-Type'])
            finally:
                server.shutdown(); server.server_close()

