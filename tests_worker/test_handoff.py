import concurrent.futures
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from eksmu.store import Store
from eksmu_bridge.service import BridgeError, Tasks
from eksmu_bridge.handoff import Handoffs, execute_capsule, verify_submission, DOMAIN
from eksmu_receipts import core as receipts


def rational(n):
    return {"op": "rational", "numerator": str(n), "denominator": "1"}


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "community.sqlite"); self.addCleanup(self.store.close)
        self.author = self.store.register("任何人"); self.critic = self.store.register("独立检查者")
        self.actor = self.author["actor"]["id"]; self.other = self.critic["actor"]["id"]
        self.revision = self.store.publish(self.author["token"], "PRIVATE SOURCE TEXT NOT SENT TO THE WORKER")["revision"]
        self.local_key = Ed25519PrivateKey.generate(); self.worker = Ed25519PrivateKey.generate()
        self.tasks = Tasks(self.root / "tasks.sqlite", self.local_key); self.addCleanup(self.tasks.close)
        self.timestamp = 1_000_000
        self.handoffs = Handoffs(self.tasks, clock=lambda: self.timestamp)
        self.task = self.tasks.prepare(self.actor, self.revision, "给定输入的有限检查",
                                      {"left": rational(1), "right": rational(1)})["task"]["id"]

    def dispatch(self, *, key=None, op=None, actor=None, ttl=3600):
        return self.handoffs.dispatch(self.task, actor or self.actor, op or str(uuid.uuid4()),
                                      receipts.public_hex(key or self.worker), True, ttl)

    def result_for(self, dispatched):
        return execute_capsule(dispatched["dispatch"]["capsule"], self.worker,
                               allow_local=True, current_time=self.timestamp)

    def collect(self, dispatched, result=None):
        return self.handoffs.collect(dispatched["dispatch"]["id"], self.actor,
                                     result if result is not None else self.result_for(dispatched))

    def test_dispatch_only_records_no_computation(self):
        with patch('eksmu_bridge.handoff.receipts.PendingRun.execute') as execute:
            d = self.dispatch()
        execute.assert_not_called(); self.assertEqual(d["disposition"], "ready")
        self.assertIsNone(d["submission"])

    def test_dispatch_requires_explicit_boolean_consent(self):
        for consent in (False, 1, 'true', None):
            with self.subTest(consent=consent), self.assertRaises(BridgeError):
                self.handoffs.dispatch(self.task, self.actor, str(uuid.uuid4()),
                                       receipts.public_hex(self.worker), consent)
        self.assertEqual(self.handoffs.list(self.task, self.actor), [])

    def test_lifetime_bounds_and_types(self):
        for ttl in (True, 0, 59, 86401, '3600'):
            with self.subTest(ttl=ttl), self.assertRaises(BridgeError): self.dispatch(ttl=ttl)

    def test_wrong_actor_cannot_dispatch(self):
        with self.assertRaises(BridgeError): self.dispatch(actor=self.other)

    def test_critic_controls_own_task_not_author(self):
        own = self.tasks.prepare(self.other, self.revision, '独立检查',
                                 {'left': rational(1), 'right': rational(2)})['task']['id']
        d = self.handoffs.dispatch(own, self.other, str(uuid.uuid4()), receipts.public_hex(self.worker), True)
        with self.assertRaises(BridgeError): self.handoffs.view(d['dispatch']['id'], self.actor)
        result = self.result_for(d)
        accepted = self.handoffs.collect(d['dispatch']['id'], self.other, result)
        self.assertEqual(accepted['submission']['body']['receipt']['body']['result']['status'], 'failed')

    def test_no_default_or_mandatory_worker_key(self):
        key2 = Ed25519PrivateKey.generate()
        a, b = self.dispatch(), self.dispatch(key=key2)
        for dispatched, signer in ((a, self.worker), (b, key2)):
            result = execute_capsule(dispatched['dispatch']['capsule'], signer, allow_local=True,
                                     current_time=self.timestamp)
            self.assertEqual(self.collect(dispatched, result)['disposition'], 'recorded')
        self.assertNotEqual(a['dispatch']['capsule']['expected_public_key'], receipts.public_hex(self.local_key))

    def test_duplicate_operation_reuses_dispatch(self):
        op = str(uuid.uuid4()); a = self.dispatch(op=op); b = self.dispatch(op=op)
        self.assertEqual(a, b); self.assertEqual(len(self.handoffs.list(self.task, self.actor)), 1)

    def test_operation_cannot_change_worker_or_lifetime(self):
        op = str(uuid.uuid4()); self.dispatch(op=op)
        with self.assertRaises(BridgeError): self.dispatch(op=op, key=Ed25519PrivateKey.generate())
        with self.assertRaises(BridgeError): self.dispatch(op=op, ttl=60)

    def test_cancel_prevents_new_dispatch(self):
        self.tasks.cancel(self.task, self.actor)
        with self.assertRaises(BridgeError): self.dispatch()

    def test_cancel_does_not_erase_existing_dispatch(self):
        op = str(uuid.uuid4()); d = self.dispatch(op=op); self.tasks.cancel(self.task, self.actor)
        same = self.dispatch(op=op)
        self.assertEqual(same['dispatch'], d['dispatch']); self.assertEqual(same['disposition'], 'cancelled')

    def test_expired_pending_dispatch_is_visible_as_expired(self):
        d = self.dispatch(ttl=60); self.timestamp += 60
        self.assertEqual(self.handoffs.view(d['dispatch']['id'], self.actor)['disposition'], 'expired')

    def test_quota_independent_of_publication(self):
        before = self.store.export(); self.dispatch()
        with patch('eksmu_bridge.handoff.MAX_HANDOFFS', 1):
            with self.assertRaises(BridgeError): self.dispatch()
        self.assertEqual(self.store.export(), before)
        self.store.publish(self.author['token'], '配额用完仍然发表')

    def test_worker_must_also_opt_in(self):
        d = self.dispatch()
        with self.assertRaises(BridgeError): execute_capsule(d['dispatch']['capsule'], self.worker)

    def test_wrong_worker_key_rejected_before_execution(self):
        d = self.dispatch()
        with patch('eksmu_bridge.handoff.receipts.PendingRun.execute') as execute:
            with self.assertRaises(BridgeError):
                execute_capsule(d['dispatch']['capsule'], self.local_key, allow_local=True, current_time=self.timestamp)
        execute.assert_not_called()

    def test_worker_checks_expiry_and_not_before(self):
        d = self.dispatch(ttl=60)
        for t in (self.timestamp - 1, self.timestamp + 60):
            with self.subTest(t=t), self.assertRaises(BridgeError):
                execute_capsule(d['dispatch']['capsule'], self.worker, allow_local=True, current_time=t)

    def test_worker_rejects_record_only(self):
        d = self.dispatch(); capsule = d['dispatch']['capsule']
        capsule['request']['execution_policy'] = 'record_only'
        with self.assertRaises(BridgeError): execute_capsule(capsule, self.worker, allow_local=True, current_time=self.timestamp)

    def test_worker_rejects_requested_network_or_code_rights(self):
        for right in ('network', 'user_code', 'community_write', 'funds'):
            capsule = deepcopy(self.dispatch()['dispatch']['capsule']); capsule['request']['rights'][right] = True
            with self.subTest(right=right), self.assertRaises(ValueError):
                execute_capsule(capsule, self.worker, allow_local=True, current_time=self.timestamp)

    def test_capsule_does_not_send_article_identity_or_private_keys(self):
        text = receipts.canonical(self.dispatch()['dispatch']['capsule']).decode()
        for secret in (self.author['token'], self.revision['payload']['body'], self.actor,
                       self.worker.private_bytes_raw().hex(), self.local_key.private_bytes_raw().hex()):
            self.assertNotIn(secret, text)

    def test_collect_verifies_dispatch_signature_and_recomputes(self):
        d = self.dispatch(); result = self.result_for(d)
        check = verify_submission(d['dispatch']['capsule'], result, receipts.public_hex(self.worker))
        self.assertTrue(check['dispatch_binding_valid']); self.assertTrue(check['replay_matches'])
        self.assertFalse(check['publication_authority']); self.assertFalse(check['requester_identity_certified'])
        self.assertEqual(self.collect(d, result)['disposition'], 'recorded')

    def test_wrong_reader_key_rejected(self):
        d = self.dispatch()
        with self.assertRaises(BridgeError):
            verify_submission(d['dispatch']['capsule'], self.result_for(d), receipts.public_hex(self.local_key))

    def test_tampered_signature_does_not_store_result(self):
        d = self.dispatch(); result = self.result_for(d); result['signature'] = '00' * 64
        with self.assertRaises(BridgeError): self.collect(d, result)
        self.assertIsNone(self.handoffs.view(d['dispatch']['id'], self.actor)['submission'])

    def test_receipt_cannot_be_replayed_into_other_dispatch(self):
        a, b = self.dispatch(), self.dispatch()
        with self.assertRaises(BridgeError): self.collect(b, self.result_for(a))

    def test_changed_capsule_rejected_against_persisted_dispatch(self):
        d = self.dispatch(); edited = deepcopy(d['dispatch']['capsule']); edited['request']['scope'] = 'changed'
        result = execute_capsule(edited, self.worker, allow_local=True, current_time=self.timestamp)
        with self.assertRaises(BridgeError): self.collect(d, result)

    def test_even_signed_false_claim_is_rejected_by_recomputation(self):
        d = self.dispatch(); result = self.result_for(d)
        body = deepcopy(result['body']['receipt']['body']); body['result']['status'] = 'failed'
        result['body']['receipt'] = receipts.sign_body(body, self.worker)
        result['signature'] = self.worker.sign(DOMAIN + receipts.canonical(result['body'])).hex()
        with self.assertRaises(ValueError): self.collect(d, result)

    def test_wrong_owner_cannot_collect(self):
        d = self.dispatch()
        with self.assertRaises(BridgeError):
            self.handoffs.collect(d['dispatch']['id'], self.other, self.result_for(d))

    def test_collect_is_idempotent_and_preserves_time(self):
        d = self.dispatch(); result = self.result_for(d); a = self.collect(d, result)
        self.timestamp += 2; self.assertEqual(self.collect(d, result), a)

    def test_conflicting_resubmission_cannot_overwrite(self):
        d = self.dispatch(); first = self.collect(d)
        with self.assertRaises(BridgeError): self.collect(d)
        self.assertEqual(self.handoffs.view(d['dispatch']['id'], self.actor), first)

    def test_late_cancelled_result_is_preserved_but_not_active_completion(self):
        before = self.store.export(); d = self.dispatch(); result = self.result_for(d)
        self.tasks.cancel(self.task, self.actor); accepted = self.collect(d, result)
        self.assertEqual(accepted['disposition'], 'late_cancelled'); self.assertIsNotNone(accepted['submission'])
        self.assertEqual(before, self.store.export())
        self.store.comment(self.critic['token'], self.revision['id'], '继续评论')
        self.store.publish(self.author['token'], '继续研究')

    def test_late_expired_result_is_preserved(self):
        d = self.dispatch(ttl=60); result = self.result_for(d); self.timestamp += 61
        self.assertEqual(self.collect(d, result)['disposition'], 'late_expired')

    def test_cancel_after_collection_keeps_prior_disposition(self):
        d = self.dispatch(); self.collect(d); self.tasks.cancel(self.task, self.actor)
        state = self.handoffs.view(d['dispatch']['id'], self.actor)
        self.assertEqual(state['disposition'], 'recorded'); self.assertIsNotNone(state['task_cancelled_at'])

    def test_persistence_and_legacy_tasks_survive_reopen(self):
        d = self.dispatch(); recorded = self.collect(d)
        other = Tasks(self.root / 'tasks.sqlite', self.local_key)
        try:
            self.assertEqual(Handoffs(other).view(d['dispatch']['id'], self.actor), recorded)
            self.assertEqual(other.view(self.task)['attempts'], [])
            other.execute(self.task, self.actor, str(uuid.uuid4()), True)
        finally: other.close()

    def test_concurrent_dispatch_is_idempotent(self):
        op = str(uuid.uuid4())
        def work(_):
            other = Tasks(self.root / 'tasks.sqlite', self.local_key)
            try: return Handoffs(other, clock=lambda: self.timestamp).dispatch(
                self.task, self.actor, op, receipts.public_hex(self.worker), True)
            finally: other.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool: results = list(pool.map(work, range(3)))
        self.assertEqual(results, [results[0]] * 3)

    def test_concurrent_collect_is_idempotent(self):
        d = self.dispatch(); result = self.result_for(d)
        def work(_):
            other = Tasks(self.root / 'tasks.sqlite', self.local_key)
            try: return Handoffs(other, clock=lambda: self.timestamp).collect(d['dispatch']['id'], self.actor, result)
            finally: other.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool: results = list(pool.map(work, range(3)))
        self.assertEqual(results, [results[0]] * 3)

    def test_worker_failure_does_not_create_a_completion(self):
        before = self.store.export(); d = self.dispatch()
        with patch('eksmu_bridge.handoff.receipts.PendingRun.execute', side_effect=RuntimeError('failure')):
            with self.assertRaises(RuntimeError): self.result_for(d)
        self.assertIsNone(self.handoffs.view(d['dispatch']['id'], self.actor)['submission'])
        self.assertEqual(before, self.store.export()); self.collect(d)
