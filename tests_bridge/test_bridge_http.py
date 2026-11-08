import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch

from eksmu.server import make_server


def rational(n):return {'op':'rational','numerator':str(n),'denominator':'1'}


class BridgeHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.start(True)
        self.author=self.request('/api/actors',{'alias':'新人'})[1]
        self.critic=self.request('/api/actors',{'alias':'另一位参与者'})[1]
        self.rev=self.request('/api/explorations',{'body':'我的猜想'},self.author['token'])[1]['revision']

    def start(self,enabled):
        self.server=make_server(self.root/'community.sqlite',0,verification=enabled)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()

    def stop(self):
        self.server.shutdown();self.server.server_close();self.thread.join()

    def tearDown(self):self.stop();self.tmp.cleanup()

    def request(self,path,data=None,token=None,headers=None):
        h={'Content-Type':'application/json','X-Eksmu-Client':'local-prototype'}
        if token:h['Authorization']='Bearer '+token
        h.update(headers or {})
        c=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        try:
            c.request('GET' if data is None else 'POST',path,None if data is None else json.dumps(data),h)
            r=c.getresponse();raw=r.read()
            return r.status,json.loads(raw) if r.getheader('Content-Type','').startswith('application/json') else raw
        finally:c.close()

    def prepare(self,token=None,n=1):
        return self.request('/api/verification/prepare',{'revision_id':self.rev['id'],'scope':'仅检查这个输入',
            'problem':{'left':rational(1),'right':rational(n)}},token or self.author['token'])

    def run_task(self,task_id,token=None,consent=True):
        return self.request('/api/verification/execute',{'task_id':task_id,'operation_id':str(uuid.uuid4()),'consent':consent},token or self.author['token'])

    def test_prepare_execute_recheck_export_http_journey(self):
        code,task=self.prepare();self.assertEqual(code,200);tid=task['task']['id']
        self.assertEqual(task['attempts'],[])
        code,result=self.run_task(tid);self.assertEqual(code,200);self.assertEqual(result['receipt']['body']['result']['status'],'passed')
        key=self.request('/api/verification/health')[1]['public_key']
        code,verified=self.request('/api/verification/recheck',{'task_id':tid,'expected_public_key':key},self.author['token'])
        self.assertEqual(code,200);self.assertTrue(verified['checks'][0]['replay_matches'])
        code,bundle=self.request('/api/verification/export?task_id='+tid)
        self.assertEqual(code,200);self.assertEqual(bundle['task']['revision'],self.rev)
        self.assertNotIn(self.author['token'],json.dumps(bundle))

    def test_health_distinguishes_optional_finite_check_from_auto_review(self):
        self.assertTrue(self.request('/api/verification/health')[1]['enabled'])
        self.assertFalse(self.request('/api/health')[1]['automatic_verification'])

    def test_no_credentials_cannot_prepare_task(self):
        self.assertEqual(self.request('/api/verification/prepare',{'revision_id':self.rev['id'],'scope':'s','problem':{}})[0],401)

    def test_unknown_revision_is_not_accepted(self):
        self.assertEqual(self.request('/api/verification/prepare',{'revision_id':str(uuid.uuid4()),'scope':'s','problem':{}},self.author['token'])[0],404)

    def test_cannot_inject_verified_or_actor_fields(self):
        self.assertEqual(self.request('/api/verification/prepare',{'revision_id':self.rev['id'],'scope':'s','problem':{},'verified':True},self.author['token'])[0],400)

    def test_requester_not_article_author_controls_task(self):
        tid=self.prepare(self.critic['token'])[1]['task']['id']
        self.assertEqual(self.run_task(tid)[0],403)
        self.assertEqual(self.run_task(tid,self.critic['token'])[0],200)

    def test_execute_requires_explicit_consent(self):
        tid=self.prepare()[1]['task']['id'];self.assertEqual(self.run_task(tid,consent=False)[0],403)

    def test_cross_origin_execution_rejected(self):
        tid=self.prepare()[1]['task']['id']
        code,_=self.request('/api/verification/execute',{'task_id':tid,'operation_id':str(uuid.uuid4()),'consent':True},self.author['token'],{'Origin':'https://other.example'})
        self.assertEqual(code,403)

    def test_normal_report_cannot_become_verified_receipt(self):
        code,report=self.request('/api/evidence',{'revision_id':self.rev['id'],'source':'lean','scope':'all','method':'claim','result':'passed'},self.author['token'])
        self.assertEqual(code,201);self.assertFalse(report['payload']['service_verified'])
        self.assertEqual(self.request('/api/verification/tasks?revision_id='+self.rev['id'])[1]['tasks'],[])

    def test_failed_check_cancellation_and_continued_publication(self):
        before=self.request('/api/export')[1];tid=self.prepare(n=2)[1]['task']['id']
        self.assertEqual(self.run_task(tid)[1]['receipt']['body']['result']['status'],'failed')
        self.assertEqual(self.request('/api/verification/cancel',{'task_id':tid},self.author['token'])[0],200)
        self.assertEqual(self.run_task(tid)[0],409);self.assertEqual(before,self.request('/api/export')[1])
        self.assertEqual(self.request('/api/explorations',{'body':'继续研究'},self.author['token'])[0],201)

    def test_duplicate_or_missing_query_refused(self):
        for q in ['', '?revision_id='+self.rev['id']+'&revision_id='+self.rev['id'],'?extra=1']:
            self.assertEqual(self.request('/api/verification/tasks'+q)[0],400)

    def test_optional_service_disabled_does_not_disable_publication(self):
        self.stop();self.start(False)
        self.assertFalse(self.request('/api/verification/health')[1]['enabled'])
        self.assertEqual(self.prepare()[0],503)
        self.assertEqual(self.request('/api/explorations',{'body':'不使用验证也可以发表'},self.author['token'])[0],201)

    def test_verification_storage_failure_does_not_disable_publication(self):
        with patch('eksmu_bridge.service.Tasks.list',side_effect=sqlite3.OperationalError('temporary')):
            self.assertEqual(self.request('/api/verification/tasks?revision_id='+self.rev['id'])[0],503)
            self.assertEqual(self.request('/api/explorations',{'body':'服务故障时继续发表'},self.author['token'])[0],201)

    def test_signer_persists_after_restart(self):
        key=self.request('/api/verification/health')[1]['public_key'];self.stop();self.start(True)
        self.assertEqual(key,self.request('/api/verification/health')[1]['public_key'])

    def test_corrupt_key_disables_only_verification(self):
        self.stop();(self.root/'community.sqlite.receipt-signer.key').write_bytes(b'corrupt');self.start(True)
        self.assertFalse(self.request('/api/verification/health')[1]['enabled'])
        self.assertEqual(self.request('/api/explorations',{'body':'继续发表'},self.author['token'])[0],201)

    def test_static_ui_has_distinct_evidence_and_receipt_sections(self):
        code,html=self.request('/zh/');self.assertEqual(code,200)
        self.assertIn('可复核工具回执'.encode(),html);self.assertIn('参与者的证据与分析'.encode(),html)
        code,js=self.request('/bridge.js');self.assertEqual(code,200);self.assertNotIn(b'https://',js)

    def test_missing_existing_signer_is_not_silently_regenerated(self):
        self.prepare();self.stop();key=self.root/'community.sqlite.receipt-signer.key';key.unlink();self.start(True)
        self.assertFalse(self.request('/api/verification/health')[1]['enabled'])
        self.assertFalse(key.exists())
        self.assertEqual(self.request('/api/explorations',{'body':'等待密钥恢复时继续发表'},self.author['token'])[0],201)
