"""Anchor record chain heads with OpenTimestamps (run as a separate job).

Only the 32-byte chain head leaves the machine, as the digest the ``ots``
client derives from it (the client adds its own random nonce). No record
content, names, emails or credentials are sent. The publishing service never
imports this module and never waits for it.

States: ``pending`` (calendars accepted the digest; Bitcoin has not confirmed
it yet) and ``confirmed`` (the proof contains a Bitcoin block header
attestation). ``checked`` records whether that block's Merkle root was compared
with public block explorers. That comparison is not a full-node verification;
anyone can run ``ots verify`` against their own node with the published files.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import urllib.request

from . import record_chain
from .maintenance import InstanceLock
from .store import Problem

METHOD = 'opentimestamps'
EXPLORERS = ('https://mempool.space/api', 'https://blockstream.info/api')
MAX_PROOF = 64 * 1024
CLIENT_TIMEOUT = 120


def _now():
    return datetime.now(timezone.utc)


def parse_proof(data: bytes, head: str) -> dict:
    """Return the Bitcoin attestations in an .ots proof for this head's stamped file."""
    from opentimestamps.core.notary import BitcoinBlockHeaderAttestation, PendingAttestation
    from opentimestamps.core.op import OpSHA256
    from opentimestamps.core.serialize import BytesDeserializationContext
    from opentimestamps.core.timestamp import DetachedTimestampFile
    if not 0 < len(data) <= MAX_PROOF:
        raise Problem('OpenTimestamps proof has an unexpected size')
    proof = DetachedTimestampFile.deserialize(BytesDeserializationContext(data))
    if not isinstance(proof.file_hash_op, OpSHA256) or proof.file_digest != hashlib.sha256(bytes.fromhex(head)).digest():
        raise Problem('OpenTimestamps proof is not for this chain head')
    blocks, calendars = [], []
    for message, attestation in proof.timestamp.all_attestations():
        if isinstance(attestation, BitcoinBlockHeaderAttestation):
            # Block headers store the Merkle root little-endian; explorers print it reversed.
            blocks.append({'height': attestation.height, 'merkle_root': message[::-1].hex()})
        elif isinstance(attestation, PendingAttestation):
            calendars.append(attestation.uri)
    blocks.sort(key=lambda item: item['height'])
    return {'blocks': blocks, 'calendars': sorted(set(calendars))}


def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={'User-Agent': 'eksmu-chain-anchor'})
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read(65536)


def check_block(height: int, merkle_root: str, explorers=EXPLORERS, fetch=_get) -> dict:
    """Compare the attested Merkle root with each explorer's block header."""
    results = []
    for base in explorers:
        try:
            block_hash = fetch(f'{base}/block-height/{int(height)}').decode('ascii').strip()
            block = json.loads(fetch(f'{base}/block/{block_hash}'))
            results.append({'source': base.split('/')[2], 'block_hash': block_hash, 'time': block['timestamp'],
                            'match': block['merkle_root'] == merkle_root})
        except Exception as exc:  # explorer outages must not fail the job
            results.append({'source': base.split('/')[2], 'error': type(exc).__name__})
    matched = [r for r in results if r.get('match')]
    return {'at': _now().isoformat(), 'results': results, 'matched': len(matched),
            'mismatch': any(r.get('match') is False for r in results),
            'block_time': datetime.fromtimestamp(matched[0]['time'], timezone.utc).isoformat() if matched else None,
            'method': 'public block explorers (not a full Bitcoin node)'}


def _run_client(ots: str, args: list[str], cwd: str):
    result = subprocess.run([ots, *args], cwd=cwd, capture_output=True, text=True, timeout=CLIENT_TIMEOUT,
                            env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'HOME': cwd})
    return result.returncode, (result.stdout + result.stderr)[-2000:]


