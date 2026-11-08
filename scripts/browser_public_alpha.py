"""Exercise public registration and research UI with synthetic local data.

Chromium sees an HTTPS test origin; Playwright forwards requests to the real
loopback app, preserving its origin, cookies and request bodies. This checks
the public UI contract, not public DNS, a TLS certificate or real SMTP.
"""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser', required=True)
    parser.add_argument('--app-root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.app_root.resolve()))
    from eksmu.server import make_server
    from playwright.sync_api import sync_playwright, expect

    report = {'status': 'FAIL', 'synthetic_users': 2,
              'runtime': str(args.app_root.resolve()),
              'transport': 'HTTPS test origin routed to real loopback HTTP application',
              'real_smtp_tested': False, 'public_dns_tested': False,
              'trusted_tls_tested': False, 'actual_user_data_touched': False,
              'checks': []}
    mail = []
    origin = 'https://eksmu.test'
    with tempfile.TemporaryDirectory(prefix='eksmu-public-browser-') as directory:
        database = Path(directory) / 'community.sqlite'
        server = make_server(database, 0, public_origin=origin,
                             mail_sender=lambda address, purpose, code: mail.append((address, purpose, code)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(executable_path=args.browser, headless=True)
                try:
                    report['browser_version'] = browser.version
                    contexts = [browser.new_context(locale='zh-CN'), browser.new_context(locale='zh-CN')]
                    pages = []
                    errors = []
                    for index, context in enumerate(contexts):
                        def forward(route, request, index=index):
                            request = route.request
                            parts = urlsplit(request.url)
                            headers = request.all_headers()
                            headers['host'] = 'eksmu.test'
                            headers['x-eksmu-client-ip'] = f'192.0.2.{index + 1}'
                            response = route.fetch(
                                url=f'http://127.0.0.1:{server.server_port}{parts.path}'
                                    + (f'?{parts.query}' if parts.query else ''),
                                headers=headers, max_redirects=0)
                            route.fulfill(response=response)
                        context.route(origin + '/**', forward)
                        page = context.new_page()
                        page.on('pageerror', lambda error: errors.append(str(error)))
                        pages.append(page)
                    a, b = pages

                    def register(page, address, alias):
                        page.goto(origin + '/account')
                        page.locator('#tab-register').click()
                        expect(page.locator('#email-register')).to_be_visible()
                        expect(page.locator('#legacy-account')).to_be_hidden()
                        page.locator('#email-register input[name=email]').fill(address)
                        page.locator('#email-register button').click()
                        expect(page.locator('#message')).to_contain_text('注册请求已接收')
                        expect(page.locator('#public-profile')).to_be_hidden()
                        deadline = time.monotonic() + 5
                        while not any(item[0] == address for item in mail):
                            if time.monotonic() >= deadline:
                                raise AssertionError('Local mail sink did not receive a registration code')
                            page.wait_for_timeout(50)
                        code = next(item[2] for item in reversed(mail) if item[0] == address)
                        page.locator('#confirm-registration input[name=code]').fill(code)
                        page.locator('#confirm-registration input[name=alias]').fill(alias)
                        page.locator('#confirm-registration input[name=password]').fill('A synthetic test password 2026')
                        page.locator('#confirm-registration button').click()
                        expect(page.locator('#public-profile')).to_be_visible()
                        expect(page.locator('#profile-contact')).to_contain_text('已验证')
                        session = next(cookie for cookie in page.context.cookies() if cookie['name'] == f'eksmu_session_{server.server_port}')
                        assert session['secure'] and session['httpOnly'] and session['sameSite'] == 'Strict'
                        expect(page.locator('#rotate-recovery')).to_be_hidden()

                    register(a, 'author@example.org', '合成作者')
                    register(b, 'reader@example.org', '合成回应者')
                    report['checks'].append('two public accounts require emailed confirmation before a Secure HttpOnly session; legacy registration stays hidden')
                    a.goto(origin + '/publish')
                    expect(a.locator('#publication-audience')).to_be_visible()
                    a.locator('#publish input[name=title]').fill('合成公开 Alpha 验收')
                    a.locator('#publish textarea[name=body]').fill('一个用于公开模式验收的合成问题。')
                    a.locator('#publish button', has_text='发表').click()
                    expect(a.locator('#message')).to_contain_text('已发表到公开站点')
                    expect(a.locator('#detail-title')).to_have_text('合成公开 Alpha 验收')
                    work, revision = a.evaluate('[selected, revision]')
                    a.locator('#attachment-editor > summary').click()
                    a.locator('#material-input').set_input_files({'name': 'synthetic.csv', 'mimeType': 'text/csv', 'buffer': b'x,y\n1,2\n'})
                    a.locator('#upload-material button').click()
                    expect(a.locator('#research-files')).to_contain_text('synthetic.csv')
                    b.goto(origin + f'/#exploration={work}&revision={revision}')
                    expect(b.locator('#detail-title')).to_have_text('合成公开 Alpha 验收')
                    b.locator('#comment textarea[name=body]').fill('合成回应：建议检查边界值。')
                    b.locator('#comment button').click()
                    expect(b.locator('#comments')).to_contain_text('建议检查边界值')
                    a.goto(origin + f'/#exploration={work}&revision={revision}')
                    expect(a.locator('#comments')).to_contain_text('建议检查边界值')
                    a.locator('#edit-box > summary').click()
                    a.locator('#revise textarea[name=body]').fill('根据合成回应补充了边界值。')
                    a.locator('#revise input[name=note]').fill('合成修订')
                    a.locator('#revise button', has_text='保存新版本').click()
                    expect(a.locator('#message')).to_contain_text('新版本已保存')
                    expect(a.locator('#versions option')).to_have_count(2)
                    a.locator('#versions').select_option(revision)
                    expect(a.locator('#comments')).to_contain_text('建议检查边界值')
                    report['checks'].append('public author publishes and uploads a small attachment; second account responds; author revises and old-version discussion remains')
                    a.locator('#snapshot-editor > summary').click()
                    a.locator('#snapshot-materials input').check()
                    a.locator('#snapshot-create button').click()
                    expect(a.locator('#publication-snapshots a')).to_have_count(1)
                    a.locator('#publication-snapshots a').click()
                    expect(a.locator('#snapshot-title')).to_have_text('合成公开 Alpha 验收')
                    expect(a.locator('#snapshot-files')).to_contain_text('synthetic.csv')
                    report['checks'].append('selected old version and attachment create a readable immutable publication snapshot')
                    b.set_viewport_size({'width': 390, 'height': 844})
                    b.goto(origin + '/founding.html')
                    expect(b.locator('main')).to_contain_text('没有已确认的合作关系')
                    assert b.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    b.goto(origin + '/participation.html')
                    expect(b.locator('main')).to_contain_text('所有附件')
                    assert b.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    assert not errors, errors
                    report['checks'].append('activity and participation pages show actual scope and attachment visibility at 390px without script errors')
                    report['status'] = 'PASS'
                finally:
                    browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'status': report['status'], 'checks': len(report['checks']), 'report': str(args.report)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
