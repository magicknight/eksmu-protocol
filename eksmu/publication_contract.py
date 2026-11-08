"""Portable publication packages: content integrity, not signatures or time proof."""
import base64
from datetime import datetime
import hashlib
import re
from urllib.parse import urlencode

from .store import Problem, KINDS, canonical, digest, enum, exact, identifier, text
from verification_lab.protocol import revision_binding

SNAPSHOT = 'eksmu.publication-snapshot.v1'
PACKAGE = 'eksmu.publication-package.v1'
EVENT = 'eksmu.publication-event.v1'
MAX_FILE = 32 * 1024 * 1024
MAX_FILES = 16
MAX_PACKAGE = 128 * 1024 * 1024
MATERIAL_FIELDS = {'id', 'revision_id', 'actor_id', 'filename', 'mime', 'sha256', 'created_at', 'size'}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def fingerprint(value):
    if not isinstance(value, str) or re.fullmatch('[0-9a-f]{64}', value) is None:
        raise Problem('Invalid SHA-256')


def timestamp(value, utc=True):
    text(value, 64)
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None or (utc and stamp.utcoffset().total_seconds() != 0):
            raise ValueError
    except ValueError as exc:
        raise Problem('A UTC record time is required') from exc


def optional_id(value):
    if value is not None:
        identifier(value)


def checked_digest(value):
    fingerprint(value['sha256'])
    if value['sha256'] != digest({k: v for k, v in value.items() if k != 'sha256'}):
        raise Problem('Publication digest mismatch')


def seal(value):
    return value | {'sha256': digest(value)}


def validate_snapshot(value):
    exact(value, {'format', 'id', 'created_at', 'previous_snapshot_id', 'publisher',
                  'exploration', 'revision', 'materials', 'sha256'})
    if value['format'] != SNAPSHOT:
        raise Problem('Unsupported publication snapshot')
    identifier(value['id']); optional_id(value['previous_snapshot_id']); timestamp(value['created_at'])
    if value['previous_snapshot_id'] == value['id']:
        raise Problem('Snapshot cannot refer to itself')
    exact(value['publisher'], {'id', 'alias'})
    identifier(value['publisher']['id']); text(value['publisher']['alias'], 80)
    work, rev = value['exploration'], value['revision']
    exact(work, {'id', 'type', 'actor_id', 'created_at', 'payload', 'sha256'})
    identifier(work['id']); identifier(work['actor_id']); timestamp(work['created_at'])
    if work['type'] != 'exploration': raise Problem('Expected an exploration')
    exact(work['payload'], {'kind'}); enum(work['payload']['kind'], KINDS); checked_digest(work)
    revision_binding(rev)
    if rev['payload']['exploration_id'] != work['id'] or rev['actor_id'] != work['actor_id'] or work['actor_id'] != value['publisher']['id']:
        raise Problem('Snapshot authorship/reference mismatch')
    items = value['materials']
    if not isinstance(items, list) or len(items) > MAX_FILES:
        raise Problem('Invalid snapshot material collection')
    seen = set()
    for item in items:
        exact(item, MATERIAL_FIELDS)
        identifier(item['id']); timestamp(item['created_at'], utc=False); fingerprint(item['sha256'])
        if item['id'] in seen or item['revision_id'] != rev['id'] or item['actor_id'] != rev['actor_id']:
            raise Problem('Snapshot material belongs to another revision or is duplicated')
        seen.add(item['id'])
        text(item['filename'], 180)
        if item['filename'] in {'.', '..'} or any(c in item['filename'] for c in '/\\') or any(ord(c) < 32 or ord(c) == 127 for c in item['filename']):
            raise Problem('Invalid material filename')
        enum(item['mime'], {'text/plain', 'application/pdf', 'application/octet-stream', 'image/png', 'image/jpeg', 'image/webp'})
        if type(item['size']) is not int or not 1 <= item['size'] <= MAX_FILE:
            raise Problem('Invalid material size')
    if [m['id'] for m in items] != sorted(seen):
        raise Problem('Snapshot materials must be sorted by ID')
    checked_digest(value)
    return value


