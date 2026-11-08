"""M2 browser drill: synthetic data, real HTTP, explicit lost-response injection."""
import argparse
import http.client
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


class Server:
    def __init__(self, python, database, app_root=None):
        self.python, self.database, self.port, self.process = python, database, 0, None
        self.root = Path(app_root) if app_root is not None else ROOT

    def start(self):
        self.process = subprocess.Popen([self.python, '-m', 'eksmu', '--db', str(self.database),
            'serve', '--port', str(self.port)], cwd=self.root, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True)
        if not select.select([self.process.stdout], [], [], 10)[0]:
            raise RuntimeError('Test server startup timed out')
        line = self.process.stdout.readline().strip()
        assert line.startswith('Local PRIVATE prototype: http://127.0.0.1:')
        self.port = int(line.rsplit(':', 1)[1]); self.url = f'http://127.0.0.1:{self.port}'

    def stop(self):
        if self.process:
            if self.process.poll() is None:
                self.process.send_signal(signal.SIGINT)
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.process.kill(); self.process.wait(timeout=5)
            self.process.stdout.close(); self.process = None

    def api(self, path, data=None, token=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        headers = {'Content-Type': 'application/json', 'X-Eksmu-Client': 'local-prototype'}
        if token: headers['Authorization'] = 'Bearer ' + token
        try:
            conn.request('GET' if data is None else 'POST', path, None if data is None else json.dumps(data), headers)
            response = conn.getresponse(); body = response.read()
            return response.status, json.loads(body)
        finally:
            conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-python', required=True)
    parser.add_argument('--browser', required=True)
    parser.add_argument('--library-path')
    parser.add_argument('--report', required=True)
    args = parser.parse_args()
    report = {'status': 'FAIL', 'demo': True, 'identities': 2, 'browser_contexts': 3,
              'actual_user_data_used': False, 'ssh_tunnel_tested': False, 'checks': [],
              'fault_injection': 'Two committed responses intentionally aborted by Playwright routing; other requests use real loopback HTTP.'}
    from playwright.sync_api import sync_playwright, expect
    with tempfile.TemporaryDirectory(prefix='eksmu-account-browser-') as directory:
        server = Server(args.app_python, Path(directory) / 'community.sqlite')
        try:
            server.start()
            _, legacy = server.api('/api/actors', {'alias': '演示 A'})
            _, old_work = server.api('/api/explorations', {'title': '原作者的旧作品', 'body': '用于核验作者迁移'}, legacy['token'])
            with sync_playwright() as playwright:
                env = {'PATH': os.environ.get('PATH', os.defpath)}
                if args.library_path: env['LD_LIBRARY_PATH'] = args.library_path
                browser = playwright.chromium.launch(executable_path=args.browser, headless=True,
                    chromium_sandbox=True, env=env)
                try:
                    report['browser_version'] = browser.version
                    ca, ca2, cb = [browser.new_context(accept_downloads=True, locale='zh-CN') for _ in range(3)]
                    a, a2, b = ca.new_page(), ca2.new_page(), cb.new_page()
                    errors = []
                    for page in (a, a2, b):
                        page.on('pageerror', lambda _: errors.append('JavaScript page error'))
                        page.on('dialog', lambda dialog: dialog.accept())
                    ca.add_init_script('localStorage.setItem("eksmu-identity-key",' + json.dumps(legacy['token']) + ');')
                    def view(page, name):
                        # Same in-page view switch the app uses; keeps the page's session state.
                        page.evaluate('name => showView(name)', name)
                    def open_work(page, work_id):
                        page.goto(server.url + '/workspace#exploration=' + work_id)
                        expect(page.locator('#detail')).to_be_visible()
                    a.goto(server.url + '/account'); expect(a.locator('#account-pending')).to_be_visible()

                    def backup(page):
                        with page.expect_download() as event:
                            page.locator('#backup').click()
                        value = json.loads(Path(event.value.path()).read_text())
                        page.locator('#backup-confirmed').check()
                        return value['credential']

                    assert backup(a) == legacy['token']
                    a.locator('#confirm-account').click()
                    expect(a.locator('#who')).to_contain_text('演示 A')
                    assert a.evaluate("localStorage.getItem('eksmu-identity-key')") is None
                    assert server.api('/api/me', token=legacy['token'])[0] == 401
                    assert a.request.get(server.url + '/api/me').json()['id'] == legacy['actor']['id']
                    report['checks'].append('legacy browser identity migrates without changing author or accepting old direct token')
                    b.goto(server.url + '/account'); b.locator('#legacy-account summary').click(); b.locator('#register input[name=alias]').fill('演示 B')
                    b.locator('#register button').click()
                    expect(b.locator('#confirm-account')).to_be_disabled()
                    b_recovery = backup(b); b.locator('#confirm-account').click()
                    expect(b.locator('#who')).to_contain_text('演示 B')
                    assert b_recovery.startswith('oir1_')
                    report['checks'].append('new identity requires saved recovery material before creation')

                    def login(page, credential, label):
                        page.locator('#device-label').fill(label)
                        page.locator('#restore input[name=token]').fill(credential)
                        page.locator('#restore button').click()

                    a2.goto(server.url + '/account'); a2.locator('#legacy-account summary').click(); login(a2, legacy['token'], '演示 A 第二设备')
                    expect(a2.locator('#who')).to_contain_text('演示 A')
                    assert a2.request.get(server.url + '/api/me').json()['id'] == legacy['actor']['id']
                    open_work(b, old_work['exploration']['id'])
                    expect(b.locator('#edit-box')).to_be_hidden()
                    expect(b.locator('#attachment-editor')).to_be_hidden()
                    open_work(a, old_work['exploration']['id'])
                    expect(a.locator('#edit-box')).to_be_visible()
                    view(a, 'account')
                    report['checks'].append('two identities and three independent browser contexts retain correct ownership controls')
                    a.locator('#logout-others').click()
                    expect(a.locator('#message')).to_contain_text('其他会话已撤销')
                    view(a2, 'publish')
                    a2.locator('#publish input[name=title]').fill('撤销会话检查')
                    a2.locator('#publish textarea[name=body]').fill('已撤销会话不能发表')
                    a2.locator('#publish button', has_text='发表').click()
                    expect(a2.locator('#message')).to_contain_text('会话已失效')
                    a2.goto(server.url + '/account'); a2.locator('#legacy-account summary').click(); expect(a2.locator('#restore')).to_be_visible()
                    report['checks'].append('revoking other devices prevents a stale browser from publishing')

                    a.locator('#rotate-recovery summary').click()
                    a.locator('#rotate-account input').fill(legacy['token'])
                    a.locator('#rotate-account button').click()
                    new_recovery = backup(a)
                    def lose_once(route):
                        response = route.fetch()
                        assert response.status == 200
                        route.abort('failed')
                    a.route('**/api/auth/rotate', lose_once, times=1)
                    a.locator('#confirm-account').click()
                    expect(a.locator('#message')).to_contain_text('网络响应未确认')
                    assert a.evaluate("sessionStorage.getItem('eksmu-account-pending-v1')")
                    a.locator('#confirm-account').click()
                    expect(a.locator('#account-pending')).to_be_hidden()
                    assert a.evaluate("sessionStorage.getItem('eksmu-account-pending-v1')") is None
                    report['checks'].append('lost committed rotation response keeps candidate and retries without a second rotation')
                    login(a2, legacy['token'], '失效凭证测试')
                    expect(a2.locator('#message')).to_contain_text('恢复凭证无效')
                    a2.locator('#cancel-account').click()
                    expect(a2.locator('#account-pending')).to_be_hidden()
                    login(a2, new_recovery, '演示 A 恢复设备')
                    expect(a2.locator('#who')).to_contain_text('演示 A')
                    assert a2.request.get(server.url + '/api/me').json()['id'] == legacy['actor']['id']
                    report['checks'].append('old recovery is refused and new recovery signs in as the original author')
                    for context, page in ((ca2, a2), (cb, b)):
                        cookies = context.cookies(server.url + '/api/me')
                        assert any(c['httpOnly'] and c['sameSite'] == 'Strict' and c['path'] == '/api' for c in cookies)
                        assert 'eksmu_session_' not in page.evaluate('document.cookie')
                        assert page.evaluate("localStorage.getItem('eksmu-identity-key')") is None
                    report['checks'].append('established sessions are HttpOnly cookies; successful flow removes temporary credential state')
                    server.stop(); server.start()
                    a2.goto(server.url + '/account'); b.goto(server.url + '/account')
                    expect(a2.locator('#who')).to_contain_text('演示 A'); expect(b.locator('#who')).to_contain_text('演示 B')
                    assert server.api('/api/me', token=legacy['token'])[0] == 401
                    report['checks'].append('server restart retains sessions, original authors and revoked legacy state')
                    open_work(a2, old_work['exploration']['id'])
                    a2.locator('#edit-box > summary').click()
                    a2.locator('#revise textarea[name=body]').fill('由恢复后的原作者提交的新版本')
                    a2.locator('#revise button', has_text='保存新版本').click()
                    expect(a2.locator('#message')).to_contain_text('新版本已保存')
                    state = server.api('/api/state')[1]['records']
                    revisions = [r for r in state if r['type'] == 'revision' and r['payload']['exploration_id'] == old_work['exploration']['id']]
                    assert len(revisions) == 2 and all(r['actor_id'] == legacy['actor']['id'] for r in revisions)
                    report['checks'].append('recovered author can revise the old work after server restart')
                    view(a2, 'account')
                    a2.route('**/api/auth/logout', lose_once, times=1)
                    a2.locator('#logout-account').click()
                    expect(a2.locator('#message')).to_contain_text('退出尚未确认')
                    a2.locator('#logout-account').click()
                    a2.locator('#legacy-account summary').click()
                    expect(a2.locator('#restore')).to_be_visible()
                    assert a2.request.get(server.url + '/api/auth/status').json()['actor'] is None
                    assert not errors, errors
                    report['checks'].append('lost logout response can be retried and confirms server-side logout')
                    report['status'] = 'PASS'
                finally:
                    browser.close()
        finally:
            server.stop()
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
