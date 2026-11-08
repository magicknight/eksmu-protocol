"""Record hash chain, record proofs, OpenTimestamps anchoring and their HTTP surface."""
from datetime import datetime, timedelta, timezone
import hashlib
import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from eksmu import record_chain
from eksmu.server import make_server
from eksmu.store import Problem, Store, canonical, digest

try:
    import opentimestamps  # noqa: F401  (optional: requirements-anchor.txt)
    HAVE_OTS = True
except ImportError:
    HAVE_OTS = False


def proof_bytes(head, height=None):
    """A real .ots structure for head's stamped file, pending or attested in a block."""
    from opentimestamps.core.notary import BitcoinBlockHeaderAttestation, PendingAttestation
    from opentimestamps.core.op import OpSHA256
    from opentimestamps.core.serialize import BytesSerializationContext
    from opentimestamps.core.timestamp import DetachedTimestampFile, Timestamp
    stamp = Timestamp(hashlib.sha256(bytes.fromhex(head)).digest())
    stamp.attestations.add(BitcoinBlockHeaderAttestation(height) if height else
                           PendingAttestation('https://a.pool.opentimestamps.org'))
    out = BytesSerializationContext()
    DetachedTimestampFile(OpSHA256(), stamp).serialize(out)
    return out.getbytes()


class ChainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'records.sqlite'
        self.store = Store(self.path); self.addCleanup(lambda: self.store.close())
        self.key = self.store.register('作者')['token']
        self.work = self.store.publish(self.key, '第一版正文', '标题')

    def test_every_record_is_chained_in_order(self):
        rows = self.store.rows()
        head = record_chain.GENESIS
        for row in rows:
            head = record_chain.fold(head, row['sha256'])
        result = record_chain.verify(self.store.db)
        self.assertEqual((result['length'], result['head']), (len(rows), head))
        self.assertEqual(record_chain.tip(self.store.db), (len(rows), head))
        self.assertEqual(record_chain.status(self.store.db)['backfilled_entries'], 0)

    def test_fold_vector(self):
        self.assertEqual(record_chain.fold('00' * 32, '00' * 32),
                         hashlib.sha256(b'eksmu.record-chain.v1\x00' + bytes(64)).hexdigest())

    def test_edits_reordering_and_chain_rewrites_are_detected(self):
        revision = self.work['revision']
        changed = dict(revision, payload=dict(revision['payload'], body='改写过的正文'))
        changed['sha256'] = digest({k: v for k, v in changed.items() if k != 'sha256'})
        with self.store.db:
            self.store.db.execute('UPDATE records SET document=? WHERE id=?', (canonical(changed), revision['id']))
        with self.assertRaises(Problem):
            record_chain.verify(self.store.db)
        with self.store.db:
            self.store.db.execute('UPDATE records SET document=? WHERE id=?', (canonical(revision), revision['id']))
            self.store.db.execute('UPDATE record_chain SET head=? WHERE seq=1', ('ab' * 32,))
        with self.assertRaises(Problem):
            record_chain.verify(self.store.db)

    def test_anchor_must_match_chain(self):
        seq, head = record_chain.tip(self.store.db)
        with self.store.db:
            self.store.db.execute("INSERT INTO chain_anchors(seq,head,method,submitted_at,state,proof,updated_at) "
                                  "VALUES(?,?,'opentimestamps','t','pending',x'00','t')", (seq, 'cd' * 32))
        with self.assertRaises(Problem):
            record_chain.verify(self.store.db)

    def test_older_databases_are_backfilled_and_marked(self):
        self.store.close()
        db = sqlite3.connect(self.path)
        db.execute('DROP TABLE record_chain'); db.commit(); db.close()
        self.store = Store(self.path)
        status = record_chain.status(self.store.db)
        self.assertEqual(status['length'], len(self.store.rows()))
        self.assertEqual(status['backfilled_entries'], status['length'])
        record_chain.verify(self.store.db)
        self.store.publish(self.key, '新作品', '新')
        self.assertEqual(record_chain.status(self.store.db)['backfilled_entries'], status['length'])
        record_chain.verify(self.store.db)

    def test_import_builds_the_chain(self):
        bundle = self.store.export()
        other = Store(Path(self.tmp.name) / 'copy.sqlite'); self.addCleanup(other.close)
        other.import_bundle(bundle)
        self.assertEqual(record_chain.verify(other.db)['head'], record_chain.verify(self.store.db)['head'])

    def test_record_proof_folds_to_the_anchored_head(self):
        revision = self.work['revision']
        self.store.publish(self.key, '之后的作品', '之后')
        seq, head = record_chain.tip(self.store.db)
        with self.store.db:
            self.store.db.execute("INSERT INTO chain_anchors(seq,head,method,submitted_at,state,proof,updated_at) "
                                  "VALUES(?,?,'opentimestamps','2026-10-08T00:00:00+00:00','pending',x'00','t')", (seq, head))
        proof = record_chain.record_proof(self.store.db, revision['id'])
        record = proof['record']
        self.assertEqual(digest({k: v for k, v in record.items() if k != 'sha256'}), record['sha256'])
        folded = record_chain.fold(proof['previous_head'], record['sha256'])
        for value in proof['following_record_sha256']:
            folded = record_chain.fold(folded, value)
        self.assertEqual(folded, proof['anchor']['head'])
        actor = self.store.rows()[0]
        self.assertEqual(actor['type'], 'actor')
        with self.assertRaises(Problem) as hidden:
            record_chain.record_proof(self.store.db, actor['id'])
        self.assertEqual(hidden.exception.status, 404)


