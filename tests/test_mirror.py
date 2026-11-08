"""Public mirror: export, pull over HTTP and offline verification."""
import base64
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
import uuid

from eksmu import record_chain
from eksmu.mirror import manifest, pull, verify_mirror, write_local
from eksmu.publications import Publications
from eksmu.research import Research
from eksmu.server import make_server
from eksmu.store import Problem, Store, canonical


class MirrorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.db = self.root / 'community.sqlite'
        self.store = Store(self.db)
        a = self.store.register('作者甲'); self.aid = a['actor']['id']
        self.store.register('读者乙')
        work = self.store.publish(a['token'], '正文', '标题')
        self.rev, self.wid = work['revision'], work['exploration']['id']
        self.store.comment(a['token'], self.rev['id'], '作者补充')
        research = Research(str(self.db) + '.research.sqlite', self.store.rows())
        pub = Publications(str(self.db) + '.publications.sqlite', self.store.rows())
        try:
            item = research.upload(self.aid, self.rev['id'], 'data.csv', base64.b64encode(b'n,v\n1,2\n').decode())
            self.snap = pub.create_snapshot(research, self.aid, self.rev['id'], [item['id']], None, str(uuid.uuid4()))
            pub.event(self.aid, self.rev['id'], 'withdraw', '撤回说明', None, str(uuid.uuid4()))
        finally:
            pub.close(); research.close()

    def tearDown(self):
        self.store.close(); self.temp.cleanup()

    def export(self):
        out = self.root / ('mirror-' + uuid.uuid4().hex)
        return out, write_local(self.store, self.db, out)

    def rewrite(self, out, change):
        path = out / 'manifest.json'
        value = json.loads(path.read_text())
        change(value)
        path.write_text(canonical(value))

    def test_complete_mirror_verifies_and_states_its_limits(self):
        out, report = self.export()
        self.assertTrue(report['internally_consistent']); self.assertTrue(report['complete'])
        self.assertEqual(report['records'], len(self.store.rows()))
        self.assertEqual(report['chain']['head'], record_chain.tip(self.store.db)[1])
        self.assertEqual(report['snapshots'], {'listed': 1, 'verified': 1, 'missing': []})
        self.assertEqual(report['notices'], 1)
        for claim in ('authorship_signed', 'external_time_verified', 'completeness_vs_operator_known'):
            self.assertFalse(report[claim])
        self.assertEqual(verify_mirror(out), report)

    def test_manifest_has_no_private_account_data(self):
        value = manifest(self.store, self.db); value.pop('excluded')
        raw = canonical(value)
        for marker in ('@', 'password', 'session', 'recovery', 'email'):
            self.assertNotIn(marker, raw)

    def test_edited_record_is_rejected(self):
        out, _ = self.export()
        def edit(value):
            for row in value['records']['records']:
                if row['id'] == self.rev['id']:
                    row['payload']['body'] = '偷偷改过'
        self.rewrite(out, edit)
        with self.assertRaises(Problem):
            verify_mirror(out)

    def test_rehashed_edit_breaks_the_chain(self):
        out, _ = self.export()
        def edit(value):
            from eksmu.store import digest
            rows = value['records']['records']
            for row in rows:
                if row['id'] == self.rev['id']:
                    row['payload']['body'] = '偷偷改过'
                    row['sha256'] = digest({k: v for k, v in row.items() if k != 'sha256'})
            value['records']['sha256'] = digest(rows)
        self.rewrite(out, edit)
        with self.assertRaisesRegex(Problem, 'chain'):
            verify_mirror(out)

    def test_anchor_must_bind_to_a_chain_head(self):
        seq, head = record_chain.tip(self.store.db)
        with self.store.db:
            self.store.db.execute("INSERT INTO chain_anchors(seq,head,method,submitted_at,state,proof,updated_at) "
                                  "VALUES(?,?,'opentimestamps','t','pending',x'01','t')", (seq, head))
        out, report = self.export()
        self.assertEqual([(a['seq'], a['bitcoin_verified_here']) for a in report['anchors']], [(seq, False)])
        self.rewrite(out, lambda v: v['anchors'][0].update(head='ab' * 32))
        with self.assertRaisesRegex(Problem, 'anchor'):
            verify_mirror(out)

    def test_missing_or_altered_snapshot_package(self):
        out, _ = self.export()
        package = out / 'publications' / (self.snap['id'] + '.json')
        value = json.loads(package.read_text())
        value['blobs'][0]['data_b64'] = base64.b64encode(b'other').decode()
        package.write_text(canonical(value))
        with self.assertRaises(Problem):
            verify_mirror(out)
        package.unlink()
        report = verify_mirror(out)
        self.assertFalse(report['complete']); self.assertEqual(report['snapshots']['missing'], [self.snap['id']])

    def test_notice_history_cannot_be_reordered(self):
        out, _ = self.export()
        self.rewrite(out, lambda v: v['publications']['events'][0].update(previous_event_id=str(uuid.uuid4())))
        with self.assertRaises(Problem):
            verify_mirror(out)


class MirrorHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.db = self.root / 'local.sqlite'
        self.server = make_server(self.db, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        actor = json.loads(self.call('/api/actors', {'alias': '作者'})[1])
        self.call('/api/explorations', {'title': '标题', 'body': '正文'}, actor['token'])

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(); self.temp.cleanup()

    def call(self, path, data=None, token=None):
        headers = {'Content-Type': 'application/json', 'X-Eksmu-Client': 'local-prototype'}
        if token: headers['Authorization'] = 'Bearer ' + token
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        try:
            conn.request('POST' if data is not None else 'GET', path, None if data is None else json.dumps(data).encode(), headers)
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def test_public_manifest_and_pull(self):
        code, body = self.call('/api/mirror')
        value = json.loads(body)
        self.assertEqual((code, value['format'], len(value['records']['records'])), (200, 'eksmu.public-mirror.v1', 3))
        self.assertEqual(self.call('/api/mirror?x=1')[0], 400)
        report = pull(f'http://127.0.0.1:{self.server.server_port}', self.root / 'copy')
        self.assertTrue(report['internally_consistent']); self.assertEqual(report['records'], 3)

    def test_pull_refuses_plain_http_and_existing_directories(self):
        with self.assertRaises(Problem):
            pull('http://example.com', self.root / 'x')
        with self.assertRaises(Problem):
            pull('https://example.com/path', self.root / 'x')
        (self.root / 'exists').mkdir()
        with self.assertRaises(FileExistsError):
            pull(f'http://127.0.0.1:{self.server.server_port}', self.root / 'exists')


if __name__ == '__main__':
    unittest.main()
