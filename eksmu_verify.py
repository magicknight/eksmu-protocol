#!/usr/bin/env python3
"""Independent verifier for Eksmu public mirrors. Python 3.9+, standard library only.

    python3 eksmu_verify.py pull https://eksmu.com ./mirror   # download, then verify
    python3 eksmu_verify.py verify ./mirror                   # offline re-check

This file is deliberately self-contained so that anyone can audit and run it
without the Eksmu server code. It implements the integrity rules in SPEC.md.
It does not judge content and does not contact Bitcoin; see the report's
"not_verified" list for what a passing result does NOT mean.
"""
import base64
import hashlib
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

MIRROR = 'oic.public-mirror.v1'
RECORD_BUNDLES = {'oic.bundle.v1', 'oic.publication-bundle.v1'}
CHAIN = 'oic.record-chain.v1'
SNAPSHOT = 'oic.publication-snapshot.v1'
PACKAGE = 'oic.publication-package.v1'
EVENT = 'oic.publication-event.v1'
GENESIS = '0' * 64
RECORD_TYPES = {'actor', 'exploration', 'revision', 'comment', 'evidence'}
MAX_MANIFEST = 64 * 1024 * 1024
MAX_PACKAGE = 90 * 1024 * 1024


class Invalid(ValueError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def self_digest_ok(obj):
    return isinstance(obj, dict) and obj.get('sha256') == digest({k: v for k, v in obj.items() if k != 'sha256'})


def fold(previous_head, record_sha256):
    return hashlib.sha256(b'oic.record-chain.v1\x00' + bytes.fromhex(previous_head)
                          + bytes.fromhex(record_sha256)).hexdigest()


def require(condition, message):
    if not condition:
        raise Invalid(message)


def is_hex64(value):
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def check_records(bundle):
    require(isinstance(bundle, dict) and set(bundle) == {'format', 'records', 'sha256'}, 'records: bad envelope')
    require(bundle['format'] in RECORD_BUNDLES, 'records: unknown format')
    rows = bundle['records']
    require(isinstance(rows, list) and bundle['sha256'] == digest(rows), 'records: bundle digest mismatch')
    index, heads = {}, {}
    for n, row in enumerate(rows, 1):
        require(isinstance(row, dict) and set(row) == {'id', 'type', 'actor_id', 'created_at', 'payload', 'sha256'},
                f'record {n}: bad fields')
        require(self_digest_ok(row), f'record {n}: digest mismatch')
        require(row['id'] not in index, f'record {n}: duplicate id')
        require(row['type'] in RECORD_TYPES, f'record {n}: unknown type')
        p = row['payload']
        require(isinstance(p, dict), f'record {n}: payload must be an object')
        if row['type'] == 'actor':
            require(row['actor_id'] == row['id'], f'record {n}: actor identity mismatch')
        else:
            author = index.get(row['actor_id'])
            require(author is not None and author['type'] == 'actor', f'record {n}: unknown author')
        if row['type'] == 'revision':
            work = index.get(p.get('exploration_id'))
            require(work is not None and work['type'] == 'exploration', f'record {n}: revision of unknown work')
            require(work['actor_id'] == row['actor_id'], f'record {n}: only the author may revise a work')
            require(p.get('parent_revision_id') == heads.get(work['id']), f'record {n}: revision out of order')
            heads[work['id']] = row['id']
        if row['type'] in {'comment', 'evidence'}:
            target = index.get(p.get('revision_id'))
            require(target is not None and target['type'] == 'revision', f'record {n}: target revision unknown')
        index[row['id']] = row
    require(all(r['id'] in heads for r in rows if r['type'] == 'exploration'), 'records: work without a revision')
    return rows, index


def check_chain(chain, rows):
    require(isinstance(chain, dict) and chain.get('format') == CHAIN, 'chain: unknown format')
    entries = chain.get('entries')
    require(isinstance(entries, list) and len(entries) == len(rows), 'chain: length differs from records')
    head = GENESIS
    for n, (entry, row) in enumerate(zip(entries, rows), 1):
        require(isinstance(entry, dict) and entry.get('seq') == n and entry.get('record_id') == row['id']
                and entry.get('record_sha256') == row['sha256'], f'chain: entry {n} does not match record {n}')
        head = fold(head, row['sha256'])
        require(entry.get('head') == head, f'chain: head mismatch at entry {n}')
    return {'length': len(entries), 'head': head,
            'backfilled_entries': sum(1 for e in entries if e.get('backfilled') is True)}


def check_anchors(anchors, entries):
    require(isinstance(anchors, list), 'anchors: must be a list')
    out = []
    for a in anchors:
        require(isinstance(a, dict), 'anchors: bad entry')
        seq = a.get('seq')
        require(type(seq) is int and 1 <= seq <= len(entries) and entries[seq - 1]['head'] == a.get('head'),
                f'anchor {a.get("id")}: does not bind to the chain')
        try:
            proof = base64.b64decode(a.get('proof_b64', ''), validate=True)
        except (ValueError, TypeError):
            raise Invalid(f'anchor {a.get("id")}: invalid proof encoding') from None
        require(proof, f'anchor {a.get("id")}: empty proof')
        out.append({'id': a.get('id'), 'seq': seq, 'head': a['head'], 'state_claimed': a.get('state'),
                    'proof_bytes': len(proof)})
    return out


def check_package(package, expected_sha256, index):
    require(isinstance(package, dict) and set(package) == {'format', 'snapshot', 'blobs'} and package['format'] == PACKAGE,
            'package: bad envelope')
    snap = package['snapshot']
    require(isinstance(snap, dict) and snap.get('format') == SNAPSHOT and self_digest_ok(snap), 'snapshot: digest mismatch')
    require(snap['sha256'] == expected_sha256, 'snapshot: differs from the mirror index')
    rev, work = snap.get('revision'), snap.get('exploration')
    require(index.get(rev.get('id')) == rev and index.get(work.get('id')) == work, 'snapshot: not identical to mirrored records')
    require(rev['payload']['exploration_id'] == work['id'] and snap['publisher']['id'] == work['actor_id'] == rev['actor_id'],
            'snapshot: authorship/reference mismatch')
    need = {m['sha256']: m['size'] for m in snap['materials']}
    got = {}
    for blob in package['blobs']:
        raw = base64.b64decode(blob['data_b64'], validate=True)
        require(hashlib.sha256(raw).hexdigest() == blob['sha256'] and blob['sha256'] in need and blob['sha256'] not in got,
                'snapshot: material digest mismatch or unexpected material')
        got[blob['sha256']] = len(raw)
    require(got == need, 'snapshot: missing or wrong-size material')
    return snap


def check_events(events, index):
    require(isinstance(events, list), 'notices: must be a list')
    previous, withdrawn = {}, {}
    for e in events:
        require(isinstance(e, dict) and e.get('format') == EVENT and self_digest_ok(e), 'notice: digest mismatch')
        work, rev = index.get(e.get('exploration_id')), index.get(e.get('revision_id'))
        require(work is not None and work['type'] == 'exploration' and rev is not None and rev['type'] == 'revision'
                and rev['payload']['exploration_id'] == work['id'] and e.get('actor_id') == work['actor_id'],
                f'notice {e.get("id")}: wrong work or author')
        require(e.get('previous_event_id') == previous.get(work['id']), f'notice {e.get("id")}: history out of order')
        state = withdrawn.get(work['id'], False)
        require(not (e['kind'] == 'withdraw' and state) and not (e['kind'] == 'reinstate' and not state),
                f'notice {e.get("id")}: invalid withdrawal sequence')
        if e['kind'] in {'withdraw', 'reinstate'}:
            withdrawn[work['id']] = e['kind'] == 'withdraw'
        previous[work['id']] = e['id']
    return {'notices': len(events), 'withdrawn_works': sorted(w for w, v in withdrawn.items() if v)}


def check_analysis(value, index):
    """AI analysis reports (oic.analysis-report.v1) and responses; optional in manifests."""
    require(isinstance(value, dict) and isinstance(value.get('reports'), list) and isinstance(value.get('responses'), list),
            'analysis: bad envelope')
    reports = {}
    for r in value['reports']:
        require(isinstance(r, dict) and r.get('format') == 'oic.analysis-report.v1' and self_digest_ok(r),
                f'analysis report {r.get("id") if isinstance(r, dict) else "?"}: digest mismatch')
        rev = index.get(r.get('revision_id'))
        require(rev is not None and rev['type'] == 'revision' and rev['sha256'] == r.get('revision_sha256'),
                f'analysis report {r["id"]}: does not match the revision it names')
        require([f.get('id') for f in r['findings']] == [f'F{n}' for n in range(1, len(r['findings']) + 1)],
                f'analysis report {r["id"]}: findings out of order')
        reports[r['id']] = r
    for v in value['responses']:
        require(isinstance(v, dict) and v.get('format') == 'oic.analysis-response.v1' and self_digest_ok(v),
                'analysis response: digest mismatch')
        report = reports.get(v.get('report_id'))
        require(report is not None and any(f['id'] == v.get('finding_id') for f in report['findings']),
                f'analysis response {v["id"]}: unknown finding')
        author = index[report['revision_id']]['actor_id']
        require(v.get('actor_id') in index and v.get('is_author') == (v['actor_id'] == author),
                f'analysis response {v["id"]}: wrong author flag')
    return {'reports': len(reports), 'responses': len(value['responses']),
            'quotes_not_found': sum(1 for r in reports.values() for f in r['findings'] if f.get('quote_verified') is False)}


def verify(directory):
    root = Path(directory)
    raw = (root / 'manifest.json').read_bytes()
    require(len(raw) <= MAX_MANIFEST, 'manifest too large')
    m = json.loads(raw)
    require(isinstance(m, dict) and m.get('format') == MIRROR, 'not an Eksmu mirror manifest')
    rows, index = check_records(m['records'])
    chain = check_chain(m['chain'], rows)
    anchors = check_anchors(m['anchors'], m['chain']['entries'])
    verified, missing = 0, []
    for item in m['publications']['snapshots']:
        sid = item['id']
        require(isinstance(sid, str) and '/' not in sid and '\\' not in sid and not sid.startswith('.'), 'bad snapshot id')
        path = root / 'publications' / (sid + '.json')
        if not path.is_file():
            missing.append(sid)
            continue
        require(path.stat().st_size <= MAX_PACKAGE, f'snapshot {sid}: too large')
        snap = check_package(json.loads(path.read_bytes()), item['sha256'], index)
        require(snap['id'] == sid and snap['revision']['id'] == item['revision_id'], f'snapshot {sid}: index mismatch')
        verified += 1
    notices = check_events(m['publications']['events'], index)
    analysis = check_analysis(m['analysis'], index) if 'analysis' in m else {'reports': 0, 'responses': 0, 'quotes_not_found': 0}
    return {
        'verifier': 'eksmu_verify.py', 'origin': m.get('origin'), 'generated_at': m.get('generated_at'),
        'records': len(rows), 'chain': chain, 'anchors': anchors,
        'snapshots': {'listed': len(m['publications']['snapshots']), 'verified': verified, 'missing': missing},
        **notices,
        'analysis': analysis,
        'internally_consistent': True, 'complete': not missing,
        'not_verified': [
            'authorship: records carry no author signatures yet',
            'time: anchors are not checked against Bitcoin here; run `ots verify -f anchors/<id>.bin anchors/<id>.bin.ots`',
            'completeness: a mirror proves only what it contains; compare chain.head with an anchored head or another mirror',
            'content: integrity says nothing about whether any research claim is correct',
            'analysis: AI reports are opinions bound to a version, not verdicts; their digests are checked, not their correctness',
        ],
    }


def fetch(url, limit):
    req = urllib.request.Request(url, headers={'User-Agent': 'eksmu-verify/1', 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=60) as response:
        data = response.read(limit + 1)
    require(len(data) <= limit, f'response too large: {url}')
    return data


def pull(origin, directory):
    parts = urllib.parse.urlsplit(origin)
    loopback = parts.hostname in {'127.0.0.1', 'localhost', '::1'}
    require(parts.scheme == 'https' or (parts.scheme == 'http' and loopback), 'origin must use https')
    require(parts.path in {'', '/'} and not parts.query and parts.hostname, 'origin must be a site root')
    base = f'{parts.scheme}://{parts.netloc}'
    root = Path(directory)
    root.mkdir()
    raw = fetch(base + '/api/mirror', MAX_MANIFEST)
    m = json.loads(raw)
    require(isinstance(m, dict) and m.get('format') == MIRROR, 'origin did not return a mirror manifest')
    (root / 'manifest.json').write_bytes(raw)
    (root / 'anchors').mkdir()
    for a in m['anchors']:
        require(type(a.get('id')) is int and is_hex64(a.get('head')), 'bad anchor entry')
        (root / 'anchors' / f'{a["id"]}.bin').write_bytes(bytes.fromhex(a['head']))
        (root / 'anchors' / f'{a["id"]}.bin.ots').write_bytes(base64.b64decode(a['proof_b64'], validate=True))
    (root / 'publications').mkdir()
    for item in m['publications']['snapshots']:
        sid = item['id']
        require(isinstance(sid, str) and sid.replace('-', '').isalnum(), 'bad snapshot id')
        data = fetch(base + '/api/publications/export?' + urllib.parse.urlencode({'snapshot_id': sid}), MAX_PACKAGE)
        (root / 'publications' / (sid + '.json')).write_bytes(data)
    return verify(root)


def main(argv):
    if len(argv) == 3 and argv[1] == 'verify':
        result = verify(argv[2])
    elif len(argv) == 4 and argv[1] == 'pull':
        result = pull(argv[2], argv[3])
    else:
        print(__doc__.strip().split('\n\n')[1], file=sys.stderr)
        return 64
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main(sys.argv))
    except (Invalid, AttributeError, KeyError, TypeError, ValueError, OSError) as exc:
        print(f'VERIFICATION FAILED: {type(exc).__name__}: {exc}', file=sys.stderr)
        sys.exit(1)
