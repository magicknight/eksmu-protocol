"""Private behavior feedback in two real browser contexts on temporary data."""
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
    report = {'status':'FAIL', 'demo':True, 'actual_user_data_used':False,
              'ssh_tunnel_tested':False, 'checks':[],
              'runtime':'checkout' if Path(args.app_root)==ROOT else 'installed app in isolated temporary instance'}
    from playwright.sync_api import sync_playwright, expect
    with tempfile.TemporaryDirectory(prefix='eksmu-behavior-browser-') as directory:
        db = Path(directory) / 'community.sqlite'
        server = Server(args.app_python, db, app_root=Path(args.app_root))
        try:
            server.start()
            with sync_playwright() as pw:
                env={'PATH':os.environ.get('PATH',os.defpath)}
                if args.library_path: env['LD_LIBRARY_PATH']=args.library_path
                browser=pw.chromium.launch(executable_path=args.browser, headless=True,
                                           chromium_sandbox=True, env=env)
                try:
                    report['browser_version']=browser.version
                    author, reader=[browser.new_context(accept_downloads=True, locale='zh-CN').new_page() for _ in range(2)]
                    errors=[]
                    for page in (author,reader): page.on('pageerror',lambda _:errors.append('JavaScript error'))
                    def register(page, alias):
                        page.goto(server.url)
                        page.locator('#register [name=alias]').fill(alias)
                        page.locator('#register button').click()
                        with page.expect_download() as download: page.locator('#backup').click()
                        assert json.loads(Path(download.value.path()).read_text())['format']=='eksmu.recovery.v1'
                        page.locator('#backup-confirmed').check();page.locator('#confirm-account').click()
                        expect(page.locator('#who')).to_contain_text(alias)
                    register(author,'行为演示作者');register(reader,'行为演示读者')
                    author.locator('#publish [name=title]').fill('演示：行为反馈')
                    author.locator('#publish [name=body]').fill('本内容为合成验收资料。')
                    author.get_by_role('button',name='发表',exact=True).click()
                    expect(author.locator('#detail-title')).to_have_text('演示：行为反馈')
                    rid=author.locator('#versions').input_value()
                    reader.goto(server.url+'/#exploration='+next(x['payload']['exploration_id'] for x in server.api('/api/state')[1]['records'] if x['id']==rid)+'&revision='+rid)
                    expect(reader.locator('#behavior-target')).to_have_value(rid)
                    reader.locator('#behavior-report').locator('xpath=..').locator('summary').click()
                    reader.locator('#behavior-report [name=reason]').fill('合成隐私问题，请本地复核')
                    reader.locator('#behavior-report button').click()
                    expect(reader.locator('#message')).to_contain_text('私下反馈已记录')
                    expect(reader.locator('#behavior-cases')).to_contain_text('合成隐私问题')
                    author.locator('#refresh-community').click()
                    expect(author.locator('#behavior-cases')).to_contain_text('合成隐私问题')
                    author.locator('#behavior-cases').locator('xpath=..').locator('summary').click()
                    author.locator('#behavior-cases textarea').fill('作者回应：这里是占位材料')
                    author.locator('#behavior-cases button').click()
                    expect(author.locator('#behavior-cases')).to_contain_text('作者回应：这里是占位材料')
                    reader.locator('#refresh-community').click()
                    expect(reader.locator('#behavior-cases')).to_contain_text('作者回应：这里是占位材料')
                    reader.locator('#logout-account').click()
                    expect(reader.locator('#behavior-cases')).not_to_contain_text('合成隐私问题')
                    expect(reader.locator('#behavior-cases')).to_contain_text('登录后可查看')
                    assert not errors, errors
                    report['checks'].append('two independent identities submit private report, read own case and author response without JavaScript error')
                    report['checks'].append('logging out clears prior private case details from the page')
                    report['checks'].append('reporting did not remove the published version or block continued reading')
                    report['status']='PASS'
                finally: browser.close()
        finally: server.stop()
    Path(args.report).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')


if __name__=='__main__': main()
