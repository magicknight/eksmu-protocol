"""Synthetic browser drill for remaining community and private feedback writes."""
import argparse
import json
import os
from pathlib import Path
import tempfile

from browser_accounts import ROOT, Server


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-python',required=True)
    parser.add_argument('--browser',required=True)
    parser.add_argument('--library-path')
    parser.add_argument('--app-root',default=str(ROOT))
    parser.add_argument('--report',required=True)
    args=parser.parse_args()
    report={'status':'FAIL','demo':True,'actual_user_data_used':False,'ssh_tunnel_tested':False,
            'runtime':'checkout' if Path(args.app_root)==ROOT else 'installed app in isolated temporary instance',
            'fault_injection':'Committed POST response aborted for introduction, follow, read, personal import and behavior response; one behavior list refresh aborted after confirmed response.',
            'checks':[]}
    from playwright.sync_api import sync_playwright,expect
    with tempfile.TemporaryDirectory(prefix='eksmu-personal-feedback-') as directory:
        server=Server(args.app_python,Path(directory)/'community.sqlite',app_root=Path(args.app_root))
        try:
            server.start()
            with sync_playwright() as pw:
                env={'PATH':os.environ.get('PATH',os.defpath)}
                if args.library_path:env['LD_LIBRARY_PATH']=args.library_path
                browser=pw.chromium.launch(executable_path=args.browser,headless=True,
                                           chromium_sandbox=True,env=env)
                try:
                    report['browser_version']=browser.version
                    ca=browser.new_context(accept_downloads=True, locale='zh-CN')
                    author=ca.new_page();reader=browser.new_context(accept_downloads=True, locale='zh-CN').new_page()
                    errors=[]
                    for page in (author,reader):page.on('pageerror',lambda _:errors.append('JavaScript error'))
                    def register(page,alias):
                        page.goto(server.url)
                        page.locator('#register [name=alias]').fill(alias)
                        page.locator('#register button').click()
                        with page.expect_download() as download:page.locator('#backup').click()
                        assert json.loads(Path(download.value.path()).read_text())['format']=='eksmu.recovery.v1'
                        page.locator('#backup-confirmed').check();page.locator('#confirm-account').click()
                        expect(page.locator('#who')).to_contain_text(alias)
                    def lose(route):
                        response=route.fetch();assert response.status==200,response.status
                        route.abort('failed')
                    register(author,'个人反馈作者');register(reader,'个人反馈读者')
                    author.locator('#publish [name=title]').fill('演示：个人写入反馈')
                    author.locator('#publish [name=body]').fill('合成的社区介绍与反馈材料。')
                    author.get_by_role('button',name='发表',exact=True).click()
                    expect(author.locator('#detail-title')).to_have_text('演示：个人写入反馈')
                    rid=author.locator('#versions').input_value()
                    wid=next(row['payload']['exploration_id'] for row in server.api('/api/state')[1]['records'] if row['id']==rid)
                    author.locator('#community-edit > summary').click()
                    author.locator('#community-details [name=topics]').fill('数学')
                    author.locator('#community-details [name=help]').fill('初始介绍')
                    author.locator('#community-details button').first.click()
                    expect(author.locator('#detail-help')).to_contain_text('初始介绍')

                    second=ca.new_page();second.on('pageerror',lambda _:errors.append('JavaScript error'))
                    second.goto(server.url+'/#exploration='+wid+'&revision='+rid)
                    expect(second.locator('#detail-help')).to_contain_text('初始介绍')
                    second.locator('#community-edit > summary').click()
                    second.locator('#community-details [name=help]').fill('遇到并发时保留的草稿')
                    author.locator('#community-details [name=help]').fill('另一页面先保存的新介绍')
                    author.locator('#community-details button').first.click()
                    expect(author.locator('#detail-help')).to_contain_text('另一页面先保存的新介绍')
                    second.locator('#community-details button').first.click()
                    expect(second.locator('#message')).to_contain_text('介绍已有新版本')
                    expect(second.locator('#community-details [name=help]')).to_have_value('遇到并发时保留的草稿')
                    expect(second.locator('#community-save-state')).to_contain_text('另一页面先保存的新介绍')
                    second.locator('#community-use-current').click()
                    second.locator('#community-details button').first.click()
                    expect(second.locator('#detail-help')).to_contain_text('遇到并发时保留的草稿')
                    report['checks'].append('409 introduction conflict preserves draft, displays current server text and requires explicit version acknowledgement')
                    second.locator('#community-details [name=help]').fill('响应丢失后保留的介绍草稿')
                    second.route('**/api/community/details',lose,times=1)
                    second.locator('#community-details button').first.click()
                    expect(second.locator('#message')).to_contain_text('保存结果未确认')
                    expect(second.locator('#community-details [name=help]')).to_have_value('响应丢失后保留的介绍草稿')
                    expect(second.locator('#community-save-state')).to_contain_text('响应丢失后保留的介绍草稿')
                    report['checks'].append('committed introduction response loss preserves draft and shows current server version without automatic retry')

                    reader.goto(server.url+'/#exploration='+wid+'&revision='+rid)
                    expect(reader.locator('#follow-work')).to_have_text('关注后续进展')
                    reader.route('**/api/community/mark',lose,times=1)
                    reader.locator('#follow-work').click()
                    expect(reader.locator('#message')).to_contain_text('操作结果未确认')
                    expect(reader.locator('#follow-work')).to_have_text('取消关注')
                    report['checks'].append('committed follow response loss refreshes displayed state without automatic toggle retry')

                    author.locator('#edit-box > summary').click()
                    author.locator('#revise [name=body]').fill('用于已读检查的新版本。')
                    author.get_by_role('button',name='保存新版本').click()
                    reader.locator('#refresh-community').click()
                    expect(reader.locator('#notice-count')).to_contain_text('未读动态')
                    reader.route('**/api/community/read',lose,times=1)
                    reader.locator('#read-notices').click()
                    expect(reader.locator('#message')).to_contain_text('已读操作结果未确认')
                    expect(reader.locator('#notice-count')).to_contain_text('暂时没有未读动态')
                    report['checks'].append('committed read response loss refreshes unread count and does not mark twice')

                    bundle=reader.evaluate("async()=>await (await fetch('/api/community/export')).json()")
                    reader.on('dialog',lambda dialog:dialog.accept())
                    reader.route('**/api/community/import',lose,times=1)
                    reader.locator('#import-personal').set_input_files({'name':'personal.json','mimeType':'application/json','buffer':json.dumps(bundle,ensure_ascii=False).encode()})
                    expect(reader.locator('#message')).to_contain_text('个人清单恢复结果未确认')
                    exported=reader.evaluate("async()=>await (await fetch('/api/community/export')).json()")
                    assert exported['following']==bundle['following']
                    report['checks'].append('committed personal import response loss shows uncertainty and leaves source file on the test client')

                    reader.locator('#behavior-report').locator('xpath=..').locator('summary').click()
                    reader.locator('#behavior-report [name=reason]').fill('合成行为反馈')
                    reader.locator('#behavior-report button').click()
                    expect(reader.locator('#behavior-cases')).to_contain_text('合成行为反馈')
                    author.locator('#refresh-community').click()
                    author.locator('#behavior-cases').locator('xpath=..').locator('summary').click()
                    expect(author.locator('#behavior-cases')).to_contain_text('合成行为反馈')
                    author.locator('#behavior-cases textarea').fill('响应丢失后的作者回应草稿')
                    author.route('**/api/community/behavior-response',lose,times=1)
                    author.locator('#behavior-cases button').click()
                    expect(author.locator('#message')).to_contain_text('回应结果未确认')
                    expect(author.locator('#behavior-cases textarea')).to_have_value('响应丢失后的作者回应草稿')
                    cases=author.evaluate("async()=>await (await fetch('/api/community/behavior-cases')).json()")
                    assert len([e for e in cases['items'][0]['events'] if e['kind']=='response'])==1
                    report['checks'].append('committed behavior response loss retains draft, shows one response and does not resubmit')

                    author.locator('#behavior-cases textarea').fill('成功写入但列表读取失败的回应')
                    author.route('**/api/community/behavior-cases',lambda route:route.abort('failed'),times=1)
                    author.locator('#behavior-cases button').click()
                    expect(author.locator('#message')).to_contain_text('回应已写入，但反馈列表暂时无法刷新')
                    cases=author.evaluate("async()=>await (await fetch('/api/community/behavior-cases')).json()")
                    assert len([e for e in cases['items'][0]['events'] if e['kind']=='response'])==2
                    report['checks'].append('confirmed behavior response remains confirmed when subsequent case-list read fails')
                    assert not errors,errors
                    report['status']='PASS'
                finally:browser.close()
        finally:server.stop()
    Path(args.report).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')


if __name__=='__main__':main()
