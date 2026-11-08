"""Synthetic browser enrollment, offline real Lean checks and backup restore."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
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
            'official_submission':False,'semantic_review_completed':False,
            'runtime':'checkout' if Path(args.app_root)==ROOT else 'installed app in isolated temporary instance',
            'checks':[]}
    from playwright.sync_api import sync_playwright,expect
    with tempfile.TemporaryDirectory(prefix='eksmu-lean-browser-') as directory:
        base=Path(directory);data=base/'data';data.mkdir();db=data/'community.sqlite'
        server=Server(args.app_python,db,app_root=Path(args.app_root))
        def cli(*parts,database=db):
            return subprocess.run([args.app_python,'-m','eksmu','--db',str(database),*map(str,parts)],
                                  cwd=args.app_root,capture_output=True,text=True,timeout=60)
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
                    for page in (author,reader):page.on('pageerror',lambda _:errors.append('JavaScript page error'))
                    def register(page,alias):
                        page.goto(server.url)
                        page.locator('#register [name=alias]').fill(alias)
                        page.locator('#register button').click()
                        with page.expect_download() as download:page.locator('#backup').click()
                        recovery=json.loads(Path(download.value.path()).read_text())
                        assert recovery['format']=='eksmu.recovery.v1'
                        page.locator('#backup-confirmed').check();page.locator('#confirm-account').click()
                        expect(page.locator('#who')).to_contain_text(alias)
                    register(author,'Lean演示作者');register(reader,'Lean演示读者')
                    author.locator('#publish [name=title]').fill('演示：自愿Lean预检')
                    author.locator('#publish [name=body]').fill('这是一份合成数学探索，发表不取决于Lean。')
                    author.get_by_role('button',name='发表',exact=True).click()
                    expect(author.locator('#detail-title')).to_have_text('演示：自愿Lean预检')
                    revision=author.locator('#versions').input_value()
                    work=next(x['payload']['exploration_id'] for x in server.api('/api/state')[1]['records'] if x['id']==revision)
                    author.locator('#attachment-editor > summary').click()
                    files=[('Valid.lean',b'theorem demo_valid : True := by trivial\n'),
                           ('Sorry.lean',b'theorem demo_sorry : True := by sorry\n'),
                           ('Withdraw.lean',b'theorem demo_withdraw : True := by trivial\n')]
                    for name,body in files:
                        author.locator('#material-input').set_input_files({'name':name,'mimeType':'text/plain','buffer':body})
                        author.locator('#upload-material button').click()
                        expect(author.locator('#research-files')).to_contain_text(name)
                    materials=author.evaluate("async()=> (await (await fetch('/api/research/workspace?revision_id=' + document.querySelector('#versions').value)).json()).materials.items")
                    ids={item['filename']:item['id'] for item in materials}
                    expect(author.locator('#lean-activity')).to_be_visible()
                    author.locator('#lean-activity > details > summary').click()
                    def enroll(name,target,reference):
                        author.locator('#lean-attachment').select_option(ids[name])
                        author.locator('#lean-enroll [name=target]').fill(target)
                        author.locator('#lean-enroll [name=problem_reference]').fill(reference)
                        author.locator('#lean-enroll [name=consent]').check()
                        author.locator('#lean-enroll button').click()
                        expect(author.locator('#message')).to_contain_text('已加入私有预检队列')
                    enroll('Valid.lean','demo_valid','作者自述问题来源')
                    enroll('Sorry.lean','demo_sorry','作者自述问题来源')
                    enroll('Withdraw.lean','demo_withdraw','作者自述问题来源')
                    expect(author.locator('#lean-enrollments article')).to_have_count(3)
                    author.locator('#lean-enrollments article').first.get_by_role('button',name='退出这项私有检查').click()
                    expect(author.locator('#lean-enrollments article').first).to_contain_text('withdrawn')
                    report['checks'].append('author opts in three fixed Lean attachments and withdraws one before execution')

                    reader.goto(server.url+'/#exploration='+work+'&revision='+revision)
                    expect(reader.locator('#lean-activity')).to_be_hidden()
                    assert reader.evaluate("async()=> (await (await fetch('/api/research/lean-enrollments')).json()).items")==[]
                    reader.locator('#publish [name=title]').fill('无需形式化也能发表')
                    reader.locator('#publish [name=body]').fill('普通读者的合成探索。')
                    reader.get_by_role('button',name='发表',exact=True).click()
                    expect(reader.locator('#detail-title')).to_have_text('无需形式化也能发表')
                    report['checks'].append('other identity cannot see private queue and can publish without Lean')

                    server.stop()
                    first=cli('run-lean-queue','--offline')
                    second=cli('run-lean-queue','--offline')
                    empty=cli('run-lean-queue','--offline')
                    assert first.returncode==second.returncode==empty.returncode==0,(first.stderr,second.stderr,empty.stderr)
                    assert json.loads(first.stdout)['mechanical_status']=='lean_process_ok'
                    assert json.loads(second.stdout)['mechanical_status']=='lean_process_ok'
                    assert json.loads(empty.stdout)=={'queued':False,'executed':False}
                    report['checks'].append('offline exclusive worker executes exactly two opted-in sources with real pinned Lean')
                    server.start()
                    author.goto(server.url+'/#exploration='+work+'&revision='+revision)
                    expect(author.locator('#lean-activity')).to_be_visible()
                    author.locator('#lean-activity > details > summary').click()
                    expect(author.locator('#lean-enrollments article')).to_have_count(3)
                    private=author.evaluate("async()=> (await (await fetch('/api/research/lean-enrollments')).json()).items")
                    checked=[item for item in private if item['state']=='checked']
                    assert len(checked)==2
                    by_target={item['request']['target']:item for item in checked}
                    assert by_target['demo_valid']['report']['sandbox_setup_completed'] is True
                    assert by_target['demo_valid']['report']['sorry_axiom_observed'] is False
                    assert by_target['demo_sorry']['report']['sorry_axiom_observed'] is True
                    assert all(item['report']['official_prize_review']=='not_submitted' for item in checked)
                    assert next(item for item in private if item['request']['target']=='demo_withdraw')['report'] is None
                    author.reload()
                    author.locator('#lean-activity > details > summary').click()
                    draft_button=author.locator('#lean-enrollments article').filter(has_text='demo_valid').get_by_role(
                        'button',name='下载本地提交准备草稿')
                    expect(draft_button).to_be_visible()
                    with author.expect_download() as downloaded:
                        draft_button.click()
                    draft=Path(downloaded.value.path()).read_text(encoding='utf-8')
                    assert by_target['demo_valid']['request']['revision_sha256'] in draft
                    assert by_target['demo_valid']['request']['attachment_sha256'] in draft
                    assert '原题对应与完整覆盖：未判定' in draft and '官方提交与审核：未进行' in draft
                    assert '原始证明仓库、完整提交 SHA' in draft
                    assert not errors,errors
                    report['checks'].append('private report separates Lean exit from sorryAx, semantics and official submission; author downloads bound local draft')
                finally:browser.close()
            server.stop()
            backup=base/'backup';restored=base/'restored'
            result=cli('backup',backup,'--offline')
            assert result.returncode==0,(result.stdout,result.stderr)
            result=cli('restore',backup,'--into',restored)
            assert result.returncode==0,(result.stdout,result.stderr)
            restored_db=restored/'community.sqlite'
            assert cli('reconcile-accounts','--from-db',db,'--offline',database=restored_db).returncode==0
            with sqlite3.connect(str(restored_db)+'.research.sqlite') as research:
                rows=research.execute('SELECT state FROM research_lean_enrollments ORDER BY rowid').fetchall()
                assert [row[0] for row in rows]==['checked','checked','withdrawn']
                assert research.execute('SELECT COUNT(*) FROM research_lean_runs').fetchone()[0]==2
            report['checks'].append('private queue, withdrawn task and two reports survive independent instance restore')
            report['status']='PASS'
        finally:server.stop()
    Path(args.report).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


if __name__=='__main__':main()
