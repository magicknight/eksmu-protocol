"""Synthetic contract checks; no real consent or transfers are created."""
import copy
import unittest
import uuid

from eksmu.archive_consent import may_retry_delivery, permits_transfer, validate_events


def uid():
    return str(uuid.uuid4())


class ArchiveConsentTest(unittest.TestCase):
    def setUp(self):
        self.grant = {'type': 'grant', 'id': uid(), 'actor_id': uid(),
                      'snapshot_id': uid(), 'snapshot_sha256': 'a' * 64,
                      'destination': 'archive:example',
                      'materials': [{'id': uid(), 'sha256': 'b' * 64}]}
        self.request = {key: copy.deepcopy(self.grant[key]) for key in
                        ('actor_id', 'snapshot_id', 'snapshot_sha256', 'destination', 'materials')}
        self.revoke = {'type': 'revoke', 'id': uid(), 'actor_id': self.grant['actor_id'],
                       'grant_id': self.grant['id']}

    def test_default_closed_and_exact_scope(self):
        self.assertFalse(permits_transfer([], self.grant['id'], **self.request))
        self.assertTrue(permits_transfer([self.grant], self.grant['id'], **self.request))
        for key, replacement in [('actor_id', uid()), ('snapshot_id', uid()),
                                 ('snapshot_sha256', 'c' * 64), ('destination', 'archive:other')]:
            request = copy.deepcopy(self.request)
            request[key] = replacement
            self.assertFalse(permits_transfer([self.grant], self.grant['id'], **request))
        request = copy.deepcopy(self.request)
        request['materials'].append({'id': uid(), 'sha256': 'd' * 64})
        request['materials'].sort(key=lambda item: (item['id'], item['sha256']))
        self.assertFalse(permits_transfer([self.grant], self.grant['id'], **request))

    def test_revocation_and_idempotent_retries(self):
        events = [self.grant, copy.deepcopy(self.grant), self.revoke, copy.deepcopy(self.revoke)]
        self.assertFalse(permits_transfer(events, self.grant['id'], **self.request))
        self.assertFalse(permits_transfer(events + [copy.deepcopy(self.grant)], self.grant['id'], **self.request))
        collision = copy.deepcopy(self.grant)
        collision['destination'] = 'archive:other'
        with self.assertRaisesRegex(ValueError, 'reused'):
            validate_events([self.grant, collision])
        with self.assertRaisesRegex(ValueError, 'unknown grant'):
            validate_events([self.revoke, self.grant])
        wrong_actor = dict(self.revoke, actor_id=uid())
        with self.assertRaisesRegex(ValueError, 'wrong revoking actor'):
            validate_events([self.grant, wrong_actor])
        with self.assertRaisesRegex(ValueError, 'already revoked'):
            validate_events([self.grant, self.revoke, dict(self.revoke, id=uid())])

    def test_delivery_retries_fail_closed_when_result_unknown(self):
        self.assertTrue(may_retry_delivery('new'))
        self.assertTrue(may_retry_delivery('not_delivered'))
        for state in ('in_progress', 'unknown', 'delivered'):
            self.assertFalse(may_retry_delivery(state))
        with self.assertRaises(ValueError):
            may_retry_delivery('timeout')

    def test_malformed_or_ambiguous_scope_is_rejected(self):
        bad = copy.deepcopy(self.grant)
        bad['materials'].append(copy.deepcopy(bad['materials'][0]))
        with self.assertRaises(ValueError):
            validate_events([bad])
        for change in ({'destination': 'https://example.com/'}, {'snapshot_sha256': 'not-a-hash'},
                       {'materials': []}, {'future_versions': True}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_events([dict(self.grant, **change)])


if __name__ == '__main__':
    unittest.main()
