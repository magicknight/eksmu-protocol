import concurrent.futures
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from eksmu.store import Store, digest
from eksmu_bridge.service import Tasks, BridgeError, check_export, load_key
from eksmu_receipts.core import Rejected, public_hex


def rational(n, d=1):
    return {"op":"rational", "numerator":str(n), "denominator":str(d)}


def problem(n=1):
    return {"left":{"op":"add", "left":rational(1,3), "right":rational(1,6)}, "right":rational(n,2)}


class LinkedServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.community=Store(self.root/'community.sqlite'); self.addCleanup(self.community.close)
        self.author=self.community.register('新人'); self.critic=self.community.register('质疑者')
        self.a=self.author['actor']['id']; self.c=self.critic['actor']['id']
        self.rev=self.community.publish(self.author['token'],'1/3 + 1/6 = 1/2，欢迎质疑')['revision']
        self.key=Ed25519PrivateKey.generate()
        self.tasks=Tasks(self.root/'tasks.sqlite',self.key); self.addCleanup(self.tasks.close)

    def prepare(self, n=1):
        return self.tasks.prepare(self.a,self.rev,'只检查给定等式',problem(n))['task']['id']

    def execute(self, task, op=None):
        return self.tasks.execute(task,self.a,op or str(uuid.uuid4()),True)

    def test_prepare_is_record_only_and_has_no_attempts(self):
        task=self.tasks.view(self.prepare())
        self.assertEqual(task['state'],'prepared'); self.assertEqual(task['attempts'],[])
        self.assertEqual(task['task']['prepared_request']['execution_policy'],'record_only')

    def test_request_snapshot_is_defensively_copied(self):
        rev=deepcopy(self.rev); p=problem()
        tid=self.tasks.prepare(self.a,rev,'范围',p)['task']['id']
        rev['payload']['body']='changed';p['right']=rational(100)
        self.assertEqual(self.tasks.view(tid)['task']['revision'],self.rev)
        self.assertEqual(self.execute(tid)['receipt']['body']['result']['status'],'passed')

    def test_true_equality_is_computed(self):
        self.assertEqual(self.execute(self.prepare())['receipt']['body']['result']['status'],'passed')

    def test_false_equality_is_not_forced_to_pass(self):
        self.assertEqual(self.execute(self.prepare(2))['receipt']['body']['result']['status'],'failed')

    def test_zero_denominator_records_invalid_input(self):
        p=problem();p['right']=rational(1,0)
        tid=self.tasks.prepare(self.a,self.rev,'范围',p)['task']['id']
        self.assertEqual(self.execute(tid)['receipt']['body']['result']['status'],'invalid_input')

    def test_unknown_operation_records_unsupported(self):
        p=problem();p['left']={'op':'shell','command':'ignored'}
        tid=self.tasks.prepare(self.a,self.rev,'范围',p)['task']['id']
        self.assertEqual(self.execute(tid)['receipt']['body']['result']['status'],'unsupported')

    def test_consent_must_be_boolean_true(self):
        tid=self.prepare()
        for value in [False,None,1,'true']:
            with self.subTest(value=value),self.assertRaises(BridgeError):
                self.tasks.execute(tid,self.a,str(uuid.uuid4()),value)
        self.assertEqual(self.tasks.view(tid)['attempts'],[])

    def test_critic_may_prepare_own_check_on_authors_work(self):
        tid=self.tasks.prepare(self.c,self.rev,'独立检查',problem())['task']['id']
        self.assertEqual(self.tasks.execute(tid,self.c,str(uuid.uuid4()),True)['receipt']['body']['result']['status'],'passed')

    def test_cannot_execute_another_requesters_task(self):
        with self.assertRaises(BridgeError):self.tasks.execute(self.prepare(),self.c,str(uuid.uuid4()),True)

    def test_cannot_cancel_another_requesters_task(self):
        with self.assertRaises(BridgeError):self.tasks.cancel(self.prepare(),self.c)

    def test_same_operation_is_idempotent(self):
        tid=self.prepare();op=str(uuid.uuid4())
        self.assertEqual(self.execute(tid,op),self.execute(tid,op))
        self.assertEqual(len(self.tasks.view(tid)['attempts']),1)

    def test_explicit_retry_preserves_both_receipts(self):
        tid=self.prepare();a=self.execute(tid);b=self.execute(tid)
        self.assertNotEqual(a['receipt']['body']['run_id'],b['receipt']['body']['run_id'])
        self.assertEqual(len(self.tasks.view(tid)['attempts']),2)

    def test_cancel_before_start_prevents_execution(self):
        tid=self.prepare();self.tasks.cancel(tid,self.a)
        with self.assertRaises(BridgeError):self.execute(tid)
        self.assertEqual(self.tasks.view(tid)['state'],'cancelled')

    def test_cancel_is_idempotent(self):
        tid=self.prepare();self.assertEqual(self.tasks.cancel(tid,self.a),self.tasks.cancel(tid,self.a))

    def test_cancel_after_execution_keeps_evidence(self):
        tid=self.prepare();r=self.execute(tid);self.tasks.cancel(tid,self.a)
        self.assertEqual(self.tasks.view(tid)['attempts'],[r])
        with self.assertRaises(BridgeError):self.execute(tid)

    def test_replayed_delivery_after_cancel_is_not_new_execution(self):
        tid=self.prepare();op=str(uuid.uuid4());r=self.execute(tid,op);self.tasks.cancel(tid,self.a)
        self.assertEqual(self.execute(tid,op),r)

    def test_receipts_and_cancellation_survive_service_reopen(self):
        tid=self.prepare();self.execute(tid);self.tasks.cancel(tid,self.a)
        other=Tasks(self.root/'tasks.sqlite',self.key)
        try:self.assertEqual(other.view(tid),self.tasks.view(tid))
        finally:other.close()

    def test_concurrent_duplicate_executes_once(self):
        tid=self.prepare();op=str(uuid.uuid4())
        def run(_):
            other=Tasks(self.root/'tasks.sqlite',self.key)
            try:return other.execute(tid,self.a,op,True)
            finally:other.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:results=list(pool.map(run,range(3)))
        self.assertEqual(results,[results[0]]*3)
        self.assertEqual(len(self.tasks.view(tid)['attempts']),1)

    def test_service_failure_rolls_back_and_can_be_retried(self):
        tid=self.prepare();op=str(uuid.uuid4())
        with patch('eksmu_bridge.service.receipts.PendingRun.execute',side_effect=RuntimeError('test failure')):
            with self.assertRaises(RuntimeError):self.execute(tid,op)
        self.assertEqual(self.tasks.view(tid)['attempts'],[])
        self.execute(tid,op)

    def test_task_and_attempt_limits_do_not_touch_publication(self):
        before=self.community.export();tid=self.prepare();self.execute(tid)
        with patch('eksmu_bridge.service.MAX_TASKS',1):
            with self.assertRaises(BridgeError):self.prepare()
        with patch('eksmu_bridge.service.MAX_ATTEMPTS',1):
            with self.assertRaises(BridgeError):self.execute(tid)
        self.assertEqual(before,self.community.export())

    def test_community_database_is_refused_without_writes(self):
        before=self.community.export()
        with self.assertRaises(BridgeError):Tasks(self.root/'community.sqlite',self.key)
        self.assertEqual(before,self.community.export())

    def test_revision_and_input_binding_verified(self):
        tid=self.prepare();self.execute(tid)
        result=self.tasks.recheck(tid,public_hex(self.key))['checks'][0]
        self.assertTrue(result['request_binding_valid']);self.assertTrue(result['replay_matches'])
        self.assertFalse(result['source_semantics_verified']);self.assertFalse(result['publication_authority'])

    def test_wrong_reader_key_is_rejected(self):
        tid=self.prepare();self.execute(tid)
        with self.assertRaises(Rejected):self.tasks.recheck(tid,public_hex(Ed25519PrivateKey.generate()))

    def test_signature_tampering_is_rejected(self):
        tid=self.prepare();self.execute(tid);bundle=self.tasks.export(tid)
        bundle['attempts'][0]['receipt']['signature']='00'*64
        with self.assertRaises(Rejected):check_export(bundle,public_hex(self.key))

    def test_source_tampering_even_with_recomputed_digest_is_rejected(self):
        tid=self.prepare();self.execute(tid);bundle=self.tasks.export(tid)
        revision=bundle['task']['revision'];revision['payload']['body']='different article'
        revision['sha256']=digest({k:v for k,v in revision.items() if k!='sha256'})
        with self.assertRaises(BridgeError):check_export(bundle,public_hex(self.key))

    def test_execution_cannot_silently_change_scope(self):
        tid=self.prepare();self.execute(tid);bundle=self.tasks.export(tid)
        bundle['attempts'][0]['request']['scope']='proof of the whole article'
        with self.assertRaises(BridgeError):check_export(bundle,public_hex(self.key))

    def test_fixture_is_not_accepted_as_signed_execution(self):
        tid=self.prepare();self.execute(tid);bundle=self.tasks.export(tid)
        bundle['attempts'][0]['receipt']={'body':{},'hmac_sha256':'00'*32}
        with self.assertRaises(Rejected):check_export(bundle,public_hex(self.key))

    def test_failed_and_cancelled_check_does_not_change_work(self):
        before=self.community.export();tid=self.prepare(2);self.execute(tid);self.tasks.cancel(tid,self.a)
        self.assertEqual(before,self.community.export())
        self.community.comment(self.critic['token'],self.rev['id'],'仍然可以质疑')
        self.community.revise(self.author['token'],self.rev['payload']['exploration_id'],self.rev['id'],'新版本','继续探索')
        self.community.publish(self.author['token'],'继续发表')

    def test_old_receipt_stays_on_old_revision(self):
        tid=self.prepare();self.execute(tid)
        new=self.community.revise(self.author['token'],self.rev['payload']['exploration_id'],self.rev['id'],'修改','新的主张')
        self.assertEqual(self.tasks.list(new['id']),[])
        self.assertEqual(self.tasks.view(tid)['task']['revision']['id'],self.rev['id'])

    def test_exports_do_not_contain_identity_or_signing_secrets(self):
        tid=self.prepare();self.execute(tid);raw=json.dumps(self.tasks.export(tid))
        self.assertNotIn(self.author['token'],raw)
        self.assertNotIn(self.key.private_bytes_raw().hex(),raw)
        self.assertNotIn('key_hash',raw)

    def test_backup_plus_task_export_rechecks_after_migration(self):
        tid=self.prepare();self.execute(tid);bundle=self.tasks.export(tid)
        target=Store(self.root/'second.sqlite')
        try:
            target.import_bundle(self.community.export())
            self.assertTrue(check_export(bundle,public_hex(self.key))[0]['replay_matches'])
            target.comment(self.author['token'],self.rev['id'],'迁移后继续讨论')
        finally:target.close()

    def test_key_persistence_and_corrupt_key_refusal(self):
        path=self.root/'signer.key';key=load_key(path)
        self.assertEqual(public_hex(key),public_hex(load_key(path)))
        path.write_bytes(b'invalid')
        with self.assertRaises(BridgeError):load_key(path)
        self.assertEqual(path.read_bytes(),b'invalid')

    def test_cli_independent_recheck(self):
        tid=self.prepare();self.execute(tid);path=self.root/'bundle.json'
        path.write_text(json.dumps(self.tasks.export(tid)))
        good=subprocess.run([sys.executable,'-m','eksmu_bridge',str(path),'--key',public_hex(self.key)],capture_output=True,text=True)
        self.assertEqual(good.returncode,0,good.stderr);self.assertTrue(json.loads(good.stdout)['checks'][0]['signature_valid'])
        bad=subprocess.run([sys.executable,'-m','eksmu_bridge',str(path),'--key','00'*32],capture_output=True,text=True)
        self.assertEqual(bad.returncode,1)