def verify_package(value, expected_sha256=None):
    exact(value, {'format', 'snapshot', 'blobs'})
    if value['format'] != PACKAGE:
        raise Problem('Unsupported publication package')
    snap = validate_snapshot(value['snapshot'])
    if expected_sha256 is not None:
        fingerprint(expected_sha256)
        if snap['sha256'] != expected_sha256:
            raise Problem('Snapshot does not match the expected digest')
    blobs = value['blobs']
    if not isinstance(blobs, list) or len(blobs) > MAX_FILES:
        raise Problem('Invalid package blob collection')
    required = {m['sha256'] for m in snap['materials']}; sizes = {}
    for blob in blobs:
        exact(blob, {'sha256', 'data_b64'}); fingerprint(blob['sha256'])
        if blob['sha256'] in sizes or blob['sha256'] not in required:
            raise Problem('Duplicate or unexpected package blob')
        encoded = blob['data_b64']
        if not isinstance(encoded, str) or len(encoded) > ((MAX_FILE + 2) // 3) * 4:
            raise Problem('Oversized package blob')
        try: raw = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc: raise Problem('Invalid blob encoding') from exc
        if not raw or len(raw) > MAX_FILE or sha(raw) != blob['sha256']:
            raise Problem('Package material digest mismatch')
        if base64.b64encode(raw).decode() != encoded:
            raise Problem('Noncanonical base64 material')
        sizes[blob['sha256']] = len(raw)
    if set(sizes) != required or any(sizes[m['sha256']] != m['size'] for m in snap['materials']):
        raise Problem('Missing or wrong-size package material')
    return {'format': PACKAGE, 'snapshot_id': snap['id'], 'sha256': snap['sha256'],
            'content_integrity': True, 'materials': len(snap['materials']),
            'authorship_verified': False, 'external_time_verified': False,
            'current_withdrawal_status_checked': False}


def validate_event(value):
    exact(value, {'format', 'id', 'exploration_id', 'revision_id', 'actor_id', 'previous_event_id',
                  'created_at', 'kind', 'body', 'sha256'})
    if value['format'] != EVENT: raise Problem('Unsupported publication event')
    for field in ('id', 'exploration_id', 'revision_id', 'actor_id'): identifier(value[field])
    optional_id(value['previous_event_id']); timestamp(value['created_at'])
    enum(value['kind'], {'note', 'withdraw', 'reinstate'}); text(value['body'], 4000)
    checked_digest(value)


def tex(value):
    # Encode data characters, never preserve user-supplied TeX commands. Numeric
    # brace characters avoid unbalanced BibTeX groups even for an unmatched '{'.
    escapes = {'\\': r'\textbackslash{}', '{': r'\char123{}', '}': r'\char125{}',
               '%': r'\%', '$': r'\$', '#': r'\#', '&': r'\&', '_': r'\_',
               '^': r'\textasciicircum{}', '~': r'\textasciitilde{}'}
    return ''.join(' ' if ord(c) < 32 or ord(c) == 127 else escapes.get(c, c) for c in value)


def citation(revision, publisher, snapshot=None):
    revision_binding(revision)
    p = revision['payload']; paper = p.get('paper') or {}
    authors = [a['name'] for a in paper.get('authors', [])] or [publisher['alias']]
    wid, rid = p['exploration_id'], revision['id']
    link = '/#' + urlencode({'exploration': wid, 'revision': rid})
    identity = 'Work ' + wid + '; revision ' + rid + '; SHA-256 ' + revision['sha256']
    note = identity + '; revision recorded ' + revision['created_at'] + ' (local clock, not first publication)'
    if snapshot is not None:
        validate_snapshot(snapshot)
        if snapshot['revision'] != revision or snapshot['publisher'] != publisher:
            raise Problem('Citation snapshot target mismatch')
        link = '/?' + urlencode({'snapshot': snapshot['id']})
        note += '; snapshot ' + snapshot['id'] + '; snapshot SHA-256 ' + snapshot['sha256']
    prior = paper.get('provenance') or {}
    if prior.get('prior_publication_date'):
        note += '; prior publication date (author-declared): ' + prior['prior_publication_date'] + '; source: ' + prior['source']
    note += '; private instance; no DOI or external timestamp assigned'
    key = 'eksmu-' + (snapshot['id'] if snapshot else rid)
    names = ' and '.join('{' + tex(a) + '}' for a in authors)
    bibtex = ('@misc{' + key + ',\n  author = {' + names + '},\n  title = {{' + tex(p['title']) + '}},\n'
              '  howpublished = {Eksmu (private instance)},\n  note = {' + tex(note) + '}\n}\n')
    return {'bibtex': bibtex, 'text': '；'.join(authors) + '. ' + p['title'] + '. ' + note,
            'relative_url': link, 'exploration_id': wid, 'revision_id': rid,
            'snapshot_id': snapshot['id'] if snapshot else None,
            'scope': '固定版本引用包含清单内材料；正文版本引用不固定后添附件。链接仅适用于同一私有实例。'}
