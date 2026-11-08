"""Real subprocess + HTTP recovery drill with two synthetic identities.

Uses temporary data only. Emits a redacted report, never identity or signing keys.
The finite arithmetic receipts tested here are not Lean or mathematical judging.
"""
import argparse
import base64
from contextlib import contextmanager
import hashlib
import http.client
import json
from pathlib import Path
import select
import secrets
import signal
import sqlite3
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from eksmu.backup import COMPONENTS, create_backup, restore_backup, verify_backup
from eksmu.account_recovery import reconcile_accounts
from eksmu_bridge.handoff import execute_capsule
from eksmu_receipts.core import public_hex
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


@contextmanager
def serving(database):
    process = subprocess.Popen([sys.executable, '-m', 'eksmu', '--db', str(database),
                                'serve', '--port', '0', '--with-verification'],
                               cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        if not select.select([process.stdout], [], [], 10)[0]:
            raise RuntimeError('Demo server did not become ready')
        line = process.stdout.readline().strip()
        if not line.startswith('Local PRIVATE prototype: http://127.0.0.1:'):
            raise RuntimeError('Demo server could not start')
        port = int(line.rsplit(':', 1)[1])

        def api(path, data=None, token=None):
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
            headers = {'Content-Type': 'application/json', 'X-Eksmu-Client': 'local-prototype'}
            if token:
                headers['Authorization'] = 'Bearer ' + token
            try:
                connection.request('GET' if data is None else 'POST', path,
                                   None if data is None else json.dumps(data), headers)
                response = connection.getresponse(); body = response.read()
                if response.status >= 400:
                    raise RuntimeError(f'Demo request failed: {path.split("?")[0]} HTTP {response.status}')
                return json.loads(body) if response.getheader('Content-Type', '').startswith('application/json') else body
            finally:
                connection.close()
        assert api('/api/health')['mode'] == 'local-private-prototype'
        yield api
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=5)
        process.stdout.close()


def database_content(database):
    """Compare records and all sidecar tables/BLOBs; auth state is deliberately reset."""
    content = {}
    for component, (suffix, _) in COMPONENTS.items():
        connection = sqlite3.connect(str(database) + suffix)
        try:
            # Restoring intentionally revokes sessions; compare publication data separately.
            body = ('\n'.join(r[0] for r in connection.execute('SELECT document FROM records ORDER BY rowid'))
                    if component == 'records' else '\n'.join(connection.iterdump()))
            content[component] = hashlib.sha256(body.encode()).hexdigest()
        finally:
            connection.close()
    return content


