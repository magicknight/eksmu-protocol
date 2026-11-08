"""Check a real browser link into the private home page without writing app data.

The link source uses localhost; the target must use 127.0.0.1, making the
navigation cross-site. No browser headers or HTTP responses are substituted.
Playwright and a Chromium executable are optional test-only dependencies.
"""
import argparse
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
from urllib.parse import urlsplit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8765')
    parser.add_argument('--browser', required=True)
    parser.add_argument('--library-path')
    parser.add_argument('--expect-status', type=int, default=200)
    parser.add_argument('--report', required=True)
    args = parser.parse_args()
    target = args.base_url.rstrip('/') + '/'
    url = urlsplit(target)
    if (url.scheme != 'http' or url.hostname != '127.0.0.1' or not url.port
            or url.username or url.password or url.path != '/' or url.query or url.fragment):
        parser.error('Use a plain http://127.0.0.1:PORT home URL.')

    class LinkPage(BaseHTTPRequestHandler):
        def do_GET(self):
            data = ('<!doctype html><a id="visit" href="' + escape(target, quote=True)
                    + '">Open private community</a>').encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_):
            pass

    source = ThreadingHTTPServer(('127.0.0.1', 0), LinkPage)
    thread = threading.Thread(target=source.serve_forever, daemon=True)
    thread.start()
    report = {'status': 'FAIL', 'target': target, 'app_writes': False,
              'transport': 'real Chromium HTTP; user clicks cross-site link',
              'ssh_tunnel_tested': False, 'expected_home_status': args.expect_status}
    try:
        from playwright.sync_api import sync_playwright, expect
        env = {'PATH': os.environ.get('PATH', os.defpath)}
        if args.library_path:
            env['LD_LIBRARY_PATH'] = args.library_path
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=args.browser,
                headless=True, chromium_sandbox=True, env=env)
            try:
                report['browser_version'] = browser.version
                page = browser.new_page(locale='zh-CN')
                failures = []
                api_statuses = []
                page.on('pageerror', lambda _: failures.append('JavaScript page error'))

                def response_received(response):
                    path = urlsplit(response.url).path
                    if path.startswith('/api/'):
                        api_statuses.append({'path': path, 'status': response.status})
                    if response.status >= 400 and (path.startswith('/api/')
                            or path.endswith(('.js', '.css'))):
                        failures.append(f'HTTP {response.status}: {path}')

                page.on('response', response_received)
                page.goto(f'http://localhost:{source.server_port}/', timeout=15000)
                with page.expect_response(lambda response: response.url == target, timeout=15000) as pending:
                    page.locator('#visit').click()
                response = pending.value
                headers = response.request.all_headers()
                report['request_metadata'] = {key: headers.get(key) for key in
                    ('sec-fetch-site', 'sec-fetch-mode', 'sec-fetch-dest', 'sec-fetch-user')}
                report['home_status'] = response.status
                assert headers.get('sec-fetch-site') == 'cross-site', 'Browser did not generate a cross-site request'
                assert response.status == args.expect_status, f'Unexpected home status: {response.status}'
                if args.expect_status == 200:
                    page.wait_for_load_state('networkidle', timeout=15000)
                    expect(page.locator('#feed')).to_be_visible()
                    page.evaluate('showView("account")')
                    expect(page.locator('#password-login')).to_be_visible()
                    page.locator('#tab-register').click()
                    expect(page.locator('#email-register')).to_be_visible()
                    expect(page.locator('#account-loading')).to_be_hidden()
                    assert api_statuses, 'No application API requests observed'
                    assert not failures, failures
                    report['rendered_home_and_same_origin_api'] = 'PASS'
                else:
                    report['error_response'] = response.json()
                report['api_responses'] = api_statuses
                report['status'] = 'PASS'
            finally:
                browser.close()
    except Exception as exc:
        report['error'] = str(exc)
    finally:
        source.shutdown()
        source.server_close()
        thread.join()
    destination = Path(args.report)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
