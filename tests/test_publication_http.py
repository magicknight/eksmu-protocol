import base64
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
import uuid

from eksmu.server import make_server


class PublicationHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = make_server(Path(self.temp.name) / 'community.sqlite', 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.a = self.req('/api/actors', {'alias':'甲'})[2]
        self.b = self.req('/api/actors', {'alias':'乙'})[2]
        self.work = self.req('/api/explorations', {'body':'演示正文'}, self.a['token'])[2]
        self.rid = self.work['revision']['id']

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(); self.temp.cleanup()

    def req(self, path, data=None, token=None, headers=None):
        h = {'Content-Type':'application/json', 'X-Eksmu-Client':'local-prototype'}
        if token: h['Authorization'] = 'Bearer ' + token
        h.update(headers or {})
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            conn.request('GET' if data is None else 'POST', path, None if data is None else json.dumps(data), h)
            response = conn.getresponse(); raw = response.read(); code = response.status; hs = dict(response.getheaders())
            return code, hs, json.loads(raw) if hs.get('Content-Type','').startswith('application/json') else raw
        finally: conn.close()

    def args(self):
        return {'revision_id':self.rid, 'attachment_ids':[], 'expected_previous_id':None, 'operation_id':str(uuid.uuid4())}

    def test_snapshot_and_file_roundtrip_with_explicit_selection(self):
        file = self.req('/api/research/upload', {'revision_id':self.rid, 'filename':'a.txt',
            'data_b64':base64.b64encode(b'original').decode()}, self.a['token'])[2]
        args = self.args(); args['attachment_ids'] = [file['id']]
        code, _, snap = self.req('/api/publications/snapshot', args, self.a['token']); self.assertEqual(code, 200)
        self.assertEqual(self.req('/api/publications/snapshot', args, self.a['token'])[2], snap)
        code, headers, raw = self.req('/api/publications/file?snapshot_id='+snap['id']+'&attachment_id='+file['id'])
        self.assertEqual(code, 200); self.assertEqual(raw, b'original'); self.assertIn('attachment;', headers['Content-Disposition'])
        code, headers, package = self.req('/api/publications/export?snapshot_id='+snap['id'])
        self.assertEqual(code, 200); self.assertEqual(package['snapshot'], snap); self.assertIn('attachment;', headers['Content-Disposition'])
        self.assertEqual(self.req('/api/research/materials?revision_id='+self.rid)[2]['items'][0]['id'], file['id'])

    def test_guest_and_nonowner_cannot_change_publication_status(self):
        for token, expected in ((None, 401), (self.b['token'], 403)):
            self.assertEqual(self.req('/api/publications/snapshot', self.args(), token)[0], expected)
            args = {'revision_id':self.rid,'kind':'withdraw','body':'test','expected_previous_id':None,'operation_id':str(uuid.uuid4())}
            self.assertEqual(self.req('/api/publications/event', args, token)[0], expected)

    def test_withdrawal_is_visible_on_old_snapshot_without_changing_package(self):
        snap = self.req('/api/publications/snapshot', self.args(), self.a['token'])[2]
        before = self.req('/api/publications/export?snapshot_id='+snap['id'])[2]
        args = {'revision_id':self.rid,'kind':'withdraw','body':'作者发现错误','expected_previous_id':None,'operation_id':str(uuid.uuid4())}
        self.assertEqual(self.req('/api/publications/event', args, self.a['token'])[0], 200)
        result = self.req('/api/publications/snapshot?snapshot_id='+snap['id'])[2]
        self.assertTrue(result['status']['withdrawn']); self.assertEqual(result['snapshot'], snap)
        self.assertEqual(self.req('/api/publications/export?snapshot_id='+snap['id'])[2], before)
        self.assertEqual(self.req('/api/comments', {'revision_id':self.rid,'body':'仍可讨论'}, self.b['token'])[0], 201)

    def test_time_sources_do_not_promote_private_records_to_external_proof(self):
        from eksmu.time_anchor import DisabledAnchor
        from eksmu.store import Problem
        snap = self.req('/api/publications/snapshot', self.args(), self.a['token'])[2]
        result = self.req('/api/publications/snapshot?snapshot_id='+snap['id'])[2]
        times = result['time_sources']
        self.assertIsNone(times['author_declaration'])
        self.assertEqual(times['platform_record']['revision_created_at'], snap['revision']['created_at'])
        self.assertEqual(times['platform_record']['snapshot_created_at'], snap['created_at'])
        self.assertEqual(times['platform_record']['clock'], 'instance')
        self.assertEqual(times['public_publication_event'], {'state':'not_recorded_by_instance','evidence':None})
        self.assertEqual(times['local_commitment'], {'state':'offline_optional','saved_by_instance':False})
        self.assertEqual(times['external_anchor']['state'], 'disabled')
        self.assertIsNone(times['external_anchor']['proof'])
        self.assertFalse(times['external_anchor']['submission_available'])
        with self.assertRaises(Problem) as blocked:
            DisabledAnchor().submit('0'*64)
        self.assertEqual(blocked.exception.status, 503)
        self.assertEqual(self.req('/api/publications/anchor', {'commitment_sha256':'0'*64}, self.a['token'])[0], 404)

    def test_author_claimed_prior_date_is_separate_from_instance_clock(self):
        paper = {'format':'eksmu.paper-metadata.v1','abstract':'','authors':[], 'references':[], 'license':'',
                 'provenance':{'source':'作者自述来源','prior_publication_date':'2020-02-29'}}
        work = self.req('/api/explorations', {'body':'演示论文','paper':paper}, self.a['token'])[2]
        args = {'revision_id':work['revision']['id'],'attachment_ids':[],
                'expected_previous_id':None,'operation_id':str(uuid.uuid4())}
        snap = self.req('/api/publications/snapshot', args, self.a['token'])[2]
        times = self.req('/api/publications/snapshot?snapshot_id='+snap['id'])[2]['time_sources']
        self.assertEqual(times['author_declaration'], paper['provenance'])
        self.assertNotEqual(times['platform_record']['snapshot_created_at'], '2020-02-29')

    def test_snapshot_landing_user_navigation_keeps_api_cross_site_guard(self):
        snap = self.req('/api/publications/snapshot', self.args(), self.a['token'])[2]
        headers = {'Sec-Fetch-Site':'cross-site','Sec-Fetch-Mode':'navigate','Sec-Fetch-Dest':'document','Sec-Fetch-User':'?1'}
        code, _, html = self.req('/?snapshot='+snap['id'], headers=headers)
        self.assertEqual(code, 200); self.assertIn(b'/publication-view.js', html)
        self.assertEqual(self.req('/api/publications/export?snapshot_id='+snap['id'], headers=headers)[0], 403)
        self.assertEqual(self.req('/?snapshot='+snap['id'], headers={'Sec-Fetch-Site':'cross-site'})[0], 403)
        self.assertEqual(self.req('/?snapshot=not-an-id')[0], 400)

    def test_snapshot_write_guard_and_stale_actor_header(self):
        for headers, status in (({'X-Eksmu-Client':''},403),({'Origin':'https://example.invalid'},403),
                                ({'X-Eksmu-Actor':self.b['actor']['id']},409)):
            self.assertEqual(self.req('/api/publications/snapshot', self.args(), self.a['token'], headers)[0], status)
        self.assertEqual(self.req('/api/publications/workspace?revision_id='+self.rid)[2]['snapshots'], [])

    def test_invalid_route_query_or_schema_does_not_mutate_records(self):
        before = self.req('/api/export')[2]
        bad = self.args(); bad['automatic_approval'] = True
        self.assertEqual(self.req('/api/publications/snapshot', bad, self.a['token'])[0], 400)
        self.assertEqual(self.req('/api/publications/workspace?revision_id='+self.rid+'&revision_id='+self.rid)[0], 400)
        self.assertEqual(self.req('/api/publications/unknown')[0], 404)
        self.assertEqual(self.req('/api/export')[2], before)


if __name__ == '__main__': unittest.main()
