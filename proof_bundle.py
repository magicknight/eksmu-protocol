#!/usr/bin/env python3
"""Create and check a minimal local chain witness from a verified v1 mirror.

Usage: python tools/proof_bundle.py create MIRROR_DIR NEW_FILE
       python tools/proof_bundle.py verify FILE [--checkpoint-seq N --checkpoint-head HEX]

The bundle contains only chain record digests, chain heads, and SHA-256 digests
of the opaque OpenTimestamps proof bytes. It excludes records, identifiers,
author names, timestamps and proof bytes. It never uploads or deletes data.
Proof digests do not validate the proof or establish Bitcoin confirmation.
Digest-only data may still be linked to guessed or previously known material.
"""
import argparse
import base64
import hashlib
import json
import sys
from pathlib import Path

try:
    from . import eksmu_verify as mirror
except ImportError:
    import eksmu_verify as mirror

FORMAT = 'eksmu.chain-witness.v1'
MAX_BYTES = 64 * 1024 * 1024


def require(value, msg):
    if not value:
        raise ValueError(msg)


def read_json(path):
    with Path(path).open('rb') as f:
        data = f.read(MAX_BYTES + 1)
    require(len(data) <= MAX_BYTES, 'file too large')
    return json.loads(data)


def create(directory):
    # Refuse a source that does not pass the established mirror checks, including
    # missing publication packages. Mirror verification does not prove time.
    manifest_path = Path(directory) / 'manifest.json'
    manifest = read_json(manifest_path)
    result = mirror.verify(directory)
    require(result['complete'], 'source mirror has missing publication packages')
    require(manifest == read_json(manifest_path), 'source manifest changed during verification')
    prefix = mirror.mirror_prefix(manifest)
    witness = {
        'format': FORMAT,
        'chain_format': f'{prefix}.record-chain.v1',
        'entries': [{'record_sha256': e['record_sha256'], 'head': e['head']} for e in manifest['chain']['entries']],
        'anchors': [{'seq': a['seq'], 'head': a['head'],
                     'proof_sha256': hashlib.sha256(base64.b64decode(a['proof_b64'], validate=True)).hexdigest()}
                    for a in manifest['anchors']],
    }
    verify(witness)
    return witness


def verify(bundle, checkpoint=None):
    require(isinstance(bundle, dict) and set(bundle) == {'format', 'chain_format', 'entries', 'anchors'}, 'bad witness envelope')
    require(bundle['format'] == FORMAT and bundle['chain_format'] in ('eksmu.record-chain.v1', 'oic.record-chain.v1'), 'unknown witness format')
    entries, anchors = bundle['entries'], bundle['anchors']
    require(isinstance(entries, list) and isinstance(anchors, list), 'invalid entries or anchors')
    prefix = bundle['chain_format'].split('.')[0]
    head = '0' * 64
    for seq, e in enumerate(entries, 1):
        require(isinstance(e, dict) and set(e) == {'record_sha256', 'head'} and mirror.is_hex64(e['record_sha256']) and mirror.is_hex64(e['head']), f'invalid entry {seq}')
        head = mirror.fold(head, e['record_sha256'], prefix)
        require(head == e['head'], f'chain mismatch at entry {seq}')
    for a in anchors:
        require(isinstance(a, dict) and set(a) == {'seq', 'head', 'proof_sha256'}, 'invalid anchor')
        seq = a['seq']
        require(type(seq) is int and 1 <= seq <= len(entries) and mirror.is_hex64(a['head']) and mirror.is_hex64(a['proof_sha256']), 'invalid anchor fields')
        require(entries[seq - 1]['head'] == a['head'], 'anchor does not match chain')
    if checkpoint is not None:
        seq, expected = checkpoint
        require(type(seq) is int and 1 <= seq <= len(entries) and mirror.is_hex64(expected), 'invalid or truncated checkpoint')
        require(entries[seq - 1]['head'] == expected, 'checkpoint mismatch')
    return {'chain_hash_consistent': True, 'length': len(entries), 'head': head,
            'anchors_bound_to_chain': len(anchors), 'checkpoint_matched': checkpoint is not None,
            'proof_bytes_included': False, 'proofs_verified': False, 'bitcoin_verified': False,
            'records_verified': False, 'completeness_against_operator_verified': False,
            'limitations': [
                'Anchor fields bind to the chain, but proof digests are unauthenticated claims and no OTS proof is present or validated.',
                'Check the actual OTS file and its digest separately, then verify the OTS proof with an independent Bitcoin node.',
                'A checkpoint is useful only when obtained and retained independently, and does not authenticate later entries.',
                'Digests can identify or confirm guesses about known content; this is not a guarantee of anonymity.',
            ]}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    create_cmd = sub.add_parser('create')
    create_cmd.add_argument('mirror_directory')
    create_cmd.add_argument('new_file')
    verify_cmd = sub.add_parser('verify')
    verify_cmd.add_argument('file')
    verify_cmd.add_argument('--checkpoint-seq', type=int)
    verify_cmd.add_argument('--checkpoint-head')
    args = p.parse_args()
    try:
        if args.command == 'create':
            bundle = create(args.mirror_directory)
            encoded = json.dumps(bundle, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n'
            require(len(encoded.encode('utf-8')) <= MAX_BYTES, 'witness too large')
            with open(args.new_file, 'x', encoding='utf-8') as out:
                out.write(encoded)
            report = verify(bundle)
        else:
            require((args.checkpoint_seq is None) == (args.checkpoint_head is None), 'checkpoint requires both sequence and head')
            cp = None if args.checkpoint_seq is None else (args.checkpoint_seq, args.checkpoint_head)
            report = verify(read_json(args.file), cp)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, TypeError, KeyError, OSError, mirror.Invalid) as exc:
        print(f'WITNESS FAILED: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
