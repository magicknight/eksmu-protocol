"""AI public analysis: request rules, report sealing, responses, worker, mirror and HTTP."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
import importlib.util

from eksmu.analysis import Analysis, build_report, quote_found, validate_report
from eksmu.mirror import verify_mirror, write_local
from eksmu.server import make_server
from eksmu.store import Problem, Store, canonical

BODY = '## 2. Model\n\nWe assume the lattice spacing a is constant.\n\nTherefore the energy is conserved (eq. 3).'
RAW = {'findings': [
    {'section': '2', 'quote': 'Therefore the energy is  conserved', 'category': 'derivation_gap', 'severity': 'major',
     'confidence': 'medium', 'statement': 'Conservation does not follow from a constant lattice spacing alone.'},
    {'section': '3', 'quote': 'a sentence that is not in the paper', 'category': 'made_up', 'severity': 'odd',
     'confidence': 'certain', 'statement': 'Invented citation.'},
], 'not_checked': ['numerics'], 'limitations': '', 'suspicious_instructions': False}
ANALYZER = {'kind': 'example', 'name': 'test', 'requested_model': 'test', 'prompt_template': 't',
            'operator': 'test', 'cost_borne_by': 'nobody'}
USAGE = {'input_tokens': 1, 'output_tokens': 1, 'cost_usd': 0.0}


class FakeStream:
    def __init__(self, message): self.message = message
    def __enter__(self): return self
    def __exit__(self, *_): return False
    def get_final_message(self): return self.message


class FakeClient:
    def __init__(self, stop='end_turn', raw=RAW):
        usage = SimpleNamespace(input_tokens=1000, output_tokens=2000, cache_read_input_tokens=0, cache_creation_input_tokens=0)
        self.message = SimpleNamespace(stop_reason=stop, stop_details=SimpleNamespace(category='test') if stop == 'refusal' else None,
                                       model='claude-opus-5-5', usage=usage,
                                       content=[SimpleNamespace(type='text', text=json.dumps(raw))])
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self.beta_stream))
        self.messages = SimpleNamespace(stream=self.stream)

    def beta_stream(self, **kwargs):
        self.calls.append(dict(kwargs, beta=True))
        return FakeStream(self.message)

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        return FakeStream(self.message)


class FakeOpenAI:
    def __init__(self, content=json.dumps(RAW), finish='stop', refusal=None):
        usage = SimpleNamespace(prompt_tokens=1000, completion_tokens=2000, prompt_tokens_details=SimpleNamespace(cached_tokens=200))
        message = SimpleNamespace(content=content, refusal=refusal)
        self.response = SimpleNamespace(model='served-model', usage=usage,
                                        choices=[SimpleNamespace(message=message, finish_reason=finish)])
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.db = self.root / 'c.sqlite'
        self.store = Store(self.db)
        self.a = self.store.register('作者'); self.b = self.store.register('读者')
        self.work = self.store.publish(self.a['token'], BODY, 'A model')
        self.rev = self.work['revision']
        self.aid, self.bid = self.a['actor']['id'], self.b['actor']['id']

    def tearDown(self):
        self.store.close(); self.temp.cleanup()

    def analysis(self):
        return Analysis(str(self.db) + '.analysis.sqlite', self.store.rows())

    def test_quote_check_and_sanitized_report(self):
        self.assertTrue(quote_found('energy is   CONSERVED', BODY))
        self.assertFalse(quote_found('', BODY))
        report = build_report(self.rev, ANALYZER, RAW, USAGE)
        first, second = report['findings']
        self.assertEqual((first['id'], first['quote_verified']), ('F1', True))
        self.assertEqual((second['quote_verified'], second['category'], second['severity'], second['confidence']),
                         (False, 'other', 'note', 'low'))
        self.assertEqual(report['revision_sha256'], self.rev['sha256'])
        self.assertNotIn('score', canonical(report))
        tampered = dict(report, findings=[dict(first, severity='blocking'), second])
        with self.assertRaises(Problem):
            validate_report(tampered)

    def test_only_author_requests_once_and_daily_limit(self):
        work = self.analysis()
        try:
            with self.assertRaises(Problem) as ctx:
                work.request(self.bid, self.rev['id'])
            self.assertEqual(ctx.exception.status, 403)
            self.assertEqual(work.request(self.aid, self.rev['id'])['state'], 'queued')
            with self.assertRaises(Problem) as ctx:
                work.request(self.aid, self.rev['id'])
            self.assertEqual(ctx.exception.status, 409)
        finally:
            work.close()
        revs = [self.store.publish(self.a['token'], f'body {n}', f'title {n}')['revision'] for n in range(3)]
        work = self.analysis()
        try:
            work.request(self.aid, revs[0]['id']); work.request(self.aid, revs[1]['id'])
            with self.assertRaises(Problem) as ctx:
                work.request(self.aid, revs[2]['id'])
            self.assertEqual(ctx.exception.status, 429)
        finally:
            work.close()

    def run_worker(self, client, budget=10.0, **kw):
        from eksmu.analysis_worker import run_queue
        return run_queue(self.db, None, budget_usd=budget, client=client, **kw)

    @unittest.skipUnless(importlib.util.find_spec('anthropic'), 'requirements-analysis.txt not installed')
    def test_worker_writes_report_and_records_spend(self):
        work = self.analysis(); work.request(self.aid, self.rev['id']); work.close()
        client = FakeClient()
        result = self.run_worker(client)
        self.assertEqual(result['processed'][0]['state'], 'done')
        self.assertAlmostEqual(result['month_spent_usd'], round(1000 * 0.10e-6 + 2000 * 0.50e-6, 4), places=6)
        call = client.calls[0]
        self.assertEqual(call['model'], 'claude-haiku-5-5')
        self.assertNotIn('fallbacks', call)   # Haiku 5.5 has no server-side fallback
        self.assertIn('<paper>', call['messages'][0]['content'])
        self.assertIn('untrusted', call['system'])
        work = self.analysis()
        try:
            view = work.view(self.rev['id'])
            self.assertEqual(view['request']['state'], 'done')
            self.assertEqual(view['reports'][0]['analyzer']['cost_borne_by'], 'Eksmu operator')
            response = work.respond(self.aid, view['reports'][0]['id'], 'F1', 'dispute', 'Eq. 3 uses the symmetry in §1.')
            self.assertTrue(response['is_author'])
            other = work.respond(self.bid, view['reports'][0]['id'], 'F2', 'accept', 'Agree.')
            self.assertFalse(other['is_author'])
            with self.assertRaises(Problem):
                work.respond(self.bid, view['reports'][0]['id'], 'F9', 'accept', 'x')
            foreign = self.store.publish(self.b['token'], 'other', 'other')['revision']
        finally:
            work.close()
        work = self.analysis()
        try:
            with self.assertRaises(Problem):
                work.respond(self.aid, view['reports'][0]['id'], 'F1', 'revised', 'see', foreign['id'])
        finally:
            work.close()
        out = self.root / 'mirror'
        report = write_local(self.store, self.db, out)
        self.assertEqual(report['analysis'], {'reports': 1, 'responses': 2})
        # The independent public verifier (tools/eksmu_verify.py, mirrored in eksmu-protocol) must agree.
        import importlib.util as iu
        spec = iu.spec_from_file_location('eksmu_verify', Path(__file__).resolve().parents[1] / 'tools' / 'eksmu_verify.py')
        ev = iu.module_from_spec(spec); spec.loader.exec_module(ev)
        public = ev.verify(out)
        self.assertEqual((public['chain']['head'], public['analysis']['reports'], public['analysis']['quotes_not_found']),
                         (report['chain']['head'], 1, 1))
        manifest = json.loads((out / 'manifest.json').read_text())
        manifest['analysis']['reports'][0]['findings'][0]['statement'] = 'softened'
        (out / 'manifest.json').write_text(canonical(manifest))
        with self.assertRaises(Problem):
            verify_mirror(out)

    @unittest.skipUnless(importlib.util.find_spec('anthropic'), 'requirements-analysis.txt not installed')
    def test_budget_refusal_and_bad_output(self):
        work = self.analysis(); work.request(self.aid, self.rev['id']); work.close()
        result = self.run_worker(FakeClient(), budget=0.001)
        self.assertEqual(result['processed'][0]['detail'], 'budget')
        self.assertEqual(self.analysis().view(self.rev['id'])['request']['state'], 'queued')
        result = self.run_worker(FakeClient(stop='refusal'))
        self.assertEqual(result['processed'][0]['state'], 'declined')
        rev2 = self.store.publish(self.a['token'], 'x', 'y')['revision']
        work = self.analysis(); work.request(self.aid, rev2['id']); work.close()
        client = FakeClient(); client.message.content = [SimpleNamespace(type='text', text='not json')]
        self.assertEqual(self.run_worker(client)['processed'][0]['state'], 'failed')
        rev3 = self.store.publish(self.a['token'], 'z', 'w')['revision']
        work = self.analysis(); work.request(self.aid, rev3['id']); work.close()
        client = FakeClient()
        self.assertEqual(self.run_worker(client, model='claude-opus-5-5')['processed'][0]['state'], 'done')
        self.assertEqual((client.calls[0]['fallbacks'], client.calls[0]['beta']), ('default', True))


    @unittest.skipUnless(importlib.util.find_spec('openai'), 'requirements-analysis.txt not installed')
    def test_openai_compatible_providers(self):
        from eksmu.analysis_worker import JSON_HINT
        work = self.analysis(); work.request(self.aid, self.rev['id']); work.close()
        luna = FakeOpenAI()
        result = self.run_worker(luna, model='gpt-6-luna')
        self.assertEqual(result['processed'][0]['state'], 'done')
        call = luna.calls[0]
        self.assertEqual((call['model'], call['response_format']['type'], call['response_format']['json_schema']['strict'],
                          call['reasoning_effort']), ('gpt-6-luna', 'json_schema', True, 'high'))
        self.assertNotIn(JSON_HINT, call['messages'][0]['content'])
        # 800 uncached input at 0.10, 200 cached at 0.01, 2000 output at 0.50 (USD per million)
        self.assertAlmostEqual(result['processed'][0]['cost_usd'], round((800 * 0.10 + 200 * 0.01 + 2000 * 0.50) / 1e6, 4))
        view = self.analysis().view(self.rev['id'])
        self.assertEqual((view['reports'][0]['analyzer']['name'], view['service']['model']), ('served-model', 'gpt-6-luna'))
        for n, (client, state) in enumerate([(FakeOpenAI(), 'done'), (FakeOpenAI(content=''), 'failed'),
                                             (FakeOpenAI(finish='length'), 'failed'), (FakeOpenAI(refusal='no'), 'declined')]):
            rev = self.store.publish(self.a['token'], f'b{n}', f't{n}')['revision']
            work = Analysis(str(self.db) + '.analysis.sqlite', self.store.rows())
            work.request(self.aid, rev['id']) if n < 2 else None
            work.close()
            if n >= 2:   # daily limit of 3 per author: use a second author
                c = self.store.register(f'作者{n}'); rev = self.store.publish(c['token'], 'x', 'y')['revision']
                work = Analysis(str(self.db) + '.analysis.sqlite', self.store.rows()); work.request(c['actor']['id'], rev['id']); work.close()
            result = self.run_worker(client, model='deepseek-flash')
            self.assertEqual(result['processed'][0]['state'], state)
            call = client.calls[0]
            self.assertEqual((call['response_format'], call['max_tokens']), ({'type': 'json_object'}, 32000))
            self.assertIn('json', call['messages'][0]['content'])


    def test_sealed_author_key_round_trip(self):
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
        from cryptography.hazmat.primitives import serialization
        from eksmu.analysis import seal_key, unseal_key
        private = X25519PrivateKey.generate()
        public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
        sealed = seal_key('sk-author-secret-0123456789', public, 'req-1')
        self.assertNotIn(b'sk-author', sealed)
        self.assertEqual(unseal_key(sealed, private, 'req-1'), 'sk-author-secret-0123456789')
        with self.assertRaises(Exception):
            unseal_key(sealed, private, 'req-2')          # bound to its request
        with self.assertRaises(Exception):
            unseal_key(sealed, X25519PrivateKey.generate(), 'req-1')

    @unittest.skipUnless(importlib.util.find_spec('openai'), 'requirements-analysis.txt not installed')
    def test_author_key_runs_deeper_model_at_author_cost_and_is_deleted(self):
        from eksmu.analysis_worker import run_queue
        key_path = self.root / 'worker.key'
        work = self.analysis()
        try:
            with self.assertRaises(Problem) as ctx:      # worker has not published its public key yet
                work.request(self.aid, self.rev['id'], 'gpt-6-sol', 'sk-author-secret-0123456789')
            self.assertEqual(ctx.exception.status, 503)
        finally:
            work.close()
        seen = []
        def factory(model, key):
            seen.append((model, key))
            return FakeOpenAI()
        run_queue(self.db, budget_usd=10.0, client=FakeClient(), worker_key=key_path, client_factory=factory)
        self.assertEqual(oct(key_path.stat().st_mode & 0o777), '0o600')
        work = self.analysis()
        try:
            self.assertTrue(work.view(self.rev['id'])['service']['author_keys'])
            for bad in [('gpt-6-sol', 'short'), ('not-a-model', 'sk-author-secret-0123456789'), (None, 'sk-author-secret-0123456789')]:
                with self.assertRaises(Problem):
                    work.request(self.aid, self.rev['id'], *bad)
            work.request(self.aid, self.rev['id'], 'gpt-6-sol', 'sk-author-secret-0123456789')
            stored = work.db.execute('SELECT sealed FROM analysis_keys').fetchone()[0]
            self.assertNotIn(b'sk-author', bytes(stored))
        finally:
            work.close()
        raw = (Path(str(self.db) + '.analysis.sqlite')).read_bytes()
        self.assertNotIn(b'sk-author-secret', raw)
        result = run_queue(self.db, budget_usd=10.0, client=FakeClient(), worker_key=key_path, client_factory=factory)
        self.assertEqual(seen, [('gpt-6-sol', 'sk-author-secret-0123456789')])
        self.assertEqual((result['processed'][0]['payer'], result['month_spent_usd']), ('author', 0.0))
        work = self.analysis()
        try:
            self.assertEqual(work.db.execute('SELECT COUNT(*) FROM analysis_keys').fetchone()[0], 0)
            report = work.view(self.rev['id'])['reports'][0]
            self.assertEqual((report['analyzer']['requested_model'], report['analyzer']['cost_borne_by'],
                              report['analyzer']['prompt_template']), ('gpt-6-sol', 'author (own API key)', 'eksmu-analysis-v2'))
            # platform analysis still available once; then deeper analysis only with an own key
            work.request(self.aid, self.rev['id'])
        finally:
            work.close()

    def test_stale_author_keys_expire_and_v1_schema_migrates(self):
        import sqlite3
        path = str(self.db) + '.analysis.sqlite'
        old = sqlite3.connect(path)
        old.executescript('''CREATE TABLE analysis_requests(id TEXT PRIMARY KEY, revision_id TEXT NOT NULL, actor_id TEXT NOT NULL,
                created_at TEXT NOT NULL, state TEXT NOT NULL, detail TEXT NOT NULL, report_id TEXT, updated_at TEXT NOT NULL);
              CREATE TABLE analysis_reports(id TEXT PRIMARY KEY, revision_id TEXT NOT NULL, document TEXT NOT NULL);
              CREATE TABLE analysis_responses(id TEXT PRIMARY KEY, report_id TEXT NOT NULL, document TEXT NOT NULL);
              CREATE TABLE analysis_spend(month TEXT PRIMARY KEY, cost_usd REAL NOT NULL, reports INTEGER NOT NULL);
              CREATE TABLE analysis_worker(id INTEGER PRIMARY KEY CHECK (id = 1), last_run TEXT NOT NULL, model TEXT NOT NULL);''')
        old.close()
        work = self.analysis()
        try:
            self.assertEqual(work.request(self.aid, self.rev['id'])['payer'], 'platform')
            work.db.execute("INSERT INTO analysis_requests VALUES('x', ?, ?, '2026-01-01T00:00:00+00:00', 'queued', '', NULL, "
                            "'2026-01-01T00:00:00+00:00', 'gpt-6-sol', 'author')", (self.rev['id'], self.aid))
            work.db.execute("INSERT INTO analysis_keys VALUES('x', x'00')")
            work.requeue_stale()
            self.assertEqual(work.db.execute("SELECT state FROM analysis_requests WHERE id='x'").fetchone()[0], 'failed')
            self.assertEqual(work.db.execute('SELECT COUNT(*) FROM analysis_keys').fetchone()[0], 0)
        finally:
            work.close()

    def test_platform_report_once_per_version(self):
        work = self.analysis()
        try:
            req = work.request(self.aid, self.rev['id'])
            work.finish(req['id'], 'done', '', build_report(self.rev, ANALYZER, RAW, USAGE))
            with self.assertRaises(Problem) as ctx:
                work.request(self.aid, self.rev['id'])
            self.assertEqual(ctx.exception.status, 409)
        finally:
            work.close()

    def test_author_models_are_all_priced(self):
        from eksmu.analysis import AUTHOR_MODELS
        from eksmu.analysis_worker import PROVIDERS, PLATFORM_KEYS
        self.assertTrue(AUTHOR_MODELS <= set(PROVIDERS))
        self.assertTrue({m for _, m in PLATFORM_KEYS} <= set(PROVIDERS))

    @unittest.skipUnless(importlib.util.find_spec('anthropic'), 'requirements-analysis.txt not installed')
    def test_email_notification_on_analysis_completion(self):
        from eksmu.analysis_worker import run_queue
        from eksmu.modern_accounts import ModernAccounts
        ModernAccounts(self.store)
        self.store.db.execute("INSERT INTO auth_passwords VALUES(?, 'author@example.com', 'h', 1, 1)", (self.aid,))
        self.store.db.commit()
        work = self.analysis(); work.request(self.aid, self.rev['id']); work.close()
        sent = []
        result = run_queue(self.db, budget_usd=10.0, client=FakeClient(),
                           mail_sender=lambda email, purpose, code: sent.append((email, purpose, code)),
                           public_origin='https://eksmu.com')
        self.assertEqual(result['processed'][0]['state'], 'done')
        self.assertEqual(len(sent), 1)
        self.assertEqual((sent[0][0], sent[0][1]), ('author@example.com', 'analysis'))
        self.assertIn(f'https://eksmu.com/en/w/{self.rev["payload"]["exploration_id"]}?revision={self.rev["id"]}#work-analysis', sent[0][2])
        # Mail failure must never fail report completion
        rev2 = self.store.publish(self.a['token'], '第二篇', BODY)['revision']
        work = self.analysis(); work.request(self.aid, rev2['id']); work.close()
        def broken_sender(*_args, **_kwargs):
            raise RuntimeError('smtp down')
        result2 = run_queue(self.db, budget_usd=10.0, client=FakeClient(), mail_sender=broken_sender)
        self.assertEqual(result2['processed'][0]['state'], 'done')

    @unittest.skipUnless(importlib.util.find_spec('openai'), 'requirements-analysis.txt not installed')
    def test_worker_auto_catalogs_works_for_classification_and_search(self):
        from eksmu.analysis_worker import run_queue
        from eksmu.community import Community
        luna = FakeOpenAI(content=json.dumps({'topics': ['物理', '晶格模型'], 'keywords': ['lattice spacing', '能量守恒', 'energy conservation']}))
        result = run_queue(self.db, budget_usd=10.0, client=luna, model='gpt-6-luna', catalog=True)
        self.assertEqual(len(result['cataloged']), 1)
        self.assertEqual(result['cataloged'][0]['topics'], ['晶格模型', '物理'])
        hub = Community(str(self.db) + '.community.sqlite', self.store.rows())
        try:
            self.assertEqual(hub.feed(topic='物理')['total'], 1)
            self.assertEqual(hub.feed(q='能量守恒')['total'], 1)
        finally:
            hub.close()
        # Running again with no new revisions makes zero extra model calls
        calls_before = len(luna.calls)
        result2 = run_queue(self.db, budget_usd=10.0, client=luna, model='gpt-6-luna', catalog=True)
        self.assertEqual(result2['cataloged'], [])
        self.assertEqual(len(luna.calls), calls_before)

    @unittest.skipUnless(importlib.util.find_spec('openai'), 'requirements-analysis.txt not installed')
    def test_ai_promotion_triage_rules_threshold_revision_retract_and_switch(self):
        import os
        from unittest.mock import patch
        from eksmu.analysis_worker import CATALOG_JSON_HINT, CATALOG_SCHEMA, CATALOG_SYSTEM, run_queue
        from eksmu.community import Community

        # 1. Policy-as-prompt and schema checks
        self.assertIn('promotion', CATALOG_SCHEMA['properties'])
        self.assertIn('promotion', CATALOG_SCHEMA['required'])
        self.assertIn('"promotion"', CATALOG_JSON_HINT)
        for keyword in ('PRIMARY purpose', 'open-source', 'non-mainstream', 'writing quality', 'disagree', 'Never evaluate academic correctness'):
            self.assertIn(keyword, CATALOG_SYSTEM)

        # Helper to run catalog with a specific fake response on newly published works
        wid1 = self.work['exploration']['id']

        # Case A: Obvious commercial ad -> adds source='ai' pending_review, public reason concise (no evidence), label-queue has confidence/reasons/evidence
        ad_resp = {
            'topics': ['应用工具'],
            'keywords': ['translation', '翻译'],
            'promotion': {
                'likely': True,
                'confidence': 0.94,
                'reasons': ['正文主要目的为引导访问商业网站购买翻译服务', '以产品品牌署名进行营销转化'],
                'evidence': ['Click https://shop.example.com/buy for 50% off our Pro plan!'],
            },
        }
        client_ad = FakeOpenAI(content=json.dumps(ad_resp))
        res_ad = run_queue(self.db, budget_usd=10.0, client=client_ad, model='gpt-6-luna', catalog=True)
        self.assertEqual(len(res_ad['cataloged']), 1)
        self.assertEqual(res_ad['cataloged'][0]['promotion'], {'likely': True, 'confidence': 0.94})
        # Single model call for both cataloging and promotion triage
        self.assertEqual(len(client_ad.calls), 1)

        hub = Community(str(self.db) + '.community.sqlite', self.store.rows())
        try:
            lbl = hub.active_label(wid1)
            self.assertIsNotNone(lbl)
            self.assertEqual(lbl['label'], 'pending_review')
            self.assertEqual(lbl['source'], 'ai')
            self.assertIn('正文主要目的为引导访问商业网站购买翻译服务', lbl['reason'])
            # Evidence quotes must NOT be in the public label reason
            self.assertNotIn('shop.example.com', lbl['reason'])
            self.assertEqual(hub.feed(view='all')['total'], 0)
            self.assertEqual(hub.feed(view='labeled')['total'], 1)
            # label-queue shows confidence, reasons, and evidence quotes for maintainer
            q = hub.label_queue()
            self.assertEqual(len(q['pending_review']), 1)
            item = q['pending_review'][0]
            self.assertEqual(item['exploration_id'], wid1)
            self.assertAlmostEqual(item['confidence'], 0.94)
            self.assertEqual(item['reasons'], ['正文主要目的为引导访问商业网站购买翻译服务', '以产品品牌署名进行营销转化'])
            self.assertEqual(item['evidence'], ['Click https://shop.example.com/buy for 50% off our Pro plan!'])

            # Idempotency on same revision: calling auto_catalog again for same revision does not duplicate label
            lbl_id_before = lbl['id']
            hub.auto_catalog(wid1, self.rev['id'], ['应用工具'], ['translation'], promotion=ad_resp['promotion'])
            self.assertEqual(hub.active_label(wid1)['id'], lbl_id_before)
            self.assertEqual(
                hub.db.execute('SELECT COUNT(*) FROM hub_labels WHERE exploration_id=?', (wid1,)).fetchone()[0],
                1,
            )
        finally:
            hub.close()

        # Case B: Author revises work1 removing promotion -> re-cataloging with likely=false automatically retracts AI pending_review
        rev1_v2 = self.store.revise(self.a['token'], wid1, self.rev['id'], '修订后纯学术模型', '去掉外部链接后的推导正文')
        clean_resp = {
            'topics': ['物理', '晶格模型'],
            'keywords': ['lattice', '晶格'],
            'promotion': {'likely': False, 'confidence': 0.05, 'reasons': [], 'evidence': []},
        }
        run_queue(self.db, budget_usd=10.0, client=FakeOpenAI(content=json.dumps(clean_resp)), model='gpt-6-luna', catalog=True)
        hub = Community(str(self.db) + '.community.sqlite', self.store.rows())
        try:
            self.assertIsNone(hub.active_label(wid1))
            self.assertEqual(hub.feed(view='all')['total'], 1)
            self.assertEqual(hub.label_queue()['pending_review'], [])
            retracted_row = hub.db.execute(
                'SELECT state, retracted_by, retract_reason FROM hub_labels WHERE exploration_id=?', (wid1,)
            ).fetchone()
            self.assertEqual(retracted_row[0], 'retracted')
            self.assertEqual(retracted_row[1], 'ai_catalog_triage')
        finally:
            hub.close()

        # Case C: Maintainer label is NEVER retracted by AI when revised
        hub = Community(str(self.db) + '.community.sqlite', self.store.rows())
        try:
            maint_lbl = hub.label_work(wid1, 'pending_review', '维护者人工标记待复核', 'maintainer_alice', source='maintainer')
        finally:
            hub.close()
        rev1_v3 = self.store.revise(self.a['token'], wid1, rev1_v2['id'], '第三版', '第三版正文')
        run_queue(self.db, budget_usd=10.0, client=FakeOpenAI(content=json.dumps(clean_resp)), model='gpt-6-luna', catalog=True)
        hub = Community(str(self.db) + '.community.sqlite', self.store.rows())
        try:
            active_maint = hub.active_label(wid1)
            self.assertIsNotNone(active_maint)
            self.assertEqual(active_maint['id'], maint_lbl['id'])
            self.assertEqual(active_maint['source'], 'maintainer')
            # Also test commercial_promotion by maintainer is never retracted by AI
            maint_cp = hub.label_work(wid1, 'commercial_promotion', '确认为商业推广', 'maintainer_alice', source='maintainer')
        finally:
            hub.close()
        rev1_v4 = self.store.revise(self.a['token'], wid1, rev1_v3['id'], '第四版', '第四版正文')
        run_queue(self.db, budget_usd=10.0, client=FakeOpenAI(content=json.dumps(clean_resp)), model='gpt-6-luna', catalog=True)
        hub = Community(str(self.db) + '.community.sqlite', self.store.rows())
        try:
            self.assertEqual(hub.active_label(wid1)['id'], maint_cp['id'])
            hub.retract_label(maint_cp['id'], '测试清理恢复', 'maintainer_bob')
        finally:
            hub.close()

        # Case D: Normal paper citing software / open-source tool -> likely=false -> no label
        w_oss = self.store.publish(self.a['token'], '本文使用开源工具 NumPy 与自研 GPL 库计算晶格规范场。', '开源软件引用测试')['exploration']['id']
        oss_resp = {
            'topics': ['计算物理'],
            'keywords': ['NumPy', '开源软件'],
            'promotion': {'likely': False, 'confidence': 0.12, 'reasons': [], 'evidence': []},
        }
        run_queue(self.db, budget_usd=10.0, client=FakeOpenAI(content=json.dumps(oss_resp)), model='gpt-6-luna', catalog=True)
        hub = Community(str(self.db) + '.community.sqlite', self.store.rows())
        try:
            self.assertIsNone(hub.active_label(w_oss))
        finally:
            hub.close()

        # Case E: Low confidence (likely=true, confidence=0.65 < 0.8) -> recorded in promotion_triage, no label added
        w_low = self.store.publish(self.a['token'], '含可疑外链但置信度低', '低置信度测试')['exploration']['id']
        low_resp = {
            'topics': ['其他'],
            'keywords': ['link', '外链'],
            'promotion': {'likely': True, 'confidence': 0.65, 'reasons': ['存在外部链接'], 'evidence': ['https://example.com']},
        }
        run_queue(self.db, budget_usd=10.0, client=FakeOpenAI(content=json.dumps(low_resp)), model='gpt-6-luna', catalog=True)
        hub = Community(str(self.db) + '.community.sqlite', self.store.rows())
        try:
            self.assertIsNone(hub.active_label(w_low))
            self.assertEqual(hub._raw_details(w_low)['promotion_triage']['confidence'], 0.65)
        finally:
            hub.close()

        # Case F: Old response format without 'promotion' field -> no error, no label
        w_old = self.store.publish(self.a['token'], '旧响应格式兼容测试正文', '旧格式兼容')['exploration']['id']
        old_resp = {'topics': ['数学'], 'keywords': ['number theory', '数论']}
        run_queue(self.db, budget_usd=10.0, client=FakeOpenAI(content=json.dumps(old_resp)), model='gpt-6-luna', catalog=True)
        hub = Community(str(self.db) + '.community.sqlite', self.store.rows())
        try:
            self.assertIsNone(hub.active_label(w_old))
            self.assertEqual(hub.details(w_old)['topics'], ['数学'])
        finally:
            hub.close()

        # Case G: Switch disabled (via env EKSMU_AI_PROMOTION_AUTO_LABEL=0 or auto_label=False) -> records triage, does not add label
        w_off = self.store.publish(self.a['token'], '开关关闭时的广告样例', '开关测试')['exploration']['id']
        with patch.dict(os.environ, {'EKSMU_AI_PROMOTION_AUTO_LABEL': '0'}):
            run_queue(self.db, budget_usd=10.0, client=FakeOpenAI(content=json.dumps(ad_resp)), model='gpt-6-luna', catalog=True)
        hub = Community(str(self.db) + '.community.sqlite', self.store.rows())
        try:
            self.assertIsNone(hub.active_label(w_off))
            self.assertTrue(hub._raw_details(w_off)['promotion_triage']['likely'])
            self.assertAlmostEqual(hub._raw_details(w_off)['promotion_triage']['confidence'], 0.94)
        finally:
            hub.close()

        # Case H: Entire chain does not affect publication or AI referee analysis itself
        w_ad2 = self.store.publish(self.a['token'], BODY + '\nBuy now at https://ad.example.com', '带广告但请求AI分析')['revision']
        wid_ad2 = w_ad2['payload']['exploration_id']
        work = self.analysis()
        try:
            work.request(self.aid, w_ad2['id'])
        finally:
            work.close()
        # Run both analysis report (Anthropic FakeClient) and catalog (with ad promotion)
        ad_anthropic = FakeClient(raw=dict(
            RAW,
            topics=['物理'],
            keywords=['energy', '能量'],
            promotion={'likely': True, 'confidence': 0.91, 'reasons': ['含商业购买引导'], 'evidence': ['Buy now at https://ad.example.com']},
        ))
        both = run_queue(self.db, budget_usd=10.0, client=ad_anthropic, model='claude-haiku-5-5', catalog=True)
        self.assertEqual(both['processed'][0]['state'], 'done')
        work = self.analysis()
        try:
            self.assertEqual(len(work.view(w_ad2['id'])['reports']), 1)
        finally:
            work.close()
        hub = Community(str(self.db) + '.community.sqlite', self.store.rows())
        try:
            self.assertEqual(hub.active_label(wid_ad2)['label'], 'pending_review')
        finally:
            hub.close()


class AnalysisHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'local.sqlite'
        self.server = make_server(self.db, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        a = json.loads(self.call('/api/actors', {'alias': '作者'})[1]); self.token = a['token']
        b = json.loads(self.call('/api/actors', {'alias': '读者'})[1]); self.other = b['token']
        self.rev = json.loads(self.call('/api/explorations', {'title': 'T', 'body': BODY}, self.token)[1])['revision']['id']

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

    def test_report_and_responses_appear_in_author_activity(self):
        from eksmu.store import Store
        store = Store(self.db); rows = store.rows(); store.close()
        rev = next(r for r in rows if r['id'] == self.rev)
        work = Analysis(str(self.db) + '.analysis.sqlite', rows)
        req = work.request(rev['actor_id'], self.rev)
        report = build_report(rev, ANALYZER, dict(RAW, summary='概要文字'), USAGE)
        work.finish(req['id'], 'done', '', report)
        reader = next(r['id'] for r in rows if r['type'] == 'actor' and r['id'] != rev['actor_id'])
        response = work.respond(reader, report['id'], 'F1', 'dispute', '不同意')
        work.close()
        items = json.loads(self.call('/api/community/notifications', token=self.token)[1])['items']
        mine = {(i['type'], i.get('action')): i for i in items if i['type'] == 'analysis'}
        self.assertEqual(mine[('analysis', 'report')]['excerpt'], '概要文字')
        self.assertEqual(mine[('analysis', 'response')]['id'], response['id'])
        self.assertEqual(self.call('/api/community/read', {'record_ids': [report['id'], response['id']]}, self.token)[0], 200)
        items = json.loads(self.call('/api/community/notifications', token=self.token)[1])['items']
        self.assertTrue(all(i['read'] for i in items if i['type'] == 'analysis'))
        others = json.loads(self.call('/api/community/notifications', token=self.other)[1])['items']
        self.assertFalse([i for i in others if i['type'] == 'analysis'])

    def test_request_view_and_permissions(self):
        code, body = self.call(f'/api/analysis?revision_id={self.rev}')
        self.assertEqual((code, json.loads(body)['request']), (200, None))
        self.assertEqual(self.call('/api/analysis/request', {'revision_id': self.rev}, self.other)[0], 403)
        self.assertEqual(self.call('/api/analysis/request', {'revision_id': self.rev})[0], 401)
        self.assertEqual(self.call('/api/analysis/request', {'revision_id': self.rev}, self.token)[0], 200)
        view = json.loads(self.call(f'/api/analysis?revision_id={self.rev}')[1])
        self.assertEqual(view['request']['state'], 'queued')
        self.assertEqual(self.call('/api/analysis/respond', {'report_id': self.rev, 'finding_id': 'F1', 'stance': 'accept',
                                                              'body': 'x', 'revision_ref': None}, self.other)[0], 404)


if __name__ == '__main__':
    unittest.main()
