"""Private, portable content commitments. No external timestamp or network access."""
import hashlib
import re
import secrets

from .publication_contract import validate_snapshot, verify_package
from .store import Problem, exact
from .time_anchor import DisabledAnchor

FORMAT = 'eksmu.local-time-evidence.v1'
SCHEME = 'sha256-eksmu-snapshot-nonce-v1'
DOMAIN = b'eksmu.local-time-evidence.v1\x00'
NONCE_BYTES = 32


def _hex(value, size):
    if not isinstance(value, str) or re.fullmatch('[0-9a-f]{' + str(size * 2) + '}', value) is None:
        raise Problem('Invalid lowercase hexadecimal commitment field')
    return bytes.fromhex(value)


def commitment(snapshot_sha256, nonce_hex):
    snapshot = _hex(snapshot_sha256, 32)
    nonce = _hex(nonce_hex, NONCE_BYTES)
    return hashlib.sha256(DOMAIN + snapshot + nonce).hexdigest()


def create(package, nonce_hex=None):
    result = verify_package(package)
    nonce_hex = nonce_hex if nonce_hex is not None else secrets.token_hex(NONCE_BYTES)
    value = {'format': FORMAT, 'publication_package': package,
             'commitment': {'scheme': SCHEME, 'snapshot_sha256': result['sha256'],
                            'nonce_hex': nonce_hex, 'sha256': commitment(result['sha256'], nonce_hex)},
             'external_proof': None}
    verify(value)
    return value


def verify(value, expected_commitment=None):
    exact(value, {'format', 'publication_package', 'commitment', 'external_proof'})
    if value['format'] != FORMAT: raise Problem('Unsupported local time evidence format')
    if value['external_proof'] is not None:
        raise Problem('External proof is unsupported in this local-only format')
    checked = verify_package(value['publication_package'])
    record = value['commitment']
    exact(record, {'scheme', 'snapshot_sha256', 'nonce_hex', 'sha256'})
    if record['scheme'] != SCHEME or record['snapshot_sha256'] != checked['sha256']:
        raise Problem('Commitment does not bind the supplied publication snapshot')
    _hex(record['sha256'], 32)
    if record['sha256'] != commitment(record['snapshot_sha256'], record['nonce_hex']):
        raise Problem('Local content commitment mismatch')
    if expected_commitment is not None:
        _hex(expected_commitment, 32)
        if record['sha256'] != expected_commitment:
            raise Problem('Evidence does not match the separately retained commitment')
    paper = value['publication_package']['snapshot']['revision']['payload'].get('paper') or {}
    provenance = paper.get('provenance') or {}
    return {'format': FORMAT, 'snapshot_id': checked['snapshot_id'],
            'snapshot_sha256': checked['sha256'], 'commitment_sha256': record['sha256'],
            'content_integrity': True, 'materials': checked['materials'],
            'status': 'local_only', 'external_time_verified': False,
            'authorship_verified': False, 'public_access_verified': False,
            'platform_recorded_at': value['publication_package']['snapshot']['created_at'],
            'author_declared_prior_publication': provenance or None,
            'public_publication_event': None, 'external_time_evidence': None}


def sources(snapshot):
    """Describe available time claims without creating or implying a proof."""
    validate_snapshot(snapshot)
    paper = snapshot['revision']['payload'].get('paper') or {}
    provenance = paper.get('provenance') or {}
    return {'author_declaration': provenance or None,
            'platform_record': {'revision_created_at': snapshot['revision']['created_at'],
                                'snapshot_created_at': snapshot['created_at'], 'clock': 'instance'},
            'public_publication_event': {'state': 'not_recorded_by_instance', 'evidence': None},
            'local_commitment': {'state': 'offline_optional', 'saved_by_instance': False},
            'external_anchor': DisabledAnchor().status()}
