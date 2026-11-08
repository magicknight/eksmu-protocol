#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Experimental offline chain-fold check for redacted records; NOT a mirror verifier.

Input is a synthetic or manually prepared JSON object using
`eksmu.redacted-chain-experiment.v1`. This tool is read-only and never deletes
or synchronizes content. It checks hash continuity, not record references,
authority of redaction decisions, Bitcoin proofs, or mirror completeness.
A prior independently saved seq/head pair can detect replacement or truncation
up to that point. No current server exports this experimental format.
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

MAX_BYTES = 64 * 1024 * 1024
HEX = set('0123456789abcdef')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def is_hash(value):
    return isinstance(value, str) and len(value) == 64 and set(value) <= HEX


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def verify(document, checkpoint=None):
    require(isinstance(document, dict) and set(document) == {'format', 'entries', 'redactions'}, 'bad envelope')
    require(document['format'] == 'eksmu.redacted-chain-experiment.v1', 'unknown format')
    entries, notices = document['entries'], document['redactions']
    require(isinstance(entries, list) and isinstance(notices, list), 'entries and redactions must be lists')
    redacted = {}
    for notice in notices:
        require(isinstance(notice, dict) and set(notice) == {'seq', 'record_id', 'record_sha256', 'reason_code'}, 'bad redaction declaration')
        seq = notice['seq']
        require(type(seq) is int and 1 <= seq <= len(entries) and seq not in redacted, 'bad or duplicate redaction sequence')
        require(isinstance(notice['record_id'], str) and notice['record_id'], 'bad redaction record id')
        require(is_hash(notice['record_sha256']), 'bad redaction digest')
        require(isinstance(notice['reason_code'], str) and notice['reason_code'] and len(notice['reason_code']) <= 64, 'bad reason code')
        redacted[seq] = notice
    head, seen = '0' * 64, set()
    for seq, entry in enumerate(entries, 1):
        require(isinstance(entry, dict) and set(entry) == {'seq', 'record_id', 'record_sha256', 'head', 'record'}, 'bad entry')
        require(type(entry['seq']) is int and entry['seq'] == seq, 'nonconsecutive sequence')
        rid, sha = entry['record_id'], entry['record_sha256']
        require(isinstance(rid, str) and rid and rid not in seen and is_hash(sha), 'bad or duplicate record')
        seen.add(rid)
        row = entry['record']
        if row is None:
            notice = redacted.get(seq)
            require(notice is not None and notice['record_id'] == rid and notice['record_sha256'] == sha, 'missing matching redaction declaration')
        else:
            require(seq not in redacted, 'redacted record still present')
            require(isinstance(row, dict) and row.get('id') == rid and row.get('sha256') == sha, 'record does not match entry')
            require(hashlib.sha256(canonical({k: v for k, v in row.items() if k != 'sha256'})).hexdigest() == sha, 'record digest mismatch')
        head = hashlib.sha256(b'eksmu.record-chain.v1\x00' + bytes.fromhex(head) + bytes.fromhex(sha)).hexdigest()
        require(entry['head'] == head, 'chain head mismatch')
    if checkpoint is not None:
        seq, expected = checkpoint
        require(type(seq) is int and 1 <= seq <= len(entries) and is_hash(expected), 'invalid or truncated checkpoint')
        require(entries[seq - 1]['head'] == expected, 'checkpoint mismatch')
    return {
        'format': 'eksmu.redacted-chain-experiment-result.v1',
        'chain_hash_consistent': True,
        'chain_length': len(entries), 'head': head,
        'checkpoint_matched': checkpoint is not None,
        'record_bodies_complete': not redacted,
        'redacted_sequences': sorted(redacted),
        'record_references_verified': False,
        'redaction_authority_verified': False,
        'bitcoin_time_verified': False,
        'complete_against_operator': False,
        'limitations': [
            'Experimental chain hash check only; record schema, references and publication packages are not verified.',
            'Redaction declarations are untrusted and do not authorize deletion or establish why content was removed.',
            'Chain folding commits only the record digest; missing record IDs, redaction metadata and reasons are not authenticated by it.',
            'A checkpoint is useful only when obtained and retained independently; it does not authenticate later entries.',
            'Hashes may reveal low-entropy content through guessing; deletion does not erase copies held by others.',
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path')
    parser.add_argument('--checkpoint-seq', type=int)
    parser.add_argument('--checkpoint-head')
    args = parser.parse_args()
    try:
        require((args.checkpoint_seq is None) == (args.checkpoint_head is None), 'checkpoint requires both sequence and head')
        with Path(args.path).open('rb') as source:
            raw = source.read(MAX_BYTES + 1)
        require(len(raw) <= MAX_BYTES, 'input too large')
        document = json.loads(raw)
        checkpoint = None if args.checkpoint_seq is None else (args.checkpoint_seq, args.checkpoint_head)
        print(json.dumps(verify(document, checkpoint), ensure_ascii=False, indent=2))
    except (ValueError, TypeError, OSError) as exc:
        print(f'VERIFICATION FAILED: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
