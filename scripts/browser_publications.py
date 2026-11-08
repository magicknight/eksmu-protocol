"""Real browser publication drill; temporary synthetic data, no external calls."""
import argparse
import base64
import hashlib
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
    parser.add_argument('--report', required=True)
    args = parser.parse_args()
    report = {'status':'FAIL', 'demo':True, 'identities':2, 'actual_user_data_used':False,
              'ssh_tunnel_tested':False, 'checks':[],
              'fault_injection':'One committed snapshot response intentionally aborted; all other HTTP is real.'}
    from playwright.sync_api import sync_playwright, expect
    with tempfile.TemporaryDirectory(prefix='eksmu-publication-browser-') as directory:
        root = Path(directory); data = root / 'source'; data.mkdir(); database = data / 'community.sqlite'
        server = Server(args.app_python, database); restored = None
        def cli(*parts):
            return subprocess.run([args.app_python, '-m', 'eksmu', *map(str, parts)], cwd=ROOT,
                                  text=True, capture_output=True, timeout=30)
        try:
            server.start()
            with sync_playwright() as pw:
                env = {'PATH':os.environ.get('PATH', os.defpath)}
                if args.library_path: env['LD_LIBRARY_PATH'] = args.library_path
                browser = pw.chromium.launch(executable_path=args.browser, headless=True, chromium_sandbox=True, env=env)
                try:
                    report['browser_version'] = browser.version
                    a, b = [browser.new_context(accept_downloads=True, locale='zh-CN').new_page() for _ in range(2)]
                    errors=[]
                    for page in (a,b): page.on('pageerror', lambda _: errors.append('JavaScript error'))
                    def register(page, alias):
                        page.goto(server.url); page.locator('#register [name=alias]').fill(alias); page.locator('#register button').click()
                        with page.expect_download() as event: page.locator('#backup').click()
                        assert json.loads(Path(event.value.path()).read_text())['format']=='eksmu.recovery.v1'
                        page.locator('#backup-confirmed').check(); page.locator('#confirm-account').click()
                        expect(page.locator('#who')).to_contain_text(alias)
                    register(a,'固定版本演示甲'); register(b,'固定版本演示乙')
                    a.locator('#publish [name=title]').fill('演示：整数求和的固定版本')
                    a.locator('#publish [name=body]').fill('# 软件验收演示\n\n结论：前 n 个整数之和是 n(n+1)/2。\n\n方法：将首尾配对。\n\n局限：这里仅演示发表流程，不宣称原创数学成果。')
                    a.locator('#publish-paper summary').click(); a.locator('#publish-paper-enabled').check()
                    a.locator('#publish-paper-abstract').fill('带材料的合成论文，用于软件验收。')
                    a.get_by_role('button',name='发表',exact=True).click()
                    expect(a.locator('#download-citation')).to_be_enabled()
                    rid=a.locator('#versions').input_value()
                    rev=next(r for r in server.api('/api/state')[1]['records'] if r['id']==rid)
                    wid=rev['payload']['exploration_id']; version_url=server.url+'/#exploration='+wid+'&revision='+rid
                    a.locator('#attachment-editor > summary').click()
                    def upload(name, raw):
                        a.locator('#material-input').set_input_files({'name':name,'mimeType':'text/plain','buffer':raw})
                        a.locator('#upload-material button').click()
                        expect(a.locator('#research-files')).to_contain_text(name)
                        expect(a.locator('#snapshot-materials')).to_contain_text(name)
                    upload('data.csv',b'n,total\n1,1\n2,3\n')
                    upload('source.py',b'# Synthetic demonstration; not executed by publication\nprint(sum(range(1, 4)))\n')
                    a.locator('#snapshot-editor > summary').click()
                    a.locator('#snapshot-materials label',has_text='data.csv').locator('input').check()
                    a.locator('#snapshot-materials label',has_text='source.py').locator('input').uncheck()
                    with a.expect_download() as event: a.locator('#download-citation').click()
                    citation=Path(event.value.path()).read_text(); assert rid in citation and 'snapshot SHA-256' not in citation
                    report['checks'].append('paper, original attachments and exact revision BibTeX created without verification requirements')

                    def lose(route):
                        response=route.fetch(); assert response.status==200; route.abort('failed')
                    a.route('**/api/publications/snapshot',lose,times=1)
                    a.locator('#snapshot-create button').click()
                    expect(a.locator('#publication-pending')).to_be_visible()
                    expect(a.locator('#message')).to_contain_text('Failed to fetch')
                    a.locator('#publication-retry').click()
                    expect(a.locator('#publication-pending')).to_be_hidden()
                    expect(a.locator('#publication-snapshots a')).to_have_count(1)
                    snapshots=server.api('/api/publications/workspace?revision_id='+rid)[1]['snapshots']; assert len(snapshots)==1
                    sid=snapshots[0]['id']; package=server.api('/api/publications/export?snapshot_id='+sid)[1]
                    assert [m['filename'] for m in package['snapshot']['materials']]==['data.csv']
                    report['checks'].append('lost committed snapshot response retries exactly once; only checked attachment is captured')

                    upload('later.txt',b'Added after the first snapshot')
                    expect(a.locator('#snapshot-materials label',has_text='source.py').locator('input')).not_to_be_checked()
                    expect(a.locator('#snapshot-materials label',has_text='later.txt').locator('input')).not_to_be_checked()
                    a.locator('#snapshot-materials label',has_text='later.txt').locator('input').check()
                    assert server.api('/api/publications/export?snapshot_id='+sid)[1]==package
                    a.locator('#snapshot-create button').click(); expect(a.locator('#publication-snapshots a')).to_have_count(2)
                    report['checks'].append('later attachment creates a new snapshot without changing the old package')

                    b.goto(server.url+'/?snapshot='+sid)
                    expect(b.locator('#snapshot-title')).to_have_text('演示：整数求和的固定版本')
                    expect(b.locator('#snapshot-time-author')).to_contain_text('作者未填写既往发表日期')
                    expect(b.locator('#snapshot-time-platform')).to_contain_text('本实例时钟')
                    expect(b.locator('#snapshot-time-public')).to_contain_text('没有可核验的记录')
                    expect(b.locator('#snapshot-time-local')).to_contain_text('本站未保存承诺或随机数')
                    expect(b.locator('#snapshot-time-external')).to_contain_text('未启用')
                    report['checks'].append('reader sees separate author, instance, public-event, local-commitment and disabled external-proof states')
                    expect(b.locator('#snapshot-files')).to_contain_text('data.csv')
                    expect(b.locator('#snapshot-files')).not_to_contain_text('later.txt')
                    expect(b.locator('#snapshot-files')).not_to_contain_text('source.py')
                    with b.expect_download() as event: b.locator('#snapshot-files a').click()
                    assert Path(event.value.path()).read_bytes()==b'n,total\n1,1\n2,3\n'
                    with b.expect_download() as event: b.locator('#snapshot-bibtex').click()
                    assert package['snapshot']['sha256'] in Path(event.value.path()).read_text()
                    with b.expect_download() as event: b.locator('#snapshot-export').click()
                    download=root/'publication.json'; event.value.save_as(download)
                    checked=cli('--db',root/'unused/db.sqlite','verify-publication',download,'--expect-sha256',package['snapshot']['sha256'])
                    assert checked.returncode==0,checked.stderr
                    assert json.loads(checked.stdout)['content_integrity'] and not (root/'unused').exists()
                    evidence=root/'local-time-evidence.json'
                    made=cli('--db',root/'unused/db.sqlite','create-time-evidence',download,evidence)
                    assert made.returncode==0,made.stderr
                    commitment=json.loads(made.stdout)['commitment_sha256']
                    proved=cli('--db',root/'unused/db.sqlite','verify-time-evidence',evidence,'--expect-commitment',commitment)
                    assert proved.returncode==0,proved.stderr
                    result=json.loads(proved.stdout)
                    assert result['status']=='local_only' and result['content_integrity']
                    assert not result['external_time_verified'] and not (root/'unused').exists()
                    bad=json.loads(download.read_text()); bad['blobs'][0]['data_b64']=base64.b64encode(b'tampered').decode()
                    corrupt=root/'corrupt.json'; corrupt.write_text(json.dumps(bad))
                    assert cli('verify-publication',corrupt).returncode!=0
                    report['checks'].append('fixed landing page and downloads use archived bytes; offline CLI accepts original and rejects tampering')
                    report['checks'].append('browser-exported package creates private local evidence and verifies in separate CLI processes without a database or external time claim')

                    b.locator('#snapshot-discuss').click()
                    expect(b.locator('#versions')).to_have_value(rid)
                    expect(b.locator('#publication-author-editor')).to_be_hidden()
                    b.locator('#comment [name=body]').fill('演示回应：这里的材料范围很明确。'); b.locator('#comment button').click()
                    expect(b.locator('#comments')).to_contain_text('材料范围很明确')
                    a.locator('#publication-author-editor > summary').click()
                    a.locator('#publication-event [name=kind]').select_option('withdraw')
                    a.locator('#publication-event [name=body]').fill('演示撤回原因：需要补充解释。'); a.locator('#publication-event button').click()
                    expect(a.locator('#publication-status')).to_contain_text('作者已撤回')
                    b.goto(server.url+'/?snapshot='+sid); expect(b.locator('#snapshot-status')).to_contain_text('演示撤回原因')
                    assert server.api('/api/publications/export?snapshot_id='+sid)[1]==package
                    report['checks'].append('author withdrawal appears on old snapshot while archived content and exact-revision discussion remain')

                    a.locator('#publication-event [name=kind]').select_option('reinstate')
                    a.locator('#publication-event [name=body]').fill('演示恢复原因：已复核材料范围。'); a.locator('#publication-event button').click()
                    expect(a.locator('#publication-status')).to_contain_text('当前没有生效')
                    state=server.api('/api/publications/workspace?revision_id='+rid)[1]
                    assert [e['kind'] for e in state['status']['events']]==['withdraw','reinstate']
                    b.locator('#snapshot-refresh').click(); expect(b.locator('#snapshot-status')).to_contain_text('当前没有生效')
                    b.set_viewport_size({'width':390,'height':844})
                    assert b.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    report['checks'].append('reinstatement preserves both author declarations; fixed snapshot page fits 390px viewport')

                    server.stop(); original=hashlib.sha256(database.read_bytes()).hexdigest()
                    backup=root/'backup'; target=root/'restored'
                    for command in (('--db',database,'backup',backup,'--offline'), ('restore',backup,'--into',target),
                                    ('--db',target/'community.sqlite','reconcile-accounts','--from-db',database,'--offline')):
                        result=cli(*command); assert result.returncode==0,result.stderr
                    assert hashlib.sha256(database.read_bytes()).hexdigest()==original
                    restored=Server(args.app_python,target/'community.sqlite'); restored.start()
                    b.goto(restored.url+'/?snapshot='+sid)
                    expect(b.locator('#snapshot-files')).to_contain_text('data.csv')
                    expect(b.locator('#snapshot-status')).to_contain_text('当前没有生效')
                    assert restored.api('/api/publications/export?snapshot_id='+sid)[1]==package
                    assert cli('verify-time-evidence',evidence,'--expect-commitment',commitment).returncode==0
                    b.locator('#snapshot-discuss').click(); expect(b.locator('#comments')).to_contain_text('材料范围很明确')
                    expect(b.locator('#publication-events')).to_contain_text('演示撤回原因')
                    assert not errors
                    report['checks'].append('independently restored instance resolves original snapshot/revision IDs, identical package and comments; source untouched')
                    report['status']='PASS'
                finally: browser.close()
        finally:
            server.stop()
            if restored: restored.stop()
            Path(args.report).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__': main()
