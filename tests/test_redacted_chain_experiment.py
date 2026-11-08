"""The experiment must not mistake hash continuity for complete verification."""
import copy
import hashlib
import unittest

from tools.verify_redacted_chain import canonical, verify


def example():
    entries = []
    head = '0' * 64
    for seq, kind in enumerate(('actor', 'exploration', 'revision'), 1):
        row = {'id': str(seq), 'type': kind, 'payload': {'example': seq}}
        row['sha256'] = hashlib.sha256(canonical(row)).hexdigest()
        head = hashlib.sha256(b'eksmu.record-chain.v1\x00' + bytes.fromhex(head) + bytes.fromhex(row['sha256'])).hexdigest()
        entries.append({'seq': seq, 'record_id': row['id'], 'record_sha256': row['sha256'], 'head': head, 'record': row})
    return {'format': 'eksmu.redacted-chain-experiment.v1', 'entries': entries, 'redactions': []}


class RedactedChainTest(unittest.TestCase):
    def test_actor_removed_reference_checks_not_claimed(self):
        doc = example()
        entry = doc['entries'][0]
        entry['record'] = None
        doc['redactions'] = [{'seq': 1, 'record_id': entry['record_id'], 'record_sha256': entry['record_sha256'], 'reason_code': 'privacy'}]
        result = verify(doc, (3, doc['entries'][2]['head']))
        self.assertTrue(result['chain_hash_consistent'])
        self.assertTrue(result['checkpoint_matched'])
        self.assertFalse(result['record_bodies_complete'])
        self.assertFalse(result['record_references_verified'])
        self.assertFalse(result['redaction_authority_verified'])
        self.assertFalse(result['bitcoin_time_verified'])
        self.assertEqual(result['redacted_sequences'], [1])

    def test_corruption_and_unexplained_absence_fail(self):
        doc = example()
        doc['entries'][1]['record']['payload']['example'] = 123
        with self.assertRaisesRegex(ValueError, 'record digest mismatch'):
            verify(doc)
        doc = example()
        doc['entries'][1]['record'] = None
        with self.assertRaisesRegex(ValueError, 'missing matching'):
            verify(doc)

    def test_redaction_does_not_authenticate_and_bad_declarations_fail(self):
        doc = example()
        e = doc['entries'][0]
        e['record'] = None
        notice = {'seq': 1, 'record_id': e['record_id'], 'record_sha256': e['record_sha256'], 'reason_code': 'privacy'}
        doc['redactions'] = [notice]
        self.assertFalse(verify(doc)['redaction_authority_verified'])
        broken = copy.deepcopy(doc)
        broken['redactions'][0]['record_sha256'] = 'a' * 64
        with self.assertRaisesRegex(ValueError, 'missing matching'):
            verify(broken)
        broken = copy.deepcopy(doc)
        broken['redactions'].append(copy.deepcopy(notice))
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            verify(broken)

    def test_checkpoint_detects_truncation_and_replacement(self):
        doc = example()
        checkpoint = (3, doc['entries'][2]['head'])
        shortened = copy.deepcopy(doc)
        shortened['entries'].pop()
        with self.assertRaisesRegex(ValueError, 'checkpoint'):
            verify(shortened, checkpoint)
        with self.assertRaisesRegex(ValueError, 'checkpoint mismatch'):
            verify(doc, (3, 'a' * 64))
        self.assertFalse(verify(doc)['checkpoint_matched'])


if __name__ == '__main__':
    unittest.main()
