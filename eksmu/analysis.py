"""AI public analysis (RFC 0031): per-finding, version-bound, rebuttable reports.

Invariants enforced here, not just documented:
- a report never affects whether a work is published or shown;
- there is no overall score, only located findings;
- every report names the exact revision (id and digest) it read;
- model "opinions" are kept apart from mechanical "checks";
- quotes a finding cites are checked against the revision text and flagged
  when they cannot be found (guards against invented citations);
- only the author can ask for an analysis, because it sends the text to an
  external service; anyone signed in can respond to any finding.

Storage is a sidecar ``<db>.analysis.sqlite``. The web server only queues
requests and records responses; reports are written by the separate worker
(``python -m eksmu run-analysis-queue``), which is the only process holding the
model API key.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import re
import sqlite3
import uuid

from .store import Problem, canonical, digest, enum, exact, identifier, text

REPORT = 'eksmu.analysis-report.v2'
REPORT_V1 = 'eksmu.analysis-report.v1'   # findings only (2026-10-11 morning); still valid
ASSESSMENTS = {'supported', 'partially_supported', 'not_established', 'not_assessed'}
MAX_CLAIMS = 8
MAX_POINTS = 12
RESPONSE = 'eksmu.analysis-response.v1'
TABLES = {'analysis_requests', 'analysis_reports', 'analysis_responses', 'analysis_spend', 'analysis_worker', 'analysis_keys'}
_V1_TABLES = TABLES - {'analysis_keys'}   # first deployment, 2026-10-11 05:23 UTC
# Deeper models an author may run with their own API key. The prompt, schema and
# checks are identical to the platform service, so reports stay comparable.
AUTHOR_MODELS = {'claude-opus-5-5', 'claude-sonnet-5-5', 'claude-fable-5-1',
                 'gpt-6-luna', 'gpt-6-sol', 'gpt-6-astra', 'deepseek-flash', 'deepseek-v4-pro'}
SEAL_INFO = b'eksmu-analysis-author-key-v1'
CATEGORIES = {'derivation_gap', 'unsupported_claim', 'internal_inconsistency', 'missing_definition',
              'known_result', 'citation_issue', 'reproduction', 'formal_check', 'clarity', 'other'}
SEVERITIES = {'blocking', 'major', 'minor', 'note'}
CONFIDENCE = {'low', 'medium', 'high'}
KINDS = {'opinion', 'check'}
STANCES = {'accept', 'dispute', 'revised', 'unclear'}
STATES = {'queued', 'running', 'done', 'failed', 'declined'}
MAX_FINDINGS = 40
MAX_REQUESTS_PER_AUTHOR_DAY = 3
MAX_RESPONSES_PER_REPORT = 2000
MAX_BODY_CHARS = 200_000   # title + body; attachment text is truncated (eksmu.manuscript), never refused
INPUT_KINDS = {'body', 'source', 'html', 'pdf'}


def now():
    return datetime.now(timezone.utc).isoformat()


# Characters a model may drop or change when quoting Markdown/LaTeX text:
# math delimiters, emphasis/code marks, quotation marks and all whitespace.
_QUOTE_NOISE = re.compile(r'\\[()\[\]]|[$`*_~"\'“”‘’「」『』\s]')


def _normal(value: str) -> str:
    return _QUOTE_NOISE.sub('', value).casefold()


def quote_found(quote: str, source: str) -> bool:
    q = _normal(quote)
    return len(q) >= 4 and q in _normal(source)


def revision_text(revision: dict) -> str:
    p = revision['payload']
    return p['title'] + '\n\n' + p['body']


def seal_key(api_key: str, worker_public_hex: str, request_id: str) -> bytes:
    """Encrypt an author's API key so that only the analysis worker can read it.

    Ephemeral X25519 + HKDF-SHA256 + ChaCha20-Poly1305, bound to the request id.
    The web server holds only the worker's public key and cannot decrypt.
    """
    import os
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    worker = X25519PublicKey.from_public_bytes(bytes.fromhex(worker_public_hex))
    ephemeral = X25519PrivateKey.generate()
    eph_pub = ephemeral.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    key = HKDF(hashes.SHA256(), 32, None, SEAL_INFO + eph_pub + bytes.fromhex(worker_public_hex)).derive(ephemeral.exchange(worker))
    nonce = os.urandom(12)
    return eph_pub + nonce + ChaCha20Poly1305(key).encrypt(nonce, api_key.encode(), request_id.encode())


def unseal_key(sealed: bytes, worker_private, request_id: str) -> str:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    eph_pub, nonce, body = sealed[:32], sealed[32:44], sealed[44:]
    own = worker_private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    key = HKDF(hashes.SHA256(), 32, None, SEAL_INFO + eph_pub + own).derive(
        worker_private.exchange(X25519PublicKey.from_public_bytes(eph_pub)))
    return ChaCha20Poly1305(key).decrypt(nonce, body, request_id.encode()).decode()


def check_api_key(value) -> None:
    if not isinstance(value, str) or not 20 <= len(value) <= 400 or re.fullmatch(r'[\x21-\x7e]+', value) is None:
        raise Problem('API 密钥格式不正确')


def validate_finding(f: dict, n: int) -> None:
    exact(f, {'id', 'kind', 'category', 'severity', 'confidence', 'location', 'statement', 'quote_verified'})
    if f['id'] != f'F{n}':
        raise Problem('Finding identifiers must be F1, F2, … in order')
    enum(f['kind'], KINDS); enum(f['category'], CATEGORIES); enum(f['severity'], SEVERITIES)
    enum(f['confidence'], CONFIDENCE)
    exact(f['location'], {'section', 'quote'})
    text(f['location']['section'], 200, empty=True); text(f['location']['quote'], 1000, empty=True)
    text(f['statement'], 4000)
    if not isinstance(f['quote_verified'], bool):
        raise Problem('quote_verified must be a boolean')


V1_FIELDS = {'format', 'id', 'revision_id', 'revision_sha256', 'created_at', 'analyzer', 'scope',
             'findings', 'limitations', 'suspicious_instructions', 'usage', 'sha256'}
V2_FIELDS = V1_FIELDS | {'summary', 'claims', 'strengths', 'questions', 'suggestions', 'overall'}


def _points(value, limit=MAX_POINTS, size=2000):
    if not isinstance(value, list) or len(value) > limit:
        raise Problem('Invalid list in analysis report')
    for item in value:
        text(item, size)


def validate_report(r: dict) -> dict:
    if not isinstance(r, dict) or r.get('format') not in (REPORT, REPORT_V1):
        raise Problem('Unsupported analysis report')
    exact(r, V2_FIELDS if r['format'] == REPORT else V1_FIELDS)
    if r['format'] == REPORT:
        text(r['summary'], 4000, empty=True); text(r['overall'], 6000, empty=True)
        for key in ('strengths', 'questions', 'suggestions'):
            _points(r[key])
        if not isinstance(r['claims'], list) or len(r['claims']) > MAX_CLAIMS:
            raise Problem('Invalid claims list')
        for n, c in enumerate(r['claims'], 1):
            exact(c, {'id', 'claim', 'section', 'assessment', 'reasoning'})
            if c['id'] != f'C{n}':
                raise Problem('Claim identifiers must be C1, C2, … in order')
            text(c['claim'], 1000); text(c['section'], 200, empty=True); text(c['reasoning'], 3000)
            enum(c['assessment'], ASSESSMENTS)
    identifier(r['id']); identifier(r['revision_id']); text(r['created_at'], 64)
    exact(r['analyzer'], {'kind', 'name', 'requested_model', 'prompt_template', 'operator', 'cost_borne_by'})
    enum(r['analyzer']['kind'], {'model', 'example'})
    for key in ('name', 'requested_model', 'prompt_template', 'operator', 'cost_borne_by'):
        text(r['analyzer'][key], 200)
    exact(r['scope'], {'read', 'not_checked', 'inputs'} if 'inputs' in r['scope'] else {'read', 'not_checked'})
    text(r['scope']['read'], 1000)
    if 'inputs' in r['scope']:
        if not isinstance(r['scope']['inputs'], list) or not 1 <= len(r['scope']['inputs']) <= 10:
            raise Problem('Invalid scope')
        for item in r['scope']['inputs']:
            exact(item, {'kind', 'filename', 'sha256', 'chars', 'truncated', 'read'})
            enum(item['kind'], INPUT_KINDS)
            text(item['filename'], 255, empty=True); text(item['sha256'], 64, empty=True)
            if type(item['chars']) is not int or item['chars'] < 0 or type(item['truncated']) is not bool or type(item['read']) is not bool:
                raise Problem('Invalid scope')
    if not isinstance(r['scope']['not_checked'], list) or len(r['scope']['not_checked']) > 20:
        raise Problem('Invalid scope')
    for item in r['scope']['not_checked']:
        text(item, 500)
    if not isinstance(r['findings'], list) or len(r['findings']) > MAX_FINDINGS:
        raise Problem('Invalid findings list')
    for n, f in enumerate(r['findings'], 1):
        validate_finding(f, n)
    text(r['limitations'], 4000, empty=True)
    if not isinstance(r['suspicious_instructions'], bool):
        raise Problem('suspicious_instructions must be a boolean')
    exact(r['usage'], {'input_tokens', 'output_tokens', 'cost_usd'})
    if r['sha256'] != digest({k: v for k, v in r.items() if k != 'sha256'}):
        raise Problem('Analysis report digest mismatch')
    return r


def validate_response(v: dict) -> dict:
    exact(v, {'format', 'id', 'report_id', 'finding_id', 'actor_id', 'is_author', 'stance', 'body',
              'revision_ref', 'created_at', 'sha256'})
    if v['format'] != RESPONSE:
        raise Problem('Unsupported analysis response')
    for key in ('id', 'report_id', 'actor_id'):
        identifier(v[key])
    if not isinstance(v['finding_id'], str) or re.fullmatch(r'F[1-9][0-9]?', v['finding_id']) is None:
        raise Problem('Invalid finding reference')
    if not isinstance(v['is_author'], bool):
        raise Problem('is_author must be a boolean')
    enum(v['stance'], STANCES); text(v['body'], 4000)
    if v['revision_ref'] is not None:
        identifier(v['revision_ref'])
    if v['sha256'] != digest({k: x for k, x in v.items() if k != 'sha256'}):
        raise Problem('Analysis response digest mismatch')
    return v


NOTE_NOT_READ = 'attachments, data files and code were not read'
NOTE_NOT_RUN = 'data files and code were not executed'
NOTE_PDF = 'PDF attachments were not read (no text extraction)'


def build_report(revision: dict, analyzer: dict, raw: dict, usage: dict, manuscript: dict | None = None) -> dict:
    """Turn a model's structured output into a sealed report, checking every quote.

    ``manuscript`` is what the model read (eksmu.manuscript.build_manuscript); quotes are checked against
    its full text, and the report records which inputs it covered."""
    source = manuscript['text'] if manuscript else revision_text(revision)
    if manuscript:
        attached = any(i['read'] and i['kind'] != 'body' for i in manuscript['inputs'])
        notes = [NOTE_NOT_RUN if attached else NOTE_NOT_READ]
        if any(i['kind'] == 'pdf' and not i['read'] for i in manuscript['inputs']):
            notes.append(NOTE_PDF)
        scope = {'read': manuscript['read'], 'inputs': manuscript['inputs']}
    else:
        notes = [NOTE_NOT_READ]
        scope = {'read': 'title and full body of this revision; no attachments'}
    findings = []
    for n, item in enumerate(raw.get('findings', [])[:MAX_FINDINGS], 1):
        quote = str(item.get('quote', ''))[:1000]
        findings.append({
            'id': f'F{n}', 'kind': 'opinion',
            'category': item.get('category') if item.get('category') in CATEGORIES else 'other',
            'severity': item.get('severity') if item.get('severity') in SEVERITIES else 'note',
            'confidence': item.get('confidence') if item.get('confidence') in CONFIDENCE else 'low',
            'location': {'section': str(item.get('section', ''))[:200], 'quote': quote},
            'statement': (str(item.get('statement', '')).strip() or '(empty)')[:4000],
            'quote_verified': quote_found(quote, source),
        })
    def points(key):
        return [str(x).strip()[:2000] for x in raw.get(key, []) if str(x).strip()][:MAX_POINTS]
    claims = []
    for item in raw.get('claims', [])[:MAX_CLAIMS]:
        claim = str(item.get('claim', '')).strip()[:1000]
        if not claim:
            continue
        claims.append({'id': f'C{len(claims) + 1}', 'claim': claim, 'section': str(item.get('section', ''))[:200],
                       'assessment': item.get('assessment') if item.get('assessment') in ASSESSMENTS else 'not_assessed',
                       'reasoning': (str(item.get('reasoning', '')).strip() or '(empty)')[:3000]})
    report = {
        'format': REPORT, 'id': str(uuid.uuid4()), 'revision_id': revision['id'],
        'summary': str(raw.get('summary', '')).strip()[:4000], 'claims': claims, 'strengths': points('strengths'),
        'questions': points('questions'), 'suggestions': points('suggestions'),
        'overall': str(raw.get('overall', '')).strip()[:6000],
        'revision_sha256': revision['sha256'], 'created_at': now(), 'analyzer': analyzer,
        'scope': dict(scope, not_checked=[str(x)[:500] for x in raw.get('not_checked', [])][:20 - len(notes)] + notes),
        'findings': findings,
        'limitations': str(raw.get('limitations', ''))[:4000],
        'suspicious_instructions': bool(raw.get('suspicious_instructions', False)),
        'usage': usage,
    }
    report['sha256'] = digest(report)
    return validate_report(report)


class Analysis:
    def __init__(self, path, records):
        self.index = {r['id']: r for r in records}
        self.db = sqlite3.connect(str(path), timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if tables == _V1_TABLES:
            self.db.executescript('''BEGIN IMMEDIATE;
              ALTER TABLE analysis_requests ADD COLUMN model TEXT NOT NULL DEFAULT '';
              ALTER TABLE analysis_requests ADD COLUMN payer TEXT NOT NULL DEFAULT 'platform';
              ALTER TABLE analysis_worker ADD COLUMN public_key TEXT NOT NULL DEFAULT '';
              CREATE TABLE analysis_keys(request_id TEXT PRIMARY KEY, sealed BLOB NOT NULL);
              COMMIT;''')
            tables = TABLES
        if tables and tables != TABLES:
            self.db.close(); raise Problem('分析记录需要完整的独立数据库', 503)
        if not tables:
            self.db.executescript('''BEGIN IMMEDIATE;
              CREATE TABLE analysis_requests(id TEXT PRIMARY KEY, revision_id TEXT NOT NULL, actor_id TEXT NOT NULL,
                created_at TEXT NOT NULL, state TEXT NOT NULL, detail TEXT NOT NULL, report_id TEXT, updated_at TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT '', payer TEXT NOT NULL DEFAULT 'platform');
              CREATE TABLE analysis_keys(request_id TEXT PRIMARY KEY, sealed BLOB NOT NULL);
              CREATE TABLE analysis_reports(id TEXT PRIMARY KEY, revision_id TEXT NOT NULL, document TEXT NOT NULL);
              CREATE TABLE analysis_responses(id TEXT PRIMARY KEY, report_id TEXT NOT NULL, document TEXT NOT NULL);
              CREATE TABLE analysis_spend(month TEXT PRIMARY KEY, cost_usd REAL NOT NULL, reports INTEGER NOT NULL);
              CREATE TABLE analysis_worker(id INTEGER PRIMARY KEY CHECK (id = 1), last_run TEXT NOT NULL, model TEXT NOT NULL,
                public_key TEXT NOT NULL DEFAULT '');
              COMMIT;''')

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback(); raise

    def revision(self, revision_id):
        identifier(revision_id)
        found = self.index.get(revision_id)
        if found is None or found['type'] != 'revision':
            raise Problem('找不到对应的版本', 404)
        return found

    def worker_public_key(self):
        row = self.db.execute('SELECT public_key FROM analysis_worker WHERE id=1').fetchone()
        return row['public_key'] if row and row['public_key'] else None

    def request(self, actor_id, revision_id, model=None, api_key=None):
        """Queue an analysis. Without a key: the platform's model and budget.
        With a key: the author's chosen deeper model, billed to the author's account."""
        rev = self.revision(revision_id)
        if rev['actor_id'] != actor_id:
            raise Problem('只有作者本人可以请求 AI 分析：分析会把正文发送给外部模型服务', 403)
        if len(revision_text(rev)) > MAX_BODY_CHARS:
            raise Problem('正文过长，暂不支持分析', 413)
        author_paid = api_key is not None
        if author_paid:
            check_api_key(api_key)
            if model not in AUTHOR_MODELS:
                raise Problem('不支持所选模型')
            public = self.worker_public_key()
            if public is None:
                raise Problem('分析服务尚未启用，暂时不能使用自带密钥', 503)
        elif model is not None:
            raise Problem('选择模型需要提供自己的 API 密钥')
        since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        rid, stamp = str(uuid.uuid4()), now()
        sealed = seal_key(api_key, public, rid) if author_paid else None
        with self.transaction():
            if self.db.execute("SELECT 1 FROM analysis_requests WHERE revision_id=? AND state IN ('queued','running')",
                               (revision_id,)).fetchone():
                raise Problem('此版本已有正在进行的分析请求', 409)
            if not author_paid and self.db.execute("SELECT 1 FROM analysis_requests WHERE revision_id=? AND payer='platform' "
                                                   "AND state='done'", (revision_id,)).fetchone():
                raise Problem('此版本已有平台分析报告；需要更深入的分析可以使用自己的 API 密钥', 409)
            if self.db.execute('SELECT COUNT(*) FROM analysis_requests WHERE actor_id=? AND created_at>=?',
                               (actor_id, since)).fetchone()[0] >= MAX_REQUESTS_PER_AUTHOR_DAY:
                raise Problem('每位作者每天最多请求 3 次分析', 429)
            self.db.execute('INSERT INTO analysis_requests VALUES(?,?,?,?,?,?,?,?,?,?)',
                            (rid, revision_id, actor_id, stamp, 'queued', '', None, stamp,
                             model or '', 'author' if author_paid else 'platform'))
            if sealed is not None:
                self.db.execute('INSERT INTO analysis_keys VALUES(?,?)', (rid, sealed))
        return {'id': rid, 'revision_id': revision_id, 'state': 'queued', 'payer': 'author' if author_paid else 'platform',
                'model': model}

    def respond(self, actor_id, report_id, finding_id, stance, body, revision_ref=None):
        identifier(actor_id)
        author = self.index.get(actor_id)
        if author is None or author['type'] != 'actor':
            raise Problem('找不到账户', 404)
        report = self.report(report_id)
        if not any(f['id'] == finding_id for f in report['findings']):
            raise Problem('报告中没有这一条发现', 404)
        rev = self.revision(report['revision_id'])
        if revision_ref is not None:
            ref = self.revision(revision_ref)
            if ref['payload']['exploration_id'] != rev['payload']['exploration_id']:
                raise Problem('引用的版本不属于同一作品')
        value = {'format': RESPONSE, 'id': str(uuid.uuid4()), 'report_id': report_id, 'finding_id': finding_id,
                 'actor_id': actor_id, 'is_author': actor_id == rev['actor_id'], 'stance': stance,
                 'body': body, 'revision_ref': revision_ref, 'created_at': now()}
        value['sha256'] = digest(value)
        validate_response(value)
        with self.transaction():
            if self.db.execute('SELECT COUNT(*) FROM analysis_responses WHERE report_id=?',
                               (report_id,)).fetchone()[0] >= MAX_RESPONSES_PER_REPORT:
                raise Problem('此报告的回应数量已达上限', 413)
            self.db.execute('INSERT INTO analysis_responses VALUES(?,?,?)', (value['id'], report_id, canonical(value)))
        return value

    def report(self, report_id):
        identifier(report_id)
        row = self.db.execute('SELECT document FROM analysis_reports WHERE id=?', (report_id,)).fetchone()
        if row is None:
            raise Problem('报告不存在', 404)
        return validate_report(json.loads(row['document']))

    def view(self, revision_id):
        self.revision(revision_id)
        reports = [validate_report(json.loads(r['document'])) for r in
                   self.db.execute('SELECT document FROM analysis_reports WHERE revision_id=? ORDER BY rowid', (revision_id,))]
        ids = [r['id'] for r in reports]
        responses = [json.loads(r['document']) for r in self.db.execute(
            f"SELECT document FROM analysis_responses WHERE report_id IN ({','.join('?' * len(ids))}) ORDER BY rowid", ids)] if ids else []
        req = self.db.execute('SELECT state, detail, created_at, updated_at, model, payer FROM analysis_requests '
                              'WHERE revision_id=? ORDER BY rowid DESC LIMIT 1', (revision_id,)).fetchone()
        worker = self.db.execute('SELECT last_run, model, public_key FROM analysis_worker WHERE id=1').fetchone()
        # The worker's timer runs every minute; 15 minutes without a heartbeat means it is not running,
        # and pages must not promise "done in 1–2 minutes".
        active = worker is not None and worker['last_run'] >= (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
        source = revision_text(self.revision(revision_id))
        # The sealed report keeps the check as run at the time; pages show the current rule.
        # Quotes from an attachment were checked by the worker against the full text it sent; keep that result.
        quote_checks = {r['id']: {f['id']: f['quote_verified'] or quote_found(f['location']['quote'], source)
                                  for f in r['findings']} for r in reports}
        return {'revision_id': revision_id, 'reports': reports, 'responses': responses, 'quote_checks': quote_checks,
                'request': dict(req) if req else None,
                'service': {'active': active, 'model': worker['model'] if worker else None,
                            'last_run': worker['last_run'] if worker else None,
                            'author_keys': bool(active and worker['public_key']),
                            'author_models': sorted(AUTHOR_MODELS)}}

    def notices(self, actor_id):
        """For "My activity": reports on the actor's works, and others' responses to them."""
        mine = {rid for rid, r in self.index.items() if r['type'] == 'revision' and r['actor_id'] == actor_id}
        out = []
        reports = {}
        for (doc,) in self.db.execute('SELECT document FROM analysis_reports ORDER BY rowid'):
            r = json.loads(doc)
            reports[r['id']] = r
            if r['revision_id'] in mine:
                rev = self.index[r['revision_id']]
                major = sum(1 for f in r['findings'] if f['severity'] in ('blocking', 'major'))
                out.append({'id': r['id'], 'exploration_id': rev['payload']['exploration_id'], 'revision_id': rev['id'],
                            'type': 'analysis', 'action': 'report', 'reason': 'your_exploration',
                            'from': r['analyzer']['name'], 'title': rev['payload']['title'], 'created_at': r['created_at'],
                            'excerpt': (r.get('summary') or '')[:160] or f"{len(r['findings'])} findings, {major} major"})
        for (doc,) in self.db.execute('SELECT document FROM analysis_responses ORDER BY rowid'):
            v = json.loads(doc)
            r = reports.get(v['report_id'])
            if r is None or r['revision_id'] not in mine or v['actor_id'] == actor_id:
                continue
            rev = self.index[r['revision_id']]
            who = self.index.get(v['actor_id'], {}).get('payload', {}).get('alias', v['actor_id'][:8])
            out.append({'id': v['id'], 'exploration_id': rev['payload']['exploration_id'], 'revision_id': rev['id'],
                        'type': 'analysis', 'action': 'response', 'reason': 'your_exploration', 'from': who,
                        'title': rev['payload']['title'], 'created_at': v['created_at'],
                        'excerpt': f"{v['finding_id']}: {v['body']}"[:160]})
        return out

    def public_dump(self):
        reports = [json.loads(r['document']) for r in self.db.execute('SELECT document FROM analysis_reports ORDER BY rowid')]
        responses = [json.loads(r['document']) for r in self.db.execute('SELECT document FROM analysis_responses ORDER BY rowid')]
        return {'reports': reports, 'responses': responses}

    # Worker side -----------------------------------------------------------

    def next_queued(self):
        with self.transaction():
            row = self.db.execute("SELECT id, revision_id, model, payer FROM analysis_requests WHERE state='queued' "
                                  "ORDER BY rowid LIMIT 1").fetchone()
            if row is None:
                return None
            self.db.execute("UPDATE analysis_requests SET state='running', updated_at=? WHERE id=?", (now(), row['id']))
            return dict(row)

    def heartbeat(self, model, public_key=''):
        with self.transaction():
            self.db.execute('INSERT INTO analysis_worker VALUES(1,?,?,?) ON CONFLICT(id) DO UPDATE SET '
                            'last_run=excluded.last_run, model=excluded.model, public_key=excluded.public_key',
                            (now(), model, public_key))

    def sealed_key(self, request_id):
        row = self.db.execute('SELECT sealed FROM analysis_keys WHERE request_id=?', (request_id,)).fetchone()
        return bytes(row['sealed']) if row else None

    def requeue_stale(self, older_than_minutes=60):
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes)).isoformat()
        expired = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        with self.transaction():
            # Author keys are never kept longer than a day, even if the queue stalls.
            old = [r[0] for r in self.db.execute("SELECT id FROM analysis_requests WHERE payer='author' AND state IN ('queued','running') "
                                                 "AND created_at<?", (expired,))]
            for rid in old:
                self.db.execute('DELETE FROM analysis_keys WHERE request_id=?', (rid,))
                self.db.execute("UPDATE analysis_requests SET state='failed', detail=?, updated_at=? WHERE id=?",
                                ('author key expired after 24 hours; please request again', now(), rid))
            return self.db.execute("UPDATE analysis_requests SET state='queued', updated_at=? WHERE state='running' AND updated_at<?",
                                   (now(), cutoff)).rowcount

    def finish(self, request_id, state, detail='', report=None, cost_usd=0.0, forget_key=False):
        enum(state, STATES)
        month = datetime.now(timezone.utc).strftime('%Y-%m')
        with self.transaction():
            if forget_key or state != 'queued':
                self.db.execute('DELETE FROM analysis_keys WHERE request_id=?', (request_id,))
            if report is not None:
                validate_report(report)
                self.db.execute('INSERT INTO analysis_reports VALUES(?,?,?)', (report['id'], report['revision_id'], canonical(report)))
            if cost_usd:
                self.db.execute('INSERT INTO analysis_spend VALUES(?,?,?) ON CONFLICT(month) DO UPDATE SET '
                                'cost_usd=cost_usd+excluded.cost_usd, reports=reports+excluded.reports',
                                (month, float(cost_usd), 1 if report is not None else 0))
            self.db.execute('UPDATE analysis_requests SET state=?, detail=?, report_id=?, updated_at=? WHERE id=?',
                            (state, detail[:500], report['id'] if report else None, now(), request_id))

    def spent(self, month=None):
        month = month or datetime.now(timezone.utc).strftime('%Y-%m')
        row = self.db.execute('SELECT cost_usd FROM analysis_spend WHERE month=?', (month,)).fetchone()
        return float(row['cost_usd']) if row else 0.0

    def record_spend(self, cost_usd: float):
        if not cost_usd:
            return
        month = datetime.now(timezone.utc).strftime('%Y-%m')
        with self.transaction():
            self.db.execute('INSERT INTO analysis_spend VALUES(?,?,0) ON CONFLICT(month) DO UPDATE SET '
                            'cost_usd=cost_usd+excluded.cost_usd', (month, float(cost_usd)))