def drill():
    report = {'demo': True, 'identities': 2, 'transport': 'real subprocesses and loopback HTTP',
              'browser_used': False, 'lean_used': False, 'checks': []}
    with tempfile.TemporaryDirectory(prefix='eksmu-backup-demo-') as temporary:
        root = Path(temporary); (root / 'source').mkdir(); database = root / 'source/community.sqlite'
        with serving(database) as api:
            a = api('/api/actors', {'alias': '恢复演示 A'}); b = api('/api/actors', {'alias': '恢复演示 B'})
            metadata = {'format': 'eksmu.paper-metadata.v1', 'abstract': '演示：测试论文元数据恢复',
                        'authors': [{'name': '演示作者', 'contribution': '准备测试数据'}],
                        'references': ['合成演示文献'], 'license': '',
                        'provenance': {'source': '合成旧稿来源', 'prior_publication_date': '2020-01-01'}}
            work = api('/api/explorations', {'title': '仅供软件恢复测试', 'body': '测试 1/3 + 1/6 = 1/2', 'paper': metadata}, a['token'])
            api('/api/explorations', {'body': '第二个作者的独立测试记录'}, b['token'])
            wid, rid = work['exploration']['id'], work['revision']['id']
            api('/api/community/details', {'exploration_id': wid, 'topics': ['软件演示'],
                'help': '仅用于恢复测试', 'expected_version': 0}, a['token'])
            for kind in ('follow', 'bookmark'):
                api('/api/community/mark', {'exploration_id': wid, 'kind': kind, 'enabled': True}, b['token'])
            raw = b'x,y\n1,2\n3,4\n'
            attachment = api('/api/research/upload', {'revision_id': rid, 'filename': 'test.csv',
                'data_b64': base64.b64encode(raw).decode()}, a['token'])
            task = api('/api/research/create-task', {'revision_id': rid, 'title': '软件恢复任务',
                'description': '核对演示数据', 'category': 'reproduce'}, a['token'])
            for action, body in (('join', ''), ('progress', '测试身份 B 已核对附件')):
                task = api('/api/research/task-action', {'task_id': task['id'], 'action': action,
                    'body': body, 'expected_version': task['version']}, b['token'])
            api('/api/analysis/request', {'revision_id': rid}, a['token'])
            comment = api('/api/comments', {'revision_id': rid, 'body': '恢复前的测试回应'}, b['token'])
            api('/api/community/read', {'record_ids': [comment['id']]}, a['token'])
            api('/api/revisions', {'exploration_id': wid, 'parent_revision_id': rid, 'title': '测试修订',
                'body': '恢复时旧版本与附件仍应保留', 'note': '软件演示'}, a['token'])
            snapshot = api('/api/publications/snapshot', {'revision_id': rid, 'attachment_ids': [attachment['id']],
                'expected_previous_id': None, 'operation_id': str(uuid.uuid4())}, a['token'])
            frozen = api('/api/publications/export?snapshot_id=' + snapshot['id'])
            later = api('/api/research/upload', {'revision_id': rid, 'filename': 'later.txt',
                'data_b64': base64.b64encode(b'later synthetic attachment').decode()}, a['token'])
            snapshot2 = api('/api/publications/snapshot', {'revision_id': rid, 'attachment_ids': [attachment['id'], later['id']],
                'expected_previous_id': snapshot['id'], 'operation_id': str(uuid.uuid4())}, a['token'])
            previous = None
            for kind in ('note', 'withdraw', 'reinstate'):
                event = api('/api/publications/event', {'revision_id': rid, 'kind': kind, 'body': '软件恢复演示声明',
                    'expected_previous_id': previous, 'operation_id': str(uuid.uuid4())}, a['token'])
                previous = event['id']
            assert api('/api/publications/export?snapshot_id=' + snapshot['id']) == frozen
            rational = lambda n, d: {'op': 'rational', 'numerator': str(n), 'denominator': str(d)}
            problem = {'left': {'op': 'add', 'left': rational(1, 3), 'right': rational(1, 6)}, 'right': rational(1, 2)}
            prepared = api('/api/verification/prepare', {'revision_id': rid, 'scope': '有限算术演示', 'problem': problem}, b['token'])
            tid = prepared['task']['id']
            api('/api/verification/execute', {'task_id': tid, 'operation_id': str(uuid.uuid4()), 'consent': True}, b['token'])
            worker = Ed25519PrivateKey.generate()
            dispatched = api('/api/verification/dispatch', {'task_id': tid, 'operation_id': str(uuid.uuid4()),
                'expected_public_key': public_hex(worker), 'consent': True, 'ttl_seconds': 3600}, b['token'])
            capsule = dispatched['dispatch']['capsule']
            submission = execute_capsule(capsule, worker, allow_local=True)
            api('/api/verification/collect', {'dispatch_id': capsule['dispatch_id'], 'submission': submission}, b['token'])
            public_key = api('/api/verification/health')['public_key']

            def capture(client):
                paths = [('/api/state', None), ('/api/me', a['token']), ('/api/me', b['token']),
                    ('/api/community/export', a['token']), ('/api/community/export', b['token']),
                    ('/api/community/notifications', a['token']), ('/api/community/notifications', b['token']),
                    ('/api/research/workspace?revision_id=' + rid, None),
                    ('/api/research/file?attachment_id=' + attachment['id'], None),
                    ('/api/verification/export?task_id=' + tid, None),
                    ('/api/verification/handoffs?task_id=' + tid, b['token']),
                    ('/api/publications/workspace?revision_id=' + rid, None),
                    ('/api/analysis?revision_id=' + rid, None),
                    ('/api/publications/snapshot?snapshot_id=' + snapshot['id'], None),
                    ('/api/publications/export?snapshot_id=' + snapshot['id'], None),
                    ('/api/publications/export?snapshot_id=' + snapshot2['id'], None),
                    ('/api/publications/file?snapshot_id=' + snapshot['id'] + '&attachment_id=' + attachment['id'], None)]
                values = [client(path, token=token) for path, token in paths]
                paper_revisions = [r for r in values[0]['records'] if r['type'] == 'revision' and r['payload']['exploration_id'] == wid]
                assert len(paper_revisions) == 2 and all(r['payload']['paper'] == metadata for r in paper_revisions)
                assert values[8] == raw
                assert values[-3] == frozen and values[-1] == raw
                assert client('/api/verification/health')['public_key'] == public_key
                checks = client('/api/verification/recheck', {'task_id': tid, 'expected_public_key': public_key}, b['token'])['checks']
                assert checks and all(c['request_binding_valid'] and c['replay_matches'] for c in checks)
                return values

            expected = capture(api)
            try:
                create_backup(database, root / 'must-fail', offline=True, secrets_dir=root / 'must-fail-secrets')
            except ValueError:
                pass
            else:
                raise AssertionError('A running instance permitted an offline backup')
        report['checks'].append('running subprocess refuses offline backup')
        with serving(database) as api:
            assert capture(api) == expected
        original = database_content(database)
        result = create_backup(database, root / 'backup', offline=True, secrets_dir=root / 'secrets')
        assert all(result['manifest']['components'].values()) and result['secrets_verified']
        report['checks'].append('all six databases and separate signer captured after original restart')
        restored = restore_backup(root / 'backup', root / 'restored', secrets_dir=root / 'secrets')
        assert database_content(restored) == original
        reconcile_accounts(restored, database, offline=True)
        with serving(restored) as api:
            for identity in (a, b):
                session = 'ois1_' + secrets.token_urlsafe(32)
                result = api('/api/auth/login', {'credential': identity['token'], 'session_token': session,
                    'operation_id': str(uuid.uuid4()), 'label': '恢复演示设备'})
                assert result['actor']['id'] == identity['actor']['id']
                identity['token'] = session
        report['checks'].append('publication and sidecar tables/BLOBs match; restored credentials require review and fresh login')
        for _ in range(2):
            with serving(restored) as api:
                assert capture(api) == expected
        report['checks'].extend([
            'author ownership survives restore review, fresh session login and restart',
            'works, old/new paper metadata, comments, follows, bookmarks and notification read state match',
            'attachment bytes, collaboration membership and attributed progress match; publication snapshots, citations and withdrawal/reinstatement history match',
            'receipt attempts, collected worker handoff and service public key match; signed checks replay'])
        with serving(restored) as api:
            api('/api/comments', {'revision_id': rid, 'body': '只在恢复实例新增的演示回应'}, b['token'])
        assert database_content(database) == original
        assert database_content(restored) != original
        report['checks'].append('new write succeeds only in restored instance; original remains unchanged')
        assert verify_backup(root / 'backup', secrets_dir=root / 'secrets')['data_verified']
    report['status'] = 'PASS'
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report')
    args = parser.parse_args()
    report = drill()
    body = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    if args.report:
        Path(args.report).write_text(body, encoding='utf-8')
    print(body, end='')


if __name__ == '__main__':
    main()
