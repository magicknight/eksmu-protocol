"""Private real-browser account drill with synthetic users and a local mail sink."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import threading

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--browser', required=True)
    parser.add_argument('--library-path')
    parser.add_argument('--app-root', default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument('--report', required=True)
    args = parser.parse_args()
    app_root = Path(args.app_root).resolve()
    sys.path.insert(0, str(app_root))
    from eksmu.server import make_server
    from playwright.sync_api import sync_playwright, expect

    report = {'status': 'FAIL', 'runtime': 'source in isolated temporary database' if app_root == Path(__file__).resolve().parents[1]
              else 'installed runtime in isolated temporary database',
              'synthetic_users': 2, 'mail_delivery': 'in-process test sink; no external email',
              'actual_instance_data_touched': False, 'checks': []}
    sent = []
    with tempfile.TemporaryDirectory(prefix='eksmu-modern-browser-') as directory:
        server = make_server(Path(directory) / 'community.sqlite', 0,
                             mail_sender=lambda address, purpose, code: sent.append((address, purpose, code)))
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            origin = f'http://127.0.0.1:{server.server_port}'
            with sync_playwright() as pw:
                env = {'PATH': os.environ.get('PATH', os.defpath)}
                if args.library_path:
                    env['LD_LIBRARY_PATH'] = args.library_path
                browser = pw.chromium.launch(executable_path=args.browser, headless=True,
                                              chromium_sandbox=False, env=env)
                try:
                    report['browser_version'] = browser.version
                    first = browser.new_context(locale='zh-CN'); second = browser.new_context(locale='zh-CN'); other_device = browser.new_context(locale='zh-CN')
                    a, b, a2 = first.new_page(), second.new_page(), other_device.new_page()
                    errors = []
                    for page in (a, b, a2):
                        page.on('pageerror', lambda error: errors.append(str(error)))
                    def view(page, name):
                        # Same in-page view switch the app uses for starter cards and new works.
                        page.evaluate('name => showView(name)', name)
                    a.goto(origin + '/account')
                    a.locator('#tab-register').click()
                    expect(a.locator('#email-register')).to_be_visible()
                    expect(a.locator('#legacy-account')).not_to_have_attribute('open', '')
                    a.locator('#email-register input[name=alias]').fill('实名作者')
                    a.locator('#email-register input[name=email]').fill('author@example.org')
                    a.locator('#email-register input[name=password]').fill('A long private passphrase 2026')
                    a.locator('#email-register button').click()
                    expect(a.locator('#public-profile')).to_be_visible()
                    expect(a.locator('#who')).to_contain_text('实名作者')
                    actor = a.request.get(origin + '/api/me').json()['id']
                    assert 'author@example.org' not in json.dumps(a.request.get(origin + '/api/profiles').json())
                    report['checks'].append('email/password registration signs in with a private email and public display name')

                    a.locator('#profile-form input[name=display_name]').fill('林某')
                    a.locator('#profile-form input[name=affiliation]').fill('独立研究者')
                    a.locator('#profile-form button').click()
                    expect(a.locator('#who')).to_contain_text('独立研究者（作者自述）')
                    view(a, 'publish')
                    a.locator('#publish input[name=title]').fill('公开署名演示')
                    a.locator('#publish textarea[name=body]').fill('一项公开署名的演示探索')
                    a.locator('#publish button', has_text='发表').click()
                    expect(a.locator('#message')).to_contain_text('发表')
                    b.goto(origin)
                    expect(b.locator('#feed')).to_contain_text('独立研究者（作者自述）')
                    assert 'author@example.org' not in b.content()
                    view(a, 'account')
                    a.locator('#profile-form input[name=affiliation]').fill('')
                    a.locator('#profile-form button').click()
                    expect(a.locator('#who')).not_to_contain_text('独立研究者')
                    b.reload()
                    expect(b.locator('#feed')).not_to_contain_text('独立研究者')
                    b.set_viewport_size({'width': 390, 'height': 844})
                    assert b.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    report['checks'].append('author can edit or clear public affiliation; a reader sees self-declared affiliation without email')

                    assert sent[-1][:2] == ('author@example.org', 'verify')
                    a.locator('#verify-email input[name=code]').fill(sent[-1][2])
                    a.locator('#verify-email button').click()
                    expect(a.locator('#profile-contact')).to_contain_text('已验证')
                    report['checks'].append('single-use email verification completes through a local mail sink')

                    a2.goto(origin + '/account')
                    a2.locator('#password-login input[name=email]').fill('author@example.org')
                    a2.locator('#password-login input[name=password]').fill('A long private passphrase 2026')
                    a2.locator('#password-login button').click()
                    expect(a2.locator('#who')).to_contain_text('林某')
                    assert a2.request.get(origin + '/api/me').json()['id'] == actor
                    a.locator('#change-password-box summary').click()
                    a.locator('#change-password input[name=current_password]').fill('A long private passphrase 2026')
                    a.locator('#change-password input[name=new_password]').fill('Another private passphrase 2026')
                    a.locator('#change-password button').click()
                    expect(a.locator('#message')).to_contain_text('其他设备已退出')
                    view(a2, 'publish')
                    a2.locator('#publish input[name=title]').fill('失效设备检查')
                    a2.locator('#publish textarea[name=body]').fill('失效设备不应发表')
                    a2.locator('#publish button', has_text='发表').click()
                    expect(a2.locator('#message')).to_contain_text('会话已失效')
                    report['checks'].append('second device retains actor before password change and loses write access afterward')

                    a.locator('#logout-account').click()
                    expect(a.locator('#email-entry')).to_be_visible()
                    a.locator('#password-recovery summary').click()
                    a.locator('#request-reset input[name=email]').fill('author@example.org')
                    a.locator('#request-reset button').click()
                    expect(a.locator('#message')).to_contain_text('代码已发送')
                    assert sent[-1][:2] == ('author@example.org', 'reset')
                    a.locator('#reset-password input[name=email]').fill('author@example.org')
                    a.locator('#reset-password input[name=code]').fill(sent[-1][2])
                    a.locator('#reset-password input[name=new_password]').fill('Final private passphrase 2026')
                    a.locator('#reset-password button').click()
                    expect(a.locator('#message')).to_contain_text('密码已重设')
                    a.locator('#password-login input[name=email]').fill('author@example.org')
                    a.locator('#password-login input[name=password]').fill('Final private passphrase 2026')
                    a.locator('#password-login button').click()
                    expect(a.locator('#who')).to_contain_text('林某')
                    assert a.request.get(origin + '/api/me').json()['id'] == actor
                    assert not errors, errors
                    report['checks'].append('verified email reset restores the same author and old sessions remain revoked')
                    report['status'] = 'PASS'
                finally:
                    browser.close()
        finally:
            server.shutdown(); server.server_close(); thread.join()
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
