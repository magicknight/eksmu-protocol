"""Synthetic two-identity appeal drill with real Chromium and offline review commands."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile

from browser_accounts import ROOT, Server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-python', required=True)
    parser.add_argument('--browser', required=True)
    parser.add_argument('--library-path')
    parser.add_argument('--app-root', default=str(ROOT))
    parser.add_argument('--report', required=True)
    args = parser.parse_args()
    report = {'status': 'FAIL', 'demo': True, 'actual_user_data_used': False,
              'ssh_tunnel_tested': False, 'independent_humans_verified': False,
              'runtime': 'checkout' if Path(args.app_root) == ROOT else 'installed app in isolated temporary instance',
              'checks': []}
    from playwright.sync_api import sync_playwright, expect
    with tempfile.TemporaryDirectory(prefix='eksmu-appeal-browser-') as directory:
        db = Path(directory) / 'community.sqlite'
        reason = Path(directory) / 'review-reason.txt'
        reason.write_text('合成材料，仅用于离线流程验收', encoding='utf-8')
        server = Server(args.app_python, db, app_root=Path(args.app_root))
        def cli(*parts):
            completed = subprocess.run([args.app_python, '-m', 'eksmu', '--db', str(db), *parts],
                                       cwd=args.app_root, capture_output=True, text=True, timeout=15)
            return completed
        try:
            server.start()
            with sync_playwright() as pw:
                env = {'PATH': os.environ.get('PATH', os.defpath)}
                if args.library_path: env['LD_LIBRARY_PATH'] = args.library_path
                browser = pw.chromium.launch(executable_path=args.browser, headless=True,
                                             chromium_sandbox=True, env=env)
                try:
                    report['browser_version'] = browser.version
                    author, reader = [browser.new_context(accept_downloads=True, locale='zh-CN').new_page() for _ in range(2)]
                    errors = []
                    for page in (author, reader): page.on('pageerror', lambda _: errors.append('JavaScript page error'))
                    def register(page, alias):
                        page.goto(server.url)
                        page.locator('#register [name=alias]').fill(alias)
                        page.locator('#register button').click()
                        with page.expect_download() as download: page.locator('#backup').click()
                        assert json.loads(Path(download.value.path()).read_text())['format'] == 'eksmu.recovery.v1'
                        page.locator('#backup-confirmed').check()
                        page.locator('#confirm-account').click()
                        expect(page.locator('#who')).to_contain_text(alias)
                    register(author, '复议演示作者')
                    register(reader, '复议演示报告者')
                    author.locator('#publish [name=title]').fill('演示：有限行为复议')
                    author.locator('#publish [name=body]').fill('仅供私有验收的合成内容。')
                    author.get_by_role('button', name='发表', exact=True).click()
                    expect(author.locator('#detail-title')).to_have_text('演示：有限行为复议')
                    rid = author.locator('#versions').input_value()
                    wid = next(row['payload']['exploration_id'] for row in server.api('/api/state')[1]['records'] if row['id'] == rid)
                    reader.goto(server.url + '/#exploration=' + wid + '&revision=' + rid)
                    reader.locator('#behavior-report').locator('xpath=..').locator('summary').click()
                    reader.locator('#behavior-report [name=reason]').fill('合成反馈，请核对行为流程')
                    reader.locator('#behavior-report button').click()
                    expect(reader.locator('#behavior-cases')).to_contain_text('合成反馈')
                    case_id = next(iter(reader.evaluate("async()=> (await (await fetch('/api/community/behavior-cases')).json()).items.map(x=>x.id)")))
                    report['checks'].append('two browser identities publish and report without changing publication')

                    server.stop()
                    initial = cli('review-behavior', case_id, '--outcome', 'no_action', '--reason-file', str(reason),
                                  '--operator-id', 'operator_one')
                    assert initial.returncode == 0, initial.stderr
                    server.start()
                    reader.locator('#refresh-community').click()
                    expect(reader.locator('#behavior-cases')).to_contain_text('本地复核')
                    reader.locator('#behavior-cases').locator('xpath=..').locator('summary').click()
                    reader.locator('#behavior-cases [data-purpose=appeal]').fill('报告者请求再核对')
                    reader.route('**/api/community/behavior-appeal', lambda route: (route.fetch(), route.abort('failed')), times=1)
                    reader.locator('#behavior-cases button', has_text='申请复议').click()
                    expect(reader.locator('#message')).to_contain_text('复议提交结果未确认')
                    expect(reader.locator('#behavior-cases')).to_contain_text('报告者请求再核对')
                    author.locator('#refresh-community').click()
                    expect(author.locator('#behavior-cases')).to_contain_text('报告者请求再核对')
                    author_case = author.evaluate("async()=> (await (await fetch('/api/community/behavior-cases')).json()).items[0]")
                    assert 'reporter' not in author_case and author_case['events'][-1]['actor'] == 'reporter'
                    report['checks'].append('committed appeal response loss shows current case without retry or reporter ID disclosure')

                    server.stop()
                    denied = cli('review-behavior-appeal', case_id, '--outcome', 'uphold', '--reason-file', str(reason),
                                 '--operator-id', 'operator_one')
                    assert denied.returncode != 0
                    decided = cli('review-behavior-appeal', case_id, '--outcome', 'reopen', '--reason-file', str(reason),
                                  '--operator-id', 'operator_two')
                    assert decided.returncode == 0, decided.stderr
                    server.start()
                    reader.locator('#refresh-community').click()
                    author.locator('#refresh-community').click()
                    expect(reader.locator('#behavior-cases')).to_contain_text('需要重新核对')
                    author.locator('#behavior-cases').locator('xpath=..').locator('summary').click()
                    author.locator('#behavior-cases [data-purpose=appeal]').fill('相关作者申请复议')
                    author.locator('#behavior-cases button', has_text='申请复议').click()
                    expect(author.locator('#behavior-cases')).to_contain_text('相关作者申请复议')
                    report['checks'].append('offline appeal review rejects same operator label and permits other side to appeal later')
                    assert not errors, errors
                    report['status'] = 'PASS'
                finally:
                    browser.close()
        finally:
            server.stop()
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
