"""Append-only hash chain over instance records, and its external time anchors.

Every record already carries ``sha256`` (canonical JSON of the record without
that field). The chain folds those digests, in storage order, into a running
head::

    head_0 = 32 zero bytes
    head_n = SHA256(UTF8("eksmu.record-chain.v1") || 0x00 || head_{n-1} || record_sha256_n)

The chain lives beside the records (same SQLite file, written in the same
transaction), so the record format, exports and imports are unchanged.

What it proves: once a head has been published outside this instance (for
example in an OpenTimestamps proof), any later edit, removal or reordering of
the records it covers changes that head and is detectable. On its own, an
operator with database access could rebuild the whole chain; the chain is a
commitment device, not a timestamp. Records chained by ``backfill`` were added
to the chain after they were written; only the platform clock vouches for
their earlier times.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json

from .store import Problem, digest

FORMAT = 'eksmu.record-chain.v1'
DOMAIN = b'eksmu.record-chain.v1\x00'
GENESIS = '0' * 64
TABLES = {'record_chain', 'chain_anchors'}
# Record types whose full JSON is already public on the site. Actor records
# stay out of proofs: their payload holds a credential fingerprint.
PROOF_TYPES = {'exploration', 'revision', 'material'}
MAX_ENTRIES_PAGE = 1000


def fold(previous_head: str, record_sha256: str) -> str:
    return hashlib.sha256(DOMAIN + bytes.fromhex(previous_head) + bytes.fromhex(record_sha256)).hexdigest()


def ensure(db) -> None:
    db.execute('CREATE TABLE IF NOT EXISTS record_chain(seq INTEGER PRIMARY KEY, record_id TEXT UNIQUE NOT NULL, '
               'record_sha256 TEXT NOT NULL, head TEXT NOT NULL, chained_at TEXT NOT NULL, backfilled INTEGER NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS chain_anchors(id INTEGER PRIMARY KEY, seq INTEGER NOT NULL, head TEXT NOT NULL, '
               'method TEXT NOT NULL, submitted_at TEXT NOT NULL, state TEXT NOT NULL, proof BLOB NOT NULL, '
               'bitcoin TEXT, checked TEXT, updated_at TEXT NOT NULL)')


def tip(db) -> tuple[int, str]:
    row = db.execute('SELECT seq, head FROM record_chain ORDER BY seq DESC LIMIT 1').fetchone()
    return (row[0], row[1]) if row else (0, GENESIS)


def append(db, rows: list[dict], *, backfilled: bool = False) -> None:
    """Chain new records. The caller holds the write transaction that stores them."""
    seq, head = tip(db)
    now = datetime.now(timezone.utc).isoformat()
    for row in rows:
        seq += 1
        head = fold(head, row['sha256'])
        db.execute('INSERT INTO record_chain VALUES(?,?,?,?,?,?)',
                   (seq, row['id'], row['sha256'], head, now, int(backfilled)))


def backfill(db) -> int:
    """Chain records written before the chain existed (or by an older version)."""
    max_seq = (db.execute('SELECT seq FROM record_chain ORDER BY seq DESC LIMIT 1').fetchone() or (0,))[0]
    max_rowid = (db.execute('SELECT rowid FROM records ORDER BY rowid DESC LIMIT 1').fetchone() or (0,))[0]
    if max_rowid <= max_seq:
        return 0
    chained = db.execute('SELECT COUNT(*) FROM record_chain').fetchone()[0]
    if db.execute('SELECT COUNT(*) FROM records').fetchone()[0] <= chained:
        return 0
    from .accounts import transaction
    with transaction(db):
        chained = db.execute('SELECT COUNT(*) FROM record_chain').fetchone()[0]
        missing = [json.loads(r[0]) for r in
                   db.execute('SELECT document FROM records ORDER BY rowid LIMIT -1 OFFSET ?', (chained,))]
        append(db, missing, backfilled=True)
    return len(missing)


def verify(db) -> dict:
    """Recompute every record digest and chain head; raise Problem on any mismatch."""
    records = db.execute('SELECT document FROM records ORDER BY rowid').fetchall()
    chain = db.execute('SELECT seq, record_id, record_sha256, head FROM record_chain ORDER BY seq').fetchall()
    if len(chain) != len(records):
        raise Problem('Record chain length does not match the records', 409)
    head = GENESIS
    for expected_seq, ((raw,), (seq, record_id, record_sha256, stored)) in enumerate(zip(records, chain), 1):
        row = json.loads(raw)
        if seq != expected_seq or row['id'] != record_id:
            raise Problem(f'Record chain order differs at entry {expected_seq}', 409)
        if digest({k: v for k, v in row.items() if k != 'sha256'}) != row['sha256'] or row['sha256'] != record_sha256:
            raise Problem(f'Record digest differs from the chain at entry {expected_seq}', 409)
        head = fold(head, record_sha256)
        if head != stored:
            raise Problem(f'Record chain head mismatch at entry {expected_seq}', 409)
    for anchor_id, seq, anchored in db.execute('SELECT id, seq, head FROM chain_anchors ORDER BY id'):
        if not 1 <= seq <= len(chain) or chain[seq - 1][3] != anchored:
            raise Problem(f'Anchor {anchor_id} does not match the record chain', 409)
    return {'format': FORMAT, 'length': len(chain), 'head': head, 'consistent': True}


def _anchor(row) -> dict:
    anchor_id, seq, head, method, submitted_at, state, bitcoin, checked = row
    return {'id': anchor_id, 'seq': seq, 'head': head, 'method': method, 'submitted_at': submitted_at,
            'state': state, 'bitcoin': json.loads(bitcoin) if bitcoin else None,
            'checked': json.loads(checked) if checked else None,
            'proof_url': f'/api/chain/anchor-proof?id={anchor_id}',
            'stamped_file_url': f'/api/chain/anchor-file?id={anchor_id}'}


ANCHOR_COLUMNS = 'id, seq, head, method, submitted_at, state, bitcoin, checked'


def status(db, limit: int = 30) -> dict:
    length, head = tip(db)
    backfilled = db.execute('SELECT COUNT(*) FROM record_chain WHERE backfilled=1').fetchone()[0]
    anchors = [_anchor(r) for r in db.execute(
        f'SELECT {ANCHOR_COLUMNS} FROM chain_anchors ORDER BY id DESC LIMIT ?', (limit,))]
    return {'format': FORMAT, 'length': length, 'head': head, 'backfilled_entries': backfilled,
            'anchors': anchors, 'algorithm': ALGORITHM}


def entries(db, start: int, limit: int) -> dict:
    if not 1 <= start <= 10_000_000 or not 1 <= limit <= MAX_ENTRIES_PAGE:
        raise Problem('分页参数不正确')
    rows = db.execute('SELECT seq, record_id, record_sha256, head, chained_at, backfilled FROM record_chain '
                      'WHERE seq >= ? ORDER BY seq LIMIT ?', (start, limit)).fetchall()
    return {'format': FORMAT, 'entries': [
        {'seq': s, 'record_id': r, 'record_sha256': d, 'head': h, 'chained_at': c, 'backfilled': bool(b)}
        for s, r, d, h, c, b in rows]}


def anchor_for(db, seq: int):
    """The earliest anchor covering ``seq``, preferring one already in a Bitcoin block."""
    rows = db.execute(f'SELECT {ANCHOR_COLUMNS} FROM chain_anchors WHERE seq >= ? ORDER BY id', (seq,)).fetchall()
    confirmed = [r for r in rows if r[5] == 'confirmed']
    chosen = (confirmed or rows or [None])[0]
    return _anchor(chosen) if chosen else None


def record_proof(db, record_id: str) -> dict:
    found = db.execute('SELECT c.seq, c.chained_at, c.backfilled, r.document FROM record_chain c '
                       'JOIN records r ON r.id = c.record_id WHERE c.record_id = ?', (record_id,)).fetchone()
    if found is None:
        raise Problem('找不到对应的记录', 404)
    seq, chained_at, backfilled, raw = found
    record = json.loads(raw)
    if record['type'] not in PROOF_TYPES:
        raise Problem('找不到对应的记录', 404)
    previous = db.execute('SELECT head FROM record_chain WHERE seq = ?', (seq - 1,)).fetchone()
    anchor = anchor_for(db, seq)
    following = []
    if anchor:
        following = [r[0] for r in db.execute('SELECT record_sha256 FROM record_chain WHERE seq > ? AND seq <= ? '
                                              'ORDER BY seq', (seq, anchor['seq']))]
    return {'format': 'eksmu.record-proof.v1', 'record': record, 'seq': seq, 'chained_at': chained_at,
            'backfilled': bool(backfilled), 'previous_head': previous[0] if previous else GENESIS,
            'following_record_sha256': following, 'anchor': anchor, 'algorithm': ALGORITHM}


def summary(db, record_id: str) -> dict | None:
    """Compact facts for a work page: chain position and best anchor."""
    found = db.execute('SELECT seq FROM record_chain WHERE record_id = ?', (record_id,)).fetchone()
    if found is None:
        return None
    return {'seq': found[0], 'anchor': anchor_for(db, found[0])}


ALGORITHM = {
    'record_sha256': 'SHA-256 of the record without its "sha256" field, as JSON with sorted keys, '
                     'separators "," and ":", UTF-8, non-ASCII characters unescaped',
    'chain': 'head_0 = 32 zero bytes; head_n = SHA-256("eksmu.record-chain.v1" || 0x00 || head_(n-1) || record_sha256_n), '
             'hashes as raw 32-byte values',
    'record_proof': 'start from previous_head, fold the record digest, then each following_record_sha256 in order; '
                    'the result must equal anchor.head',
    'opentimestamps': 'the stamped file is the 32 raw bytes of anchor.head; '
                      'verify with: ots verify -f <stamped file> <proof .ots> (needs a Bitcoin node)',
}
