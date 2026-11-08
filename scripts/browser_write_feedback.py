"""Synthetic browser drill for uncertain writes and confirmed writes with failed refresh."""
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
            'fault_injection':'Committed POST response aborted for publish, comment, revision and upload; read refresh aborted after confirmed upload and after confirmed publication.',
            'checks':[]}
    from playwright.sync_api import sync_playwright,expect
    with tempfile.TemporaryDirectory(prefix='eksmu-write-feedback-') as directory:
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
                    author,reader=[browser.new_context(accept_downloads=True, locale='zh-CN').new_page() for _ in range(2)]
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
                        response=route.fetch();assert response.status in (200,201),response.status
                        route.abort('failed')
                    def records():return server.api('/api/state')[1]['records']
                    register(author,'写入演示作者');register(reader,'写入演示读者')
                    author.locator('#publish [name=title]').fill('演示：原始版本')
                    author.locator('#publish [name=body]').fill('合成的初始正文。')
                    author.get_by_role('button',name='发表',exact=True).click()
                    expect(author.locator('#detail-title')).to_have_text('演示：原始版本')
                    original=author.locator('#versions').input_value()
                    work=next(row['payload']['exploration_id'] for row in records() if row['id']==original)

                    author.locator('#publish [name=title]').fill('演示：响应丢失的新探索')
                    author.locator('#publish [name=body]').fill('发表响应丢失后必须留下的草稿。')
                    author.route('**/api/explorations',lose,times=1)
                    author.get_by_role('button',name='发表',exact=True).click()
                    expect(author.locator('#message')).to_contain_text('提交结果未确认')
                    expect(author.locator('#publish [name=body]')).to_have_value('发表响应丢失后必须留下的草稿。')
                    assert sum(row['type']=='exploration' for row in records())==2
                    report['checks'].append('committed publish response loss keeps draft, refreshes discovery and does not submit twice')

                    reader.goto(server.url+'/#exploration='+work+'&revision='+original)
                    expect(reader.locator('#versions')).to_have_value(original)
                    reader.locator('#comment [name=body]').fill('读者响应丢失的评论')
                    reader.route('**/api/comments',lose,times=1)
                    reader.locator('#comment button').click()
                    expect(reader.locator('#message')).to_contain_text('提交结果未确认')
                    expect(reader.locator('#comment [name=body]')).to_have_value('读者响应丢失的评论')
                    expect(reader.locator('#comments')).to_contain_text('读者响应丢失的评论')
                    assert sum(row['type']=='comment' for row in records())==1
                    report['checks'].append('committed comment response loss keeps input and displays one version-bound comment')

                    author.locator('#edit-box > summary').click()
                    author.locator('#revise [name=title]').fill('演示：修订版')
                    author.locator('#revise [name=body]').fill('响应丢失后保留的修订正文。')
                    author.route('**/api/revisions',lose,times=1)
                    author.get_by_role('button',name='保存新版本').click()
                    expect(author.locator('#message')).to_contain_text('提交结果未确认')
                    expect(author.locator('#revise [name=body]')).to_have_value('响应丢失后保留的修订正文。')
                    revisions=[row for row in records() if row['type']=='revision' and row['payload']['exploration_id']==work]
                    assert len(revisions)==2
                    latest=revisions[-1]['id']
                    report['checks'].append('committed revision response loss keeps editor input and creates one new version')
                    author.get_by_role('button',name='保存新版本').click()
                    expect(author.locator('#message')).to_contain_text('Stale or invalid parent revision')
                    expect(author.locator('#revise [name=body]')).to_have_value('响应丢失后保留的修订正文。')
                    assert len([row for row in records() if row['type']=='revision' and row['payload']['exploration_id']==work])==2
                    report['checks'].append('explicit stale revision retry is rejected without clearing editor text or creating another version')

                    author.locator('#versions').select_option(latest)
                    expect(author.locator('#attachment-editor')).not_to_be_hidden()
                    author.locator('#attachment-editor > summary').click()
                    material={'name':'response-loss.txt','mimeType':'text/plain','buffer':b'synthetic material\n'}
                    author.locator('#material-input').set_input_files(material)
                    author.route('**/api/research/upload',lose,times=1)
                    author.locator('#upload-material button').click()
                    expect(author.locator('#message')).to_contain_text('附件提交结果未确认')
                    expect(author.locator('#material-input')).to_have_attribute('type','file')
                    expect(author.locator('#research-files')).to_contain_text('response-loss.txt')
                    assert len(server.api('/api/research/materials?revision_id='+latest)[1]['items'])==1
                    report['checks'].append('committed upload response loss refreshes materials and leaves one attachment without automatic retry')

                    author.locator('#material-input').set_input_files({'name':'confirmed.txt','mimeType':'text/plain','buffer':b'confirmed\n'})
                    author.route('**/api/research/workspace?revision_id=*',lambda route:route.abort('failed'),times=1)
                    author.locator('#upload-material button').click()
                    expect(author.locator('#message')).to_contain_text('附件已写入，但材料列表暂时无法刷新')
                    assert len(server.api('/api/research/materials?revision_id='+latest)[1]['items'])==2
                    report['checks'].append('confirmed upload stays confirmed when its subsequent list refresh fails')

                    author.locator('#publish [name=title]').fill('演示：确认写入后读取失败')
                    author.locator('#publish [name=body]').fill('这次写入已有成功响应。')
                    author.route('**/api/state',lambda route:route.abort('failed'),times=1)
                    author.get_by_role('button',name='发表',exact=True).click()
                    expect(author.locator('#message')).to_contain_text('写入已确认，但页面暂时无法刷新')
                    assert sum(row['type']=='exploration' for row in records())==3
                    report['checks'].append('confirmed publication stays confirmed when subsequent state refresh fails')
                    assert not errors,errors
                    report['status']='PASS'
                finally:browser.close()
        finally:server.stop()
    Path(args.report).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')


if __name__=='__main__':main()
