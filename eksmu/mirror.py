"""Public mirror: everything an outsider needs to copy and independently check the
public record of this instance, without trusting this instance afterwards.

The manifest (``GET /api/mirror``) carries the public records, the full record
chain, every time anchor with its OpenTimestamps proof bytes, the index of
publication snapshots and the author notices (withdraw/reinstate/note).
Snapshot packages, which can be large, are fetched one by one from
``/api/publications/export``. ``mirror-pull`` does both and then runs
``verify_mirror``, which needs nothing but the downloaded files.

What a successful check means: the copy is internally consistent (record
digests, ownership and revision order, chain heads, anchor bindings, snapshot
contents, notice histories). It does NOT mean: authorship is cryptographically
signed (records carry no author signatures yet), the anchors are confirmed in
Bitcoin (run ``ots verify`` against a node for that), or that this is every
record the operator ever held (a mirror only proves what it was given; compare
the chain head with an anchored head or another mirror to detect truncation).
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from urllib.parse import urlencode, urlsplit
import urllib.request

from . import record_chain
from .store import FORMAT as RECORDS_FORMAT, Problem, canonical, digest, exact, identifier, validate
from .publication_contract import MAX_PACKAGE, fingerprint, validate_event, validate_snapshot, verify_package

FORMAT = 'eksmu.public-mirror.v1'
FORMAT_V2 = 'eksmu.public-mirror.v2'
MAX_MANIFEST = 64 * 1024 * 1024
ANCHOR_FIELDS = {'id', 'seq', 'head', 'method', 'submitted_at', 'state', 'bitcoin', 'checked', 'proof_b64'}
SNAPSHOT_FIELDS = {'id', 'exploration_id', 'revision_id', 'created_at', 'sha256'}
REDACTION_FIELDS = {'attachment_id', 'record_id', 'revision_id', 'content_sha256', 'reason_code', 'reason', 'decided_at'}
EXCLUDED = [
    'account e-mail addresses, password hashes, sessions and recovery material',
    'private behavior reports, notifications, bookmarks and reading state',
    'attachments that the author did not pin into a publication snapshot',
    'community topic metadata and Lean/verification worker queues',
]


def _publication_rows(path: Path):
    if not path.is_file():
        return [], []
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'publication_snapshots' not in tables:
            return [], []
        snaps = [json.loads(r[0]) for r in db.execute('SELECT document FROM publication_snapshots ORDER BY rowid')]
        events = [json.loads(r[0]) for r in db.execute('SELECT document FROM publication_events ORDER BY rowid')]
        return snaps, events
    finally:
        db.close()


def _analysis_rows(path: Path):
    empty = {'reports': [], 'responses': []}
    if not path.is_file():
        return empty
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'analysis_reports' not in tables:
            return empty
        return {'reports': [json.loads(r[0]) for r in db.execute('SELECT document FROM analysis_reports ORDER BY rowid')],
                'responses': [json.loads(r[0]) for r in db.execute('SELECT document FROM analysis_responses ORDER BY rowid')]}
    finally:
        db.close()


def _redaction_rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    from .material_chain import material_record_id
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'research_redactions' not in tables:
            return []
        rows = db.execute(
            'SELECT attachment_id, revision_id, sha256, reason_code, reason, decided_at '
            'FROM research_redactions ORDER BY decided_at, attachment_id'
        ).fetchall()
        return [
            {
                'attachment_id': aid,
                'record_id': material_record_id(aid),
                'revision_id': rid,
                'content_sha256': csha,
                'reason_code': rcode,
                'reason': rtext,
                'decided_at': decided,
            }
            for aid, rid, csha, rcode, rtext, decided in rows
        ]
    finally:
        db.close()


def _check_redactions(value, index) -> int:
    from .research import REDACTION_REASONS
    if not isinstance(value, list):
        raise Problem('Mirror redactions must be a list')
    seen = set()
    for item in value:
        exact(item, REDACTION_FIELDS)
        identifier(item['attachment_id'])
        if item['attachment_id'] in seen:
            raise Problem(f'Duplicate redaction for attachment {item["attachment_id"]}')
        seen.add(item['attachment_id'])
        identifier(item['record_id'])
        identifier(item['revision_id'])
        fingerprint(item['content_sha256'])
        if item['reason_code'] not in REDACTION_REASONS:
            raise Problem(f'Mirror redaction {item["attachment_id"]}: invalid reason_code')
        if not isinstance(item['reason'], str) or not item['reason'].strip():
            raise Problem(f'Mirror redaction {item["attachment_id"]}: missing reason')
        rev = index.get(item['revision_id'])
        if rev is None or rev['type'] != 'revision':
            raise Problem(f'Mirror redaction {item["attachment_id"]} references unknown revision')
        mrec = index.get(item['record_id'])
        if mrec is not None:
            if (
                mrec['type'] != 'material'
                or mrec['payload']['attachment_id'] != item['attachment_id']
                or mrec['payload']['revision_id'] != item['revision_id']
                or mrec['payload']['content_sha256'] != item['content_sha256']
            ):
                raise Problem(f'Mirror redaction {item["attachment_id"]} does not match its material record')
    return len(value)


def _check_analysis(value, index) -> dict:
    from .analysis import validate_report, validate_response
    exact(value, {'reports', 'responses'})
    reports = {}
    for report in value['reports']:
        validate_report(report)
        rev = index.get(report['revision_id'])
        if rev is None or rev['type'] != 'revision' or rev['sha256'] != report['revision_sha256']:
            raise Problem(f'Analysis report {report["id"]} does not match the revision it names')
        reports[report['id']] = report
    for response in value['responses']:
        validate_response(response)
        report = reports.get(response['report_id'])
        if report is None or not any(f['id'] == response['finding_id'] for f in report['findings']):
            raise Problem(f'Analysis response {response["id"]} targets an unknown finding')
        actor = index.get(response['actor_id'])
        author = index[report['revision_id']]['actor_id']
        if actor is None or actor['type'] != 'actor' or response['is_author'] != (response['actor_id'] == author):
            raise Problem(f'Analysis response {response["id"]} has a wrong author flag')
    return {'reports': len(reports), 'responses': len(value['responses'])}


def manifest(store, database: str | Path, origin: str | None = None) -> dict:
    """Build the public manifest. The caller holds a consistent read of ``store``."""
    from .papers import BUNDLE_FORMAT
    # Publications first: snapshots and notices only reference records that
    # already existed, so records read afterwards always cover them.
    snaps, events = _publication_rows(Path(str(database) + '.publications.sqlite'))
    analysis = _analysis_rows(Path(str(database) + '.analysis.sqlite'))
    redactions = _redaction_rows(Path(str(database) + '.research.sqlite'))
    rows = store.rows()
    db = store.db
    entries = [{'seq': s, 'record_id': r, 'record_sha256': d, 'head': h, 'chained_at': c, 'backfilled': bool(b)}
               for s, r, d, h, c, b in db.execute('SELECT seq, record_id, record_sha256, head, chained_at, backfilled '
                                                   'FROM record_chain ORDER BY seq')]
    anchors = []
    for row in db.execute(f'SELECT {record_chain.ANCHOR_COLUMNS}, proof FROM chain_anchors ORDER BY id'):
        value = record_chain._anchor(row[:-1])
        anchors.append({k: value[k] for k in ANCHOR_FIELDS - {'proof_b64'}}
                       | {'proof_b64': base64.b64encode(bytes(row[-1])).decode()})
    extended = any(r['type'] == 'revision' and 'paper' in r['payload'] for r in rows)
    is_v2 = any(r['type'] == 'material' for r in rows) or bool(redactions)
    out = {
        'format': FORMAT_V2 if is_v2 else FORMAT,
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'origin': origin,
        'records': {'format': BUNDLE_FORMAT if extended else RECORDS_FORMAT, 'records': rows, 'sha256': digest(rows)},
        'chain': {'format': record_chain.FORMAT, 'entries': entries, 'algorithm': record_chain.ALGORITHM},
        'anchors': anchors,
        'publications': {
            'snapshots': [{'id': s['id'], 'exploration_id': s['exploration']['id'], 'revision_id': s['revision']['id'],
                           'created_at': s['created_at'], 'sha256': s['sha256']} for s in snaps],
            'events': events,
            'package_path': '/api/publications/export?snapshot_id={id}',
        },
        'analysis': analysis,
        'excluded': EXCLUDED,
        'signatures': None,
    }
    if is_v2:
        out['redactions'] = redactions
    return out


def _check_records(bundle) -> list[dict]:
    from .papers import BUNDLE_FORMAT
    exact(bundle, {'format', 'records', 'sha256'})
    if bundle['format'] not in (RECORDS_FORMAT, BUNDLE_FORMAT) or bundle['sha256'] != digest(bundle['records']):
        raise Problem('Mirror records: unsupported format or bundle digest mismatch')
    validate(bundle['records'])
    return bundle['records']


def _check_chain(chain, rows) -> dict:
    exact(chain, {'format', 'entries', 'algorithm'})
    if chain['format'] != record_chain.FORMAT:
        raise Problem('Mirror chain: unsupported format')
    entries = chain['entries']
    if not isinstance(entries, list) or len(entries) != len(rows):
        raise Problem('Mirror chain: length differs from the records')
    head = record_chain.GENESIS
    for n, (entry, row) in enumerate(zip(entries, rows), 1):
        exact(entry, {'seq', 'record_id', 'record_sha256', 'head', 'chained_at', 'backfilled'})
        if entry['seq'] != n or entry['record_id'] != row['id'] or entry['record_sha256'] != row['sha256']:
            raise Problem(f'Mirror chain: entry {n} does not match record {n}')
        fingerprint(entry['head'])
        head = record_chain.fold(head, row['sha256'])
        if head != entry['head']:
            raise Problem(f'Mirror chain: head mismatch at entry {n}')
    return {'length': len(entries), 'head': head,
            'backfilled_entries': sum(1 for e in entries if e['backfilled'] is True)}


def _check_anchors(anchors, entries) -> list[dict]:
    if not isinstance(anchors, list):
        raise Problem('Mirror anchors must be a list')
    out = []
    for anchor in anchors:
        exact(anchor, ANCHOR_FIELDS)
        seq = anchor['seq']
        if type(seq) is not int or not 1 <= seq <= len(entries) or entries[seq - 1]['head'] != anchor['head']:
            raise Problem(f'Mirror anchor {anchor["id"]} does not bind to the chain')
        try:
            proof = base64.b64decode(anchor['proof_b64'], validate=True)
        except (ValueError, TypeError) as exc:
            raise Problem(f'Mirror anchor {anchor["id"]}: invalid proof encoding') from exc
        if not proof:
            raise Problem(f'Mirror anchor {anchor["id"]}: empty proof')
        out.append({'id': anchor['id'], 'seq': seq, 'head': anchor['head'], 'state_claimed': anchor['state'],
                    'proof_bytes': len(proof), 'bitcoin_verified_here': False})
    return out


def _check_events(events, index) -> int:
    if not isinstance(events, list):
        raise Problem('Mirror notices must be a list')
    previous, withdrawn = {}, {}
    for event in events:
        validate_event(event)
        work, rev = index.get(event['exploration_id']), index.get(event['revision_id'])
        if (work is None or work['type'] != 'exploration' or rev is None or rev['type'] != 'revision'
                or rev['payload']['exploration_id'] != work['id'] or event['actor_id'] != work['actor_id']
                or event['previous_event_id'] != previous.get(work['id'])):
            raise Problem(f'Mirror notice {event["id"]} does not fit its work history')
        state = withdrawn.get(work['id'], False)
        if (event['kind'] == 'withdraw' and state) or (event['kind'] == 'reinstate' and not state):
            raise Problem(f'Mirror notice {event["id"]}: invalid withdrawal sequence')
        if event['kind'] != 'note':
            withdrawn[work['id']] = event['kind'] == 'withdraw'
        previous[work['id']] = event['id']
    return len(events)


def verify_mirror(directory: str | Path) -> dict:
    """Offline check of a pulled mirror directory. Raises Problem on inconsistency."""
    root = Path(directory)
    raw = (root / 'manifest.json').read_bytes()
    if len(raw) > MAX_MANIFEST:
        raise Problem('Mirror manifest is too large')
    value = json.loads(raw)
    optional = {'analysis', 'redactions'} & set(value)
    exact(value, {'format', 'generated_at', 'origin', 'records', 'chain', 'anchors', 'publications', 'excluded', 'signatures'} | optional)
    if value['format'] not in (FORMAT, FORMAT_V2):
        raise Problem('Unsupported mirror format')
    rows = _check_records(value['records'])
    if value['format'] == FORMAT and (any(r['type'] == 'material' for r in rows) or 'redactions' in value):
        raise Problem('Material records require mirror v2')
    index = {r['id']: r for r in rows}
    chain = _check_chain(value['chain'], rows)
    anchors = _check_anchors(value['anchors'], value['chain']['entries'])
    pubs = value['publications']
    exact(pubs, {'snapshots', 'events', 'package_path'})
    verified, missing = 0, []
    for item in pubs['snapshots']:
        exact(item, SNAPSHOT_FIELDS)
        identifier(item['id'])
        path = root / 'publications' / (item['id'] + '.json')
        if not path.is_file():
            missing.append(item['id'])
            continue
        if path.stat().st_size > MAX_PACKAGE:
            raise Problem(f'Snapshot package {item["id"]} is too large')
        package = json.loads(path.read_bytes())
        verify_package(package, item['sha256'])
        snap = validate_snapshot(package['snapshot'])
        if (snap['id'] != item['id'] or index.get(snap['revision']['id']) != snap['revision']
                or index.get(snap['exploration']['id']) != snap['exploration']
                or item['exploration_id'] != snap['exploration']['id'] or item['revision_id'] != snap['revision']['id']):
            raise Problem(f'Snapshot {item["id"]} does not match the mirrored records')
        verified += 1
    notices = _check_events(pubs['events'], index)
    analysis = _check_analysis(value['analysis'], index) if 'analysis' in optional else {'reports': 0, 'responses': 0}
    redactions = _check_redactions(value['redactions'], index) if 'redactions' in optional else 0
    return {
        'format': value['format'], 'origin': value['origin'], 'generated_at': value['generated_at'],
        'records': len(rows), 'chain': chain, 'anchors': anchors,
        'snapshots': {'listed': len(pubs['snapshots']), 'verified': verified, 'missing': missing},
        'notices': notices,
        'analysis': analysis,
        'redactions': redactions,
        'internally_consistent': True,
        'complete': not missing,
        'authorship_signed': False,
        'external_time_verified': False,
        'completeness_vs_operator_known': False,
        'next_steps': ['ots verify -f anchors/<id>.bin anchors/<id>.bin.ots (needs your own Bitcoin node) to confirm an anchor',
                       'compare chain.head with an anchored head or another mirror to detect truncation'],
    }


def _write_anchor_files(root: Path, value: dict) -> None:
    """anchors/<id>.bin (the 32 stamped bytes) and <id>.bin.ots, ready for ``ots verify``."""
    (root / 'anchors').mkdir(mode=0o755)
    for anchor in value['anchors']:
        if type(anchor.get('id')) is not int:
            raise Problem('Invalid anchor identifier')
        fingerprint(anchor['head'])
        (root / 'anchors' / f'{anchor["id"]}.bin').write_bytes(bytes.fromhex(anchor['head']))
        (root / 'anchors' / f'{anchor["id"]}.bin.ots').write_bytes(base64.b64decode(anchor['proof_b64'], validate=True))


def _fetch(url: str, limit: int) -> bytes:
    request = urllib.request.Request(url, headers={'User-Agent': 'eksmu-mirror/1', 'Accept': 'application/json'})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise Problem(f'Response from {url} exceeds the size limit')
    return data


def pull(origin: str, directory: str | Path) -> dict:
    """Download a mirror from ``origin`` into a NEW directory, then verify it offline."""
    parts = urlsplit(origin)
    local = parts.hostname in {'127.0.0.1', 'localhost', '::1'}
    if parts.scheme not in {'https', 'http'} or (parts.scheme == 'http' and not local) or parts.path not in {'', '/'} \
            or parts.query or parts.fragment or not parts.hostname:
        raise Problem('Origin must be an https:// site root (http only for loopback)')
    base = f'{parts.scheme}://{parts.netloc}'
    root = Path(directory)
    root.mkdir(mode=0o755)
    raw = _fetch(base + '/api/mirror', MAX_MANIFEST)
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get('format') not in (FORMAT, FORMAT_V2):
        raise Problem('The origin did not return a mirror manifest')
    (root / 'manifest.json').write_bytes(raw)
    _write_anchor_files(root, value)
    (root / 'publications').mkdir(mode=0o755)
    for item in value['publications']['snapshots']:
        identifier(item['id'])
        data = _fetch(base + '/api/publications/export?' + urlencode({'snapshot_id': item['id']}), MAX_PACKAGE)
        (root / 'publications' / (item['id'] + '.json')).write_bytes(data)
    return verify_mirror(root)


def write_local(store, database, directory: str | Path) -> dict:
    """Operator-side: write a mirror of a local instance directly, without HTTP."""
    from .publications import Publications
    root = Path(directory)
    root.mkdir(mode=0o755)
    value = manifest(store, database)
    (root / 'manifest.json').write_text(canonical(value) + '\n', encoding='utf-8')
    _write_anchor_files(root, value)
    (root / 'publications').mkdir(mode=0o755)
    if value['publications']['snapshots']:
        pubs = Publications(str(database) + '.publications.sqlite', store.rows())
        try:
            for item in value['publications']['snapshots']:
                (root / 'publications' / (item['id'] + '.json')).write_text(
                    canonical(pubs.export(item['id'])) + '\n', encoding='utf-8')
        finally:
            pubs.close()
    return verify_mirror(root)