@unittest.skipUnless(HAVE_OTS, 'opentimestamps is optional (requirements-anchor.txt)')
class AnchorTests(unittest.TestCase):
    def setUp(self):
        from eksmu import chain_anchor
        self.anchor = chain_anchor
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'records.sqlite'
        store = Store(self.path)
        store.publish(store.register('作者')['token'], '正文', '标题')
        self.seq, self.head = record_chain.tip(store.db)
        store.close()
        self.sent = []

    def stamper(self, _ots, head):
        self.sent.append(head)
        return proof_bytes(head)

    def fetch_for(self, merkle_root):
        def fetch(url):
            if '/block-height/' in url:
                return b'00000000000000000001abcd'
            return json.dumps({'merkle_root': merkle_root, 'timestamp': 1791432000}).encode()
        return fetch

    def test_stamp_upgrade_and_explorer_check(self):
        report = self.anchor.run(self.path, 'ots', stamper=self.stamper, fetch=self.fetch_for('x'))
        self.assertEqual(report['stamped']['head'], self.head)
        self.assertEqual(self.sent, [self.head])
        # Nothing new to anchor: no second submission.
        self.anchor.run(self.path, 'ots', stamper=self.stamper, upgrader=lambda o, h, p: p)
        self.assertEqual(self.sent, [self.head])
        root = hashlib.sha256(bytes.fromhex(self.head)).digest()[::-1].hex()
        report = self.anchor.run(self.path, 'ots', stamper=self.stamper, fetch=self.fetch_for(root),
                                 upgrader=lambda o, h, p: proof_bytes(h, 915000))
        self.assertEqual(report['upgraded'], [1])
        db = sqlite3.connect(self.path)
        status = record_chain.status(db)
        record_chain.verify(db); db.close()
        anchor = status['anchors'][0]
        self.assertEqual(anchor['state'], 'confirmed')
        self.assertEqual(anchor['bitcoin'], {'height': 915000, 'merkle_root': root})
        self.assertEqual(anchor['checked']['matched'], 2)
        self.assertTrue(anchor['checked']['block_time'].startswith('2026-10-08'))

    def test_explorer_mismatch_is_reported_not_trusted(self):
        self.anchor.run(self.path, 'ots', stamper=self.stamper)
        report = self.anchor.run(self.path, 'ots', fetch=self.fetch_for('ff' * 32),
                                 upgrader=lambda o, h, p: proof_bytes(h, 915000))
        self.assertTrue(report['errors'])
        db = sqlite3.connect(self.path)
        anchor = record_chain.status(db)['anchors'][0]; db.close()
        self.assertEqual(anchor['checked']['matched'], 0)
        self.assertTrue(anchor['checked']['mismatch'])
        self.assertIsNone(anchor['checked']['block_time'])

    def test_new_records_wait_for_the_interval(self):
        self.anchor.run(self.path, 'ots', stamper=self.stamper)
        store = Store(self.path)
        store.publish(store.register('另一位')['token'], '新正文', '新标题')
        store.close()
        self.anchor.run(self.path, 'ots', stamper=self.stamper, upgrader=lambda o, h, p: p)
        self.assertEqual(len(self.sent), 1)
        self.anchor.run(self.path, 'ots', stamper=self.stamper, upgrader=lambda o, h, p: p, min_interval=timedelta(0))
        self.assertEqual(len(self.sent), 2)

    def test_proof_for_another_head_is_rejected(self):
        with self.assertRaises(Problem):
            self.anchor.parse_proof(proof_bytes('11' * 32), self.head)


class ChainHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'local.sqlite'
        self.server = make_server(self.db, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        actor = json.loads(self.call('/api/actors', {'alias': '作者'})[2])
        self.actor, token = actor['actor']['id'], actor['token']
        created = json.loads(self.call('/api/explorations', {'title': '标题', 'body': '正文'}, token)[2])
        self.work, self.revision = created['exploration']['id'], created['revision']['id']

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.temp.cleanup()

    def call(self, path, data=None, token=None):
        headers = {'Content-Type': 'application/json', 'X-Eksmu-Client': 'local-prototype'}
        if token: headers['Authorization'] = 'Bearer ' + token
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        try:
            conn.request('POST' if data is not None else 'GET', path, None if data is None else json.dumps(data).encode(), headers)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def test_public_chain_and_proofs(self):
        code, _, body = self.call('/api/chain')
        status = json.loads(body)
        self.assertEqual((code, status['length'], status['anchors']), (200, 3, []))
        entries = json.loads(self.call('/api/chain/entries?from=2&limit=1')[2])['entries']
        self.assertEqual([e['seq'] for e in entries], [2])
        code, headers, body = self.call(f'/api/chain/proof?record={self.revision}')
        self.assertEqual(code, 200)
        self.assertIn('attachment', headers['Content-Disposition'])
        self.assertEqual(json.loads(body)['record']['id'], self.revision)
        self.assertEqual(self.call(f'/api/chain/proof?record={self.actor}')[0], 404)
        self.assertEqual(self.call('/api/chain/proof?record=nope')[0], 400)
        self.assertEqual(self.call('/api/chain?x=1')[0], 400)
        self.assertEqual(self.call('/api/chain/anchor-proof?id=1')[0], 404)
        self.assertEqual(self.call('/api/chain/entries?from=0')[0], 400)

    def test_anchor_files_and_work_page(self):
        db = sqlite3.connect(self.db)
        seq, head = record_chain.tip(db)
        db.execute("INSERT INTO chain_anchors(seq,head,method,submitted_at,state,proof,bitcoin,checked,updated_at) "
                   "VALUES(?,?,'opentimestamps','2026-10-08T03:00:00+00:00','confirmed',x'0102',?,?,'t')",
                   (seq, head, json.dumps({'height': 915000, 'merkle_root': 'aa'}),
                    json.dumps({'matched': 2, 'block_time': '2026-10-08T04:12:00+00:00'})))
        db.commit(); db.close()
        code, headers, body = self.call('/api/chain/anchor-file?id=1')
        self.assertEqual((code, body), (200, bytes.fromhex(head)))
        self.assertEqual(self.call('/api/chain/anchor-proof?id=1')[2], b'\x01\x02')
        page = self.call(f'/zh/w/{self.work}')[2].decode()
        self.assertIn('比特币区块 #915000', page)
        self.assertIn('2026-10-08 04:12 UTC', page)
        self.assertIn('已与 2 个公开区块浏览器核对', page)
        english = self.call(f'/en/w/{self.work}')[2].decode()
        self.assertIn('anchored in Bitcoin block #915000', english)

    def test_work_page_before_any_anchor(self):
        page = self.call(f'/zh/w/{self.work}')[2].decode()
        self.assertIn('本版正文是本站记录链的第 3 条记录（记录链按顺序收录账户、作品、版本与讨论等所有公开记录）。', page)
        self.assertIn('本站第 1 篇作品', page)
        self.assertNotIn('OpenTimestamps 证明', page)
        en_page = self.call(f'/en/w/{self.work}')[2].decode()
        self.assertIn("This version’s body is record #3 in this site’s record chain", en_page)
        self.assertIn('Work #1 on this site', en_page)


if __name__ == '__main__':
    unittest.main()
