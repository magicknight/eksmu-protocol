import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid

from eksmu.server import make_server
from eksmu_receipts import core as receipts

ROOT = Path(__file__).resolve().parents[1]


class WorkerHTTPCLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.server = make_server(self.root / 'community.sqlite', 0, verification=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.actor = self.request('/api/actors', {'alias': '无机构参与者'})[1]
        self.other = self.request('/api/actors', {'alias': '其他人'})[1]
        self.rev = self.request('/api/explorations', {'body': 'private source'}, self.actor['token'])[1]['revision']
        r = {'op': 'rational', 'numerator': '1', 'denominator': '1'}
        self.task = self.request('/api/verification/prepare', {'revision_id': self.rev['id'], 'scope': 'given input',
                'problem': {'left': r, 'right': r}}, self.actor['token'])[1]['task']['id']
        self.key = self.root / 'worker.key'
        generated = self.cli('keygen', '--key', str(self.key))
        self.assertEqual(generated.returncode, 0, generated.stderr); self.public_key = generated.stdout.strip()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(); self.tmp.cleanup()

    def cli(self, *args):
        return subprocess.run([sys.executable, '-m', 'eksmu_bridge.worker', *args], cwd=ROOT,
                              capture_output=True, text=True, timeout=15)

    def request(self, path, data=None, token=None, extra=None):
        headers = {'Content-Type': 'application/json', 'X-Eksmu-Client': 'local-prototype'}
        if token: headers['Authorization'] = 'Bearer ' + token
        headers.update(extra or {})
        client = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            client.request('GET' if data is None else 'POST', path, json.dumps(data) if data is not None else None, headers)
            r = client.getresponse(); return r.status, json.loads(r.read())
        finally: client.close()

    def dispatch(self, extra=None, token=None):
        payload = {'task_id': self.task, 'operation_id': str(uuid.uuid4()),
                   'expected_public_key': self.public_key, 'consent': True, 'ttl_seconds': 3600}
        payload.update(extra or {})
        return self.request('/api/verification/dispatch', payload, token or self.actor['token'])

    def files(self):
        code, d = self.dispatch(); self.assertEqual(code, 200)
        did = d['dispatch']['id']
        code, capsule = self.request('/api/verification/capsule?dispatch_id=' + did, token=self.actor['token'])
        self.assertEqual(code, 200)
        job, out = self.root / 'job.json', self.root / 'result.json'
        job.write_bytes(receipts.canonical(capsule))
        return did, job, out

    def test_real_subprocess_http_roundtrip_without_browser(self):
        before = self.request('/api/export')[1]
        did, job, out = self.files()
        run = self.cli('run', '--job', str(job), '--key', str(self.key), '--out', str(out), '--allow-local-compute')
        self.assertEqual(run.returncode, 0, run.stderr)
        result = json.loads(out.read_bytes())
        code, record = self.request('/api/verification/collect', {'dispatch_id': did, 'submission': result}, self.actor['token'])
        self.assertEqual(code, 200); self.assertEqual(record['disposition'], 'recorded')
        self.assertEqual(record['submission']['body']['receipt']['body']['result']['status'], 'passed')
        check = self.cli('check', '--job', str(job), '--result', str(out), '--public-key', self.public_key)
        self.assertEqual(check.returncode, 0, check.stderr); self.assertTrue(json.loads(check.stdout)['replay_matches'])
        self.assertEqual(before, self.request('/api/export')[1])
        self.assertEqual(self.request('/api/explorations', {'body': '仍然自由发表'}, self.actor['token'])[0], 201)

    def test_no_worker_execution_flag_no_output(self):
        _, job, out = self.files()
        run = self.cli('run', '--job', str(job), '--key', str(self.key), '--out', str(out))
        self.assertEqual(run.returncode, 1); self.assertFalse(out.exists())

    def test_worker_does_not_overwrite_keys_or_results(self):
        original = self.key.read_bytes()
        self.assertEqual(self.cli('keygen', '--key', str(self.key)).returncode, 1)
        self.assertEqual(self.key.read_bytes(), original)
        _, job, out = self.files(); out.write_text('preserve')
        run = self.cli('run', '--job', str(job), '--key', str(self.key), '--out', str(out), '--allow-local-compute')
        self.assertEqual(run.returncode, 1); self.assertEqual(out.read_text(), 'preserve')

    def test_missing_key_is_not_generated(self):
        _, job, out = self.files(); missing = self.root / 'missing.key'
        run = self.cli('run', '--job', str(job), '--key', str(missing), '--out', str(out), '--allow-local-compute')
        self.assertEqual(run.returncode, 1); self.assertFalse(missing.exists()); self.assertFalse(out.exists())

    def test_malformed_job_creates_no_result(self):
        _, job, out = self.files(); job.write_text('{"schema": 1, "schema": 2}')
        run = self.cli('run', '--job', str(job), '--key', str(self.key), '--out', str(out), '--allow-local-compute')
        self.assertEqual(run.returncode, 1); self.assertFalse(out.exists())

    def test_worker_key_file_is_private_on_posix(self):
        if os.name == 'posix': self.assertEqual(self.key.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(self.key.read_bytes()), 32)

    def test_http_unknown_actor_or_injected_fields_rejected(self):
        self.assertEqual(self.dispatch(token=self.other['token'])[0], 403)
        self.assertEqual(self.dispatch(extra={'actor_id': self.other['actor']['id']})[0], 400)

    def test_http_consent_cannot_be_string(self):
        self.assertEqual(self.dispatch(extra={'consent': 'true'})[0], 403)

    def test_capsule_and_handoffs_require_owner_authentication(self):
        _, d = self.dispatch(); did = d['dispatch']['id']
        self.assertEqual(self.request('/api/verification/capsule?dispatch_id=' + did)[0], 401)
        self.assertEqual(self.request('/api/verification/capsule?dispatch_id=' + did, token=self.other['token'])[0], 403)
        self.assertEqual(self.request('/api/verification/handoffs?task_id=' + self.task, token=self.actor['token'])[0], 200)
        self.assertEqual(self.request('/api/verification/handoffs?task_id=' + self.task, token=self.other['token'])[0], 403)

    def test_origin_check_still_applies_to_dispatch(self):
        payload = {'task_id': self.task, 'operation_id': str(uuid.uuid4()), 'expected_public_key': self.public_key,
                   'consent': True, 'ttl_seconds': 3600}
        self.assertEqual(self.request('/api/verification/dispatch', payload, self.actor['token'],
                                     {'Origin': 'https://other.example'})[0], 403)

    def test_worker_absent_does_not_block_publication(self):
        _, d = self.dispatch()
        self.assertEqual(d['disposition'], 'ready')
        self.assertEqual(self.request('/api/explorations', {'body': '不等验证者也继续发表'}, self.actor['token'])[0], 201)

    def test_late_result_is_labelled_and_legacy_tasks_still_work(self):
        did, job, out = self.files()
        self.assertEqual(self.cli('run', '--job', str(job), '--key', str(self.key), '--out', str(out),
                                 '--allow-local-compute').returncode, 0)
        self.assertEqual(self.request('/api/verification/cancel', {'task_id': self.task}, self.actor['token'])[0], 200)
        code, saved = self.request('/api/verification/collect', {'dispatch_id': did, 'submission': json.loads(out.read_bytes())}, self.actor['token'])
        self.assertEqual(code, 200); self.assertEqual(saved['disposition'], 'late_cancelled')
        self.assertEqual(self.request('/api/verification/tasks?revision_id=' + self.rev['id'])[0], 200)

    def test_private_worker_key_and_tokens_do_not_appear_in_capsule(self):
        _, job, _ = self.files(); text = job.read_text()
        self.assertNotIn(self.actor['token'], text); self.assertNotIn(self.key.read_bytes().hex(), text)
        self.assertNotIn('private source', text)
