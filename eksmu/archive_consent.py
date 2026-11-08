"""Experimental archive consent ledger validation; this module sends nothing.

It validates syntax and scope, not identity, snapshot ownership, authority, or
third-party terms. The caller must authenticate and authorize every event and
independently verify snapshot and material bytes. No event is a real consent
until such a trusted integration exists. An external sender must coordinate the
last authorization check with revocation and record each transfer outcome;
validation alone cannot prevent a race or recover already disclosed bytes.
"""
from __future__ import annotations

import copy
import re
import uuid

_HEX = re.compile(r'[0-9a-f]{64}\Z')
_DEST = re.compile(r'[a-z0-9][a-z0-9._:-]{0,127}\Z')


def _id(value):
    if not isinstance(value, str):
        raise ValueError('invalid identifier')
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError('invalid identifier')
    except (AttributeError, ValueError) as exc:
        raise ValueError('invalid identifier') from exc
    return value


def _sha(value):
    if not isinstance(value, str) or _HEX.fullmatch(value) is None:
        raise ValueError('invalid SHA-256')
    return value


def _destination(value):
    if not isinstance(value, str) or _DEST.fullmatch(value) is None:
        raise ValueError('invalid destination identifier')
    return value


def _materials(value):
    if not isinstance(value, list) or not value:
        raise ValueError('materials must be a non-empty list')
    result = []
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {'id', 'sha256'}:
            raise ValueError('invalid material')
        result.append((_id(entry['id']), _sha(entry['sha256'])))
    if result != sorted(result) or len({item[0] for item in result}) != len(result):
        raise ValueError('materials must have unique IDs and be sorted')
    return tuple(result)


def validate_events(events):
    """Validate an ordered ledger and return scoped grants, including revocations.

    Repeating an identical event ID is an idempotent retry. Reusing it with
    different contents, revoking before a grant, or revoking another actor's
    grant fails closed. Claimed actor IDs are not authenticated by this function.
    """
    if not isinstance(events, list):
        raise ValueError('events must be a list')
    seen, grants = {}, {}
    for event in events:
        if not isinstance(event, dict):
            raise ValueError('invalid event')
        event_id = _id(event.get('id'))
        if event_id in seen:
            if seen[event_id] != event:
                raise ValueError('event ID reused with different contents')
            continue
        kind = event.get('type')
        actor = _id(event.get('actor_id'))
        if kind == 'grant':
            if set(event) != {'type', 'id', 'actor_id', 'snapshot_id', 'snapshot_sha256', 'destination', 'materials'}:
                raise ValueError('invalid grant fields')
            _id(event['snapshot_id'])
            _sha(event['snapshot_sha256'])
            _destination(event['destination'])
            _materials(event['materials'])
            grants[event_id] = {'event': copy.deepcopy(event), 'revoked': False}
        elif kind == 'revoke':
            if set(event) != {'type', 'id', 'actor_id', 'grant_id'}:
                raise ValueError('invalid revocation fields')
            grant_id = _id(event['grant_id'])
            if grant_id not in grants or grants[grant_id]['event']['actor_id'] != actor:
                raise ValueError('unknown grant or wrong revoking actor')
            if grants[grant_id]['revoked']:
                raise ValueError('grant already revoked')
            grants[grant_id]['revoked'] = True
        else:
            raise ValueError('unknown event type')
        seen[event_id] = copy.deepcopy(event)
    return grants


def permits_transfer(events, grant_id, *, actor_id, snapshot_id, snapshot_sha256, destination, materials):
    """Check exact scope; this is not authentication or authority to transmit."""
    grants = validate_events(events)
    _id(grant_id)
    _id(actor_id)
    _id(snapshot_id)
    _sha(snapshot_sha256)
    _destination(destination)
    requested = _materials(materials)
    grant = grants.get(grant_id)
    if grant is None or grant['revoked']:
        return False
    event = grant['event']
    return (event['actor_id'] == actor_id and event['snapshot_id'] == snapshot_id
            and event['snapshot_sha256'] == snapshot_sha256 and event['destination'] == destination
            and _materials(event['materials']) == requested)


def may_retry_delivery(last_result):
    """Fail closed after uncertain delivery; no remote idempotency is assumed.

    The caller must persist the attempt ID and transition atomically around any
    transfer. 'not_delivered' must be supported by evidence, not just timeout.
    This helper does not implement a task queue or provide concurrency control.
    """
    if last_result not in {'new', 'not_delivered', 'in_progress', 'unknown', 'delivered'}:
        raise ValueError('unknown delivery state')
    return last_result in {'new', 'not_delivered'}
