import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode
from eksmu.server import make_server


class CommunityHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.server = make_server(self.root / 'records.sqlite', 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.a = self.call('/api/actors', {'alias': '新人甲'})[1]
        self.b = self.call('/api/actors', {'alias': '新人乙'})[1]
        self.work = self.call('/api/explorations', {'body': '计算一个有意思的等式', 'title': '等式探索'}, self.a)[1]
        self.wid = self.work['exploration']['id']; self.rid = self.work['revision']['id']

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(); self.tmp.cleanup()

    def call(self, path, payload=None, user=None, headers=None):
        h = {'Content-Type': 'application/json', 'X-Eksmu-Client': 'local-prototype'}
        if user: h['Authorization'] = 'Bearer ' + user['token']
        h.update(headers or {})
        client = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=8)
        try:
            client.request('GET' if payload is None else 'POST', path, json.dumps(payload) if payload is not None else None, h)
            response = client.getresponse(); raw = response.read()
            return response.status, json.loads(raw) if response.getheader('Content-Type','').startswith('application/json') else raw
        finally: client.close()

    def metadata(self, user=None, **changes):
        d = {'exploration_id': self.wid, 'topics': ['数学'], 'help': '一起找反例', 'expected_version': 0}; d.update(changes)
        return self.call('/api/community/details', d, user or self.a)

    def test_private_behavior_report_and_author_response(self):
        third = self.call('/api/actors', {'alias':'第三人'})[1]
        payload = {'target_id':self.rid, 'category':'harassment', 'reason':'针对这条记录的行为反馈'}
        status, case = self.call('/api/community/behavior-report', payload, self.b)
        self.assertEqual(status, 200)
        self.assertEqual(self.call('/api/community/behavior-report', payload, self.b)[1]['id'], case['id'])
        self.assertEqual(self.call('/api/community/behavior-cases', user=third)[1]['items'], [])
        self.assertEqual(self.call('/api/community/behavior-cases', user=None)[0], 401)
        self.assertNotIn('reporter', self.call('/api/community/behavior-cases', user=self.a)[1]['items'][0])
        self.assertEqual(self.call('/api/community/behavior-response', {'case_id':case['id'], 'body':'请核对上下文'}, self.a)[0], 200)
        self.assertEqual(self.call('/api/community/behavior-response', {'case_id':case['id'], 'body':'越权'}, third)[0], 403)
        self.assertEqual(self.call('/api/community/behavior-report', {**payload, 'category':'academic_disagreement'}, self.b)[0], 400)
        self.assertEqual(self.call('/api/community/behavior-report', {**payload, 'category':[]}, self.b)[0], 400)
        self.assertEqual(self.call('/api/community/feed')[1]['total'], 1)

    def test_behavior_appeal_http_is_private_and_cannot_replay(self):
        third = self.call('/api/actors', {'alias':'第三人'})[1]
        _, case = self.call('/api/community/behavior-report',
                            {'target_id':self.rid, 'category':'spam', 'reason':'合成重复内容'}, self.b)
        payload = {'case_id':case['id'], 'body':'请复议'}
        self.assertEqual(self.call('/api/community/behavior-appeal', payload, self.b)[0], 409)
        from eksmu.community import Community
        from eksmu.store import Store
        db = self.root / 'records.sqlite'
        store = Store(db)
        try:
            hub = Community(str(db) + '.community.sqlite', store.rows())
            try: hub.review_behavior(case['id'], 'follow_up', '待核对', 'operator_one')
            finally: hub.close()
        finally: store.close()
        self.assertEqual(self.call('/api/community/behavior-appeal', payload, third)[0], 403)
        status, result = self.call('/api/community/behavior-appeal', payload, self.b)
        self.assertEqual(status, 200)
        self.assertEqual(result['state'], 'appeal_pending')
        self.assertEqual(self.call('/api/community/behavior-appeal', payload, self.b)[0], 409)
        author = self.call('/api/community/behavior-cases', user=self.a)[1]['items'][0]
        self.assertNotIn(self.b['actor']['id'], json.dumps(author, ensure_ascii=False))
        self.assertEqual(self.call('/api/community/feed')[1]['total'], 1)

    def test_behavior_report_accepts_research_file_and_event_without_publication_change(self):
        import base64
        file = self.call('/api/research/upload', {'revision_id':self.rid,'filename':'sample.txt',
            'data_b64':base64.b64encode(b'synthetic file').decode()}, self.a)[1]
        task = self.call('/api/research/create-task', {'revision_id':self.rid,'title':'复核',
            'description':'合成协作','category':'reproduce'}, self.b)[1]
        for target, reporter, owner in ((file['id'],self.b,self.a),(task['events'][0]['id'],self.a,self.b)):
            status, case = self.call('/api/community/behavior-report', {'target_id':target,
                'category':'privacy','reason':'合成行为反馈'}, reporter)
            self.assertEqual(status, 200)
            self.assertEqual(case['target_author'], owner['actor']['id'])
            owner_case = next(item for item in self.call('/api/community/behavior-cases', user=owner)[1]['items']
                              if item['id'] == case['id'])
            self.assertNotIn('reporter', owner_case)
        self.assertEqual(self.call('/api/community/behavior-report', {'target_id':task['id'],
            'category':'spam','reason':'非具体事件'}, self.a)[0], 404)
        self.assertEqual(self.call('/api/community/feed')[1]['total'], 1)

    def test_complete_two_people_community_journey(self):
        self.assertEqual(self.metadata()[0], 200)
        code, feed = self.call('/api/community/feed?' + urlencode({'q': '反例', 'topic': '数学', 'view': 'collaborate'}))
        self.assertEqual(code, 200); self.assertEqual(feed['items'][0]['id'], self.wid)
        for kind in ['follow', 'bookmark']:
            self.assertEqual(self.call('/api/community/mark', {'exploration_id': self.wid, 'kind': kind, 'enabled': True}, self.b)[0], 200)
        comment = self.call('/api/comments', {'revision_id': self.rid, 'body': '我来尝试一个反例'}, self.b)[1]
        notices = self.call('/api/community/notifications', user=self.a)[1]
        self.assertEqual(notices['items'][0]['id'], comment['id'])
        self.assertEqual(self.call('/api/community/read', {'record_ids': [comment['id']]}, self.a)[0], 200)
        self.assertEqual(self.call('/api/community/notifications', user=self.a)[1]['unread'], 0)
        self.assertEqual(self.call('/api/community/feed?view=bookmarks', user=self.b)[1]['total'], 1)
        self.assertEqual(self.call('/api/explorations', {'body': '新想法'}, self.b)[0], 201)

    def test_collaboration_events_notify_historical_audience_and_restore_read_state(self):
        third = self.call('/api/actors', {'alias':'协作丙'})[1]
        task = self.call('/api/research/create-task', {'revision_id':self.rid, 'title':'复核计算',
            'description':'请复算一个例子', 'category':'reproduce'}, self.b)[1]
        created = task['events'][0]['id']
        alice = self.call('/api/community/notifications', user=self.a)[1]
        self.assertEqual([x['id'] for x in alice['items']], [created])
        self.assertEqual(alice['items'][0]['action'], 'created')
        self.assertEqual(self.call('/api/community/notifications', user=self.b)[1]['total'], 0)
        self.assertEqual(self.call('/api/community/read', {'record_ids':[created]}, third)[0], 403)
        forged = self.call('/api/community/export', user=third)[1]
        forged['read'] = [created]
        self.assertEqual(self.call('/api/community/import', {'bundle':forged}, third)[0], 400)

        def action(user, kind, body=''):
            current = self.call('/api/research/workspace?revision_id='+self.rid)[1]['tasks'][0]
            code, value = self.call('/api/research/task-action', {'task_id':task['id'], 'action':kind,
                'body':body, 'expected_version':current['version']}, user)
            self.assertEqual(code, 200, value)
            return value['events'][-1]['id']

        joined = action(third, 'join')
        progress = action(third, 'progress', '复算得到相同结果')
        closed = action(self.b, 'close', '材料已整理')
        for user, expected in ((self.a, {created, joined, progress, closed}),
                               (self.b, {joined, progress}), (third, {closed})):
            notices = self.call('/api/community/notifications', user=user)[1]
            self.assertEqual({x['id'] for x in notices['items']}, expected)
            self.assertEqual(notices['total'], len(expected))
        self.assertEqual(self.call('/api/community/read', {'record_ids':[closed]}, third)[0], 200)
        saved = self.call('/api/community/export', user=third)[1]
        self.assertIn(closed, saved['read'])
        self.assertEqual(self.call('/api/community/import', {'bundle':saved}, third)[0], 200)
        self.assertEqual(self.call('/api/community/notifications', user=third)[1]['unread'], 0)
        action(third, 'leave')
        reopened = action(self.b, 'reopen')
        self.assertNotIn(reopened, {x['id'] for x in self.call('/api/community/notifications', user=third)[1]['items']})
        self.assertIn(closed, {x['id'] for x in self.call('/api/community/notifications', user=third)[1]['items']})

    def test_personal_endpoints_require_auth(self):
        for route in ['notifications', 'export']:
            self.assertEqual(self.call('/api/community/' + route)[0], 401)
        self.assertEqual(self.call('/api/community/mark', {'exploration_id': self.wid, 'kind': 'follow', 'enabled': True})[0], 401)

    def test_public_details_never_expose_another_persons_marks(self):
        self.call('/api/community/mark', {'exploration_id': self.wid, 'kind': 'bookmark', 'enabled': True}, self.a)
        path = '/api/community/details?exploration_id=' + self.wid
        self.assertTrue(self.call(path, user=self.a)[1]['bookmarked'])
        self.assertFalse(self.call(path, user=self.b)[1]['bookmarked'])
        self.assertFalse(self.call(path)[1]['bookmarked'])

    def test_unknown_duplicate_and_bad_pagination_rejected(self):
        for query in ['view=elite', 'limit=-1', 'limit=999', 'q=a&q=b', 'actor_id=owner', 'offset=x']:
            with self.subTest(query=query): self.assertEqual(self.call('/api/community/feed?' + query)[0], 400)
        self.assertEqual(self.call('/api/community/unknown')[0], 401)  # Unknown private route still needs auth.
        self.assertEqual(self.call('/api/community/unknown', user=self.a)[0], 404)

    def test_metadata_ownership_and_version(self):
        self.assertEqual(self.metadata(self.b)[0], 403)
        self.assertEqual(self.metadata()[0], 200)
        self.assertEqual(self.metadata(help='stale')[0], 409)

    def test_unknown_fields_cannot_inject_privileges(self):
        self.assertEqual(self.metadata(admin=True)[0], 400)
        self.assertEqual(self.call('/api/community/mark', {'exploration_id': self.wid, 'kind': 'follow', 'enabled': True, 'actor_id': self.a['actor']['id']}, self.b)[0], 400)

    def test_origin_checks_apply_to_social_features(self):
        self.assertEqual(self.call('/api/community/mark', {'exploration_id': self.wid, 'kind': 'follow', 'enabled': True}, self.b, {'Origin': 'https://other.example'})[0], 403)

    def test_community_service_failure_does_not_disable_posting(self):
        with patch('eksmu.community.Community.__init__', side_effect=sqlite3.OperationalError('test failure')):
            self.assertEqual(self.call('/api/community/feed')[0], 503)
            self.assertEqual(self.call('/api/explorations', {'body': '发现服务坏了也继续发表'}, self.a)[0], 201)

    def test_backup_restore_via_authenticated_api(self):
        self.metadata()
        bundle = self.call('/api/community/export', user=self.a)[1]
        self.assertEqual(self.call('/api/community/import', {'bundle': bundle}, self.b)[0], 403)
        self.assertEqual(self.call('/api/community/import', {'bundle': bundle}, self.a)[0], 200)
        self.assertEqual(self.call('/api/community/export', user=self.a)[1], bundle)

    def test_new_ui_and_script_are_served(self):
        code, page = self.call('/zh/'); self.assertEqual(code, 200)
        for value in ['发现作品', '我的动态', 'topic-filter', 'import-personal']:
            self.assertIn(value.encode(), page)
        code, js = self.call('/community.js'); self.assertEqual(code, 200)
        self.assertNotIn(b'innerHTML', js); self.assertNotIn(b'https://', js)

    def test_social_actions_never_modify_evidence_or_article_records(self):
        before = self.call('/api/export')[1]
        self.metadata()
        self.call('/api/community/mark', {'exploration_id': self.wid, 'kind': 'follow', 'enabled': True}, self.b)
        self.assertEqual(self.call('/api/export')[1], before)

    def test_label_appeal_http_endpoint_and_privacy(self):
        from eksmu.community import Community
        from eksmu.store import Store
        db = self.root / 'records.sqlite'
        store = Store(db)
        try:
            hub = Community(str(db) + '.community.sqlite', store.rows())
            try:
                lbl = hub.label_work(self.wid, 'commercial_promotion', '附带推广链接', 'operator_one')
            finally:
                hub.close()
        finally:
            store.close()

        # Public labeled feed includes the work and public reason, while default feed excludes it
        self.assertEqual(self.call('/api/community/feed?view=all')[1]['total'], 0)
        labeled = self.call('/api/community/feed?view=labeled')[1]
        self.assertEqual(labeled['total'], 1)
        self.assertEqual(labeled['items'][0]['moderation_label'], 'commercial_promotion')
        self.assertEqual(labeled['items'][0]['moderation_reason'], '附带推广链接')

        # Unauthenticated or non-author cannot submit appeal or read appeal status
        self.assertEqual(self.call('/api/community/label-appeal', {'label_id': lbl['id'], 'body': '匿名申诉'})[0], 401)
        self.assertEqual(self.call('/api/community/label-appeal', {'label_id': lbl['id'], 'body': '非作者申诉'}, self.b)[0], 403)
        self.assertEqual(self.call('/api/community/label-appeal?exploration_id=' + self.wid)[0], 401)
        self.assertEqual(self.call('/api/community/label-appeal?exploration_id=' + self.wid, user=self.b)[0], 403)

        # Author submits appeal via POST /api/community/label-appeal
        code, appeal = self.call('/api/community/label-appeal', {'label_id': lbl['id'], 'body': '正文链接为开源代码库，非商业推广。'}, self.a)
        self.assertEqual(code, 200)
        self.assertEqual(appeal['state'], 'pending')
        self.assertEqual(self.call('/api/community/label-appeal', {'label_id': lbl['id'], 'body': '第二次申诉'}, self.a)[0], 409)

        # Private appeal body is only visible to author, never to guest or self.b
        guest_details = self.call('/api/community/details?exploration_id=' + self.wid)[1]
        self.assertNotIn('label_appeal', guest_details)
        self.assertNotIn('开源代码库', json.dumps(guest_details, ensure_ascii=False))
        b_details = self.call('/api/community/details?exploration_id=' + self.wid, user=self.b)[1]
        self.assertNotIn('label_appeal', b_details)
        self.assertNotIn('开源代码库', json.dumps(b_details, ensure_ascii=False))
        a_details = self.call('/api/community/details?exploration_id=' + self.wid, user=self.a)[1]
        self.assertEqual(a_details['label_appeal']['appeal']['body'], '正文链接为开源代码库，非商业推广。')
        code, a_status = self.call('/api/community/label-appeal?exploration_id=' + self.wid, user=self.a)
        self.assertEqual(code, 200)
        self.assertEqual(a_status['appeal']['id'], appeal['id'])
