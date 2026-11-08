"""Synthetic Chromium drill for private reports on attachments and collaboration events."""
import argparse
import json
import os
from pathlib import Path
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
              'ssh_tunnel_tested': False, 'runtime': 'checkout' if Path(args.app_root) == ROOT
              else 'installed app in isolated temporary instance', 'checks': []}
    from playwright.sync_api import sync_playwright, expect
    with tempfile.TemporaryDirectory(prefix='eksmu-behavior-targets-') as directory:
        server = Server(args.app_python, Path(directory) / 'community.sqlite', app_root=Path(args.app_root))
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
                    register(author, '附件演示作者')
                    register(reader, '协作演示参与者')
                    author.locator('#publish [name=title]').fill('演示：具体研究材料反馈')
                    author.locator('#publish [name=body]').fill('合成正文。')
                    author.get_by_role('button', name='发表', exact=True).click()
                    expect(author.locator('#detail-title')).to_have_text('演示：具体研究材料反馈')
                    revision = author.locator('#versions').input_value()
                    work = next(row['payload']['exploration_id'] for row in server.api('/api/state')[1]['records'] if row['id'] == revision)
                    author.locator('#attachment-editor > summary').click()
                    author.locator('#material-input').set_input_files({'name':'notes.txt','mimeType':'text/plain',
                                                                        'buffer':b'Synthetic file for a private browser drill'})
                    author.locator('#upload-material button').click()
                    expect(author.locator('#research-files')).to_contain_text('notes.txt')
                    file_id = author.evaluate("async()=> (await (await fetch('/api/research/workspace?revision_id=' + document.querySelector('#versions').value)).json()).materials.items[0].id")
                    expect(author.locator('#behavior-target')).to_contain_text('附件')
                    report['checks'].append('author upload exposes exact attachment ID in the private report target selector')

                    reader.goto(server.url + '/#exploration=' + work + '&revision=' + revision)
                    expect(reader.locator('#research-files')).to_contain_text('notes.txt')
                    reader.locator('#create-collaboration').locator('xpath=..').locator('summary').click()
                    reader.locator('#create-collaboration [name=title]').fill('复核这份材料')
                    reader.locator('#create-collaboration [name=description]').fill('合成协作记录')
                    reader.locator('#create-collaboration button').click()
                    expect(reader.locator('#research-tasks')).to_contain_text('复核这份材料')
                    event_id = reader.evaluate("async()=> (await (await fetch('/api/research/workspace?revision_id=' + document.querySelector('#versions').value)).json()).tasks[0].events[0].id")
                    expect(reader.locator('#behavior-target')).to_contain_text('协作记录')
                    assert reader.locator('#behavior-target option', has_text='notes.txt').count() == 1
                    assert reader.locator('#behavior-target option', has_text='协作记录').count() == 1
                    report['checks'].append('collaboration creation exposes exact event ID, not only a task container')

                    reader.locator('#behavior-report').locator('xpath=..').locator('summary').click()
                    reader.locator('#behavior-target').select_option(file_id)
                    reader.locator('#behavior-report [name=reason]').fill('合成附件隐私反馈')
                    reader.locator('#behavior-report button').click()
                    expect(reader.locator('#behavior-cases')).to_contain_text('合成附件隐私反馈')
                    author.locator('#refresh-community').click()
                    expect(author.locator('#behavior-target')).to_contain_text('协作记录')
                    author.locator('#behavior-report').locator('xpath=..').locator('summary').click()
                    author.locator('#behavior-target').select_option(event_id)
                    author.locator('#behavior-report [name=reason]').fill('合成协作行为反馈')
                    author.locator('#behavior-report button').click()
                    expect(author.locator('#behavior-cases')).to_contain_text('合成协作行为反馈')
                    author_cases = author.evaluate("async()=> (await (await fetch('/api/community/behavior-cases')).json()).items")
                    reader_cases = reader.evaluate("async()=> (await (await fetch('/api/community/behavior-cases')).json()).items")
                    assert {case['target'] for case in author_cases} == {file_id, event_id}
                    assert {case['target'] for case in reader_cases} == {file_id, event_id}
                    assert all('reporter' not in case for case in author_cases if case['target'] == file_id)
                    assert all('reporter' not in case for case in reader_cases if case['target'] == event_id)
                    assert server.api('/api/community/feed')[1]['total'] == 1
                    report['checks'].append('both reports remain private to related parties, hide reporter IDs from target authors and preserve publication')
                    assert not errors, errors
                    report['status'] = 'PASS'
                finally: browser.close()
        finally: server.stop()
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
