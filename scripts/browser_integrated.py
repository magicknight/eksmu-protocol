"""Real Chromium/HTTP journey. An environment block is reported, not bypassed."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import threading

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from eksmu.server import make_server
from eksmu_bridge.service import check_export


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',default='reports/linked-browser.json')
    parser.add_argument('--screenshot')
    args=parser.parse_args()
    report={'status':'FAIL','transport':'real Chromium to loopback HTTP; no request substitution','checks':[]}
    with tempfile.TemporaryDirectory() as directory:
        server=make_server(Path(directory)/'browser.sqlite',0,verification=True)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            from playwright.sync_api import sync_playwright, expect
            with sync_playwright() as playwright:
                browser=playwright.chromium.launch(executable_path='/usr/bin/chromium',headless=True)
                try:
                    page=browser.new_page(viewport={'width':1280,'height':1000},accept_downloads=True,locale='zh-CN')
                    page.goto(f'http://127.0.0.1:{server.server_port}',wait_until='networkidle',timeout=15000)
                    page.locator('#register [name=alias]').fill('首次参与者')
                    page.locator('#register button').click()
                    expect(page.locator('#who')).to_contain_text('首次参与者')
                    page.locator('#publish [name=title]').fill('分数探索·验收测试数据')
                    page.locator('#publish [name=body]').fill('欢迎检查、批评和继续探索。此内容仅用于软件验收。')
                    page.locator('#publish button').click()
                    expect(page.locator('#detail')).to_be_visible()
                    page.locator('.tool-workbench > summary').click()
                    expect(page.locator('#verification-status')).to_contain_text('服务已启用')
                    report['checks'].append('pseudonymous publication without review')
                    page.locator('#prepare-check button').click()
                    cards=page.locator('#verification-tasks .card')
                    expect(cards).to_have_count(1);expect(cards.first).to_contain_text('仅登记')
                    expect(cards.first).not_to_contain_text('本机执行：')
                    report['checks'].append('preparation alone does not execute')
                    cards.first.get_by_role('button',name='明确允许本机计算',exact=True).click()
                    expect(cards.first).to_contain_text('本机执行：等式成立')
                    report['checks'].append('actual finite computation appears separately from participant reports')
                    page.get_by_text('公钥与信任范围',exact=True).click()
                    page.locator('#select-local-key').click()
                    key=page.locator('#expected-key').input_value()
                    assert len(key)==64
                    cards.first.get_by_role('button',name='按所选公钥验签并重算').click()
                    expect(page.locator('#message')).to_contain_text('1 份回执的签名')
                    report['checks'].append('explicit reader key and deterministic replay')
                    with page.expect_download() as download:
                        cards.first.get_by_role('button',name='导出任务、版本与签名回执').click()
                    path=download.value.path()
                    exported=json.loads(Path(path).read_text())
                    assert check_export(exported,key)[0]['replay_matches']
                    report['checks'].append('real browser download independently rechecks')
                    cards.first.get_by_role('button',name='取消后续检查，保留已有记录').click()
                    expect(cards.first).to_contain_text('已取消后续执行')
                    expect(cards.first).to_contain_text('本机执行：等式成立')
                    report['checks'].append('cancellation retains signed evidence')
                    page.locator('#prepare-check [name=c]').fill('2/3')
                    page.locator('#prepare-check button').click();expect(cards).to_have_count(2)
                    cards.last.get_by_role('button',name='明确允许本机计算',exact=True).click()
                    expect(cards.last).to_contain_text('本机执行：等式不成立')
                    page.locator('#publish [name=body]').fill('检查失败后仍然自由发表。')
                    page.locator('#publish button').click()
                    expect(page.locator('#feed .entry')).to_have_count(2)
                    report['checks'].append('failed check does not prevent further publication')
                    page.set_viewport_size({'width':390,'height':844})
                    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                    report['checks'].append('mobile viewport without horizontal overflow')
                    if args.screenshot:
                        page.screenshot(path=args.screenshot,full_page=True)
                    report['status']='PASS'
                finally:
                    browser.close()
        except Exception as exc:
            report['error']=str(exc)
            if 'ERR_BLOCKED_BY_ADMINISTRATOR' in str(exc):report['status']='BLOCKED'
        finally:
            server.shutdown();server.server_close();thread.join()
    dest=Path(args.report);dest.parent.mkdir(parents=True,exist_ok=True)
    dest.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return {'PASS':0,'BLOCKED':2,'FAIL':1}[report['status']]

if __name__=='__main__':sys.exit(main())