def stamp(ots: str, head: str) -> bytes:
    with tempfile.TemporaryDirectory() as work:
        target = Path(work) / 'head.bin'
        target.write_bytes(bytes.fromhex(head))
        code, output = _run_client(ots, ['stamp', target.name], work)
        proof = target.with_name('head.bin.ots')
        if code or not proof.is_file():
            raise Problem('OpenTimestamps stamp failed: ' + output.strip(), 502)
        data = proof.read_bytes()
    parse_proof(data, head)
    return data


def upgrade(ots: str, head: str, proof: bytes) -> bytes:
    with tempfile.TemporaryDirectory() as work:
        (Path(work) / 'head.bin').write_bytes(bytes.fromhex(head))
        target = Path(work) / 'head.bin.ots'
        target.write_bytes(proof)
        # Exit status is non-zero while the proof is still pending; that is not an error.
        _run_client(ots, ['upgrade', target.name], work)
        data = target.read_bytes()
    parse_proof(data, head)
    return data


def _writing(database):
    # A shared instance lease, like the running server's: an offline backup
    # (exclusive) never copies the database while this job writes. Network
    # calls happen outside the lease.
    return InstanceLock(database, wait_seconds=60)


def run(database: str | Path, ots: str, *, min_interval=timedelta(hours=20), explorers=EXPLORERS,
        fetch=_get, stamper=stamp, upgrader=upgrade) -> dict:
    """One anchoring pass: upgrade pending proofs, re-check, then stamp a new head if due."""
    with _writing(database):
        db = sqlite3.connect(str(database), timeout=30)
        record_chain.ensure(db)
        db.commit()
        db.close()
    db = sqlite3.connect(str(database), timeout=30)
    try:
        report = {'upgraded': [], 'checked': [], 'stamped': None, 'errors': []}
        for anchor_id, head, state, proof, checked in db.execute(
                'SELECT id, head, state, proof, checked FROM chain_anchors WHERE state = ? OR checked IS NULL '
                'OR json_extract(checked, "$.matched") = 0 ORDER BY id', ('pending',)).fetchall():
            try:
                if state == 'pending':
                    proof = upgrader(ots, head, proof)
                parsed = parse_proof(proof, head)
                bitcoin = parsed['blocks'][0] if parsed['blocks'] else None
                new_state = 'confirmed' if bitcoin else 'pending'
                verdict = check_block(bitcoin['height'], bitcoin['merkle_root'], explorers, fetch) if bitcoin else None
                if verdict and verdict['mismatch']:
                    report['errors'].append({'anchor': anchor_id, 'error': 'explorer Merkle root mismatch'})
                with _writing(database), db:
                    db.execute('UPDATE chain_anchors SET proof=?, state=?, bitcoin=?, checked=?, updated_at=? WHERE id=?',
                               (proof, new_state, json.dumps(bitcoin) if bitcoin else None,
                                json.dumps(verdict) if verdict else None, _now().isoformat(), anchor_id))
                if new_state != state:
                    report['upgraded'].append(anchor_id)
                if verdict:
                    report['checked'].append({'anchor': anchor_id, 'matched': verdict['matched']})
            except Exception as exc:
                report['errors'].append({'anchor': anchor_id, 'error': str(exc)[:300]})
        seq, head = record_chain.tip(db)
        last = db.execute('SELECT seq, submitted_at FROM chain_anchors ORDER BY id DESC LIMIT 1').fetchone()
        due = seq > 0 and (last is None or (last[0] != seq and
                                            _now() - datetime.fromisoformat(last[1]) >= min_interval))
        if due:
            try:
                proof = stamper(ots, head)
                now = _now().isoformat()
                with _writing(database), db:
                    cursor = db.execute('INSERT INTO chain_anchors(seq, head, method, submitted_at, state, proof, '
                                        'updated_at) VALUES(?,?,?,?,?,?,?)', (seq, head, METHOD, now, 'pending', proof, now))
                report['stamped'] = {'anchor': cursor.lastrowid, 'seq': seq, 'head': head}
            except Exception as exc:
                report['errors'].append({'stamp': str(exc)[:300]})
        return report
    finally:
        db.close()
