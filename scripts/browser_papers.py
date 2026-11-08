"""M3.1 browser drill with synthetic papers and real loopback HTTP only."""
import argparse
import json
import os
from pathlib import Path
import tempfile

from browser_accounts import Server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-python', required=True)
    parser.add_argument('--browser', required=True)
    parser.add_argument('--library-path')
    parser.add_argument('--report', required=True)
    args = parser.parse_args()
    report = {'status': 'FAIL', 'demo': True, 'identities': 2,
              'transport': 'real Chromium and real loopback HTTP; no response substitution',
              'actual_user_data_used': False, 'ssh_tunnel_tested': False, 'checks': []}
    from playwright.sync_api import sync_playwright, expect
    with tempfile.TemporaryDirectory(prefix='eksmu-paper-browser-') as directory:
        server = Server(args.app_python, Path(directory) / 'community.sqlite')
        try:
            server.start()
            with sync_playwright() as playwright:
                env = {'PATH': os.environ.get('PATH', os.defpath)}
                if args.library_path: env['LD_LIBRARY_PATH'] = args.library_path
                browser = playwright.chromium.launch(executable_path=args.browser, headless=True,
                    chromium_sandbox=True, env=env)
                try:
                    report['browser_version'] = browser.version
                    a, b = [browser.new_context(accept_downloads=True, locale='zh-CN').new_page() for _ in range(2)]
                    errors = []
                    for page in (a, b):
                        page.on('pageerror', lambda _: errors.append('JavaScript error'))
                        page.on('dialog', lambda dialog: dialog.accept())

                    def register(page, name):
                        page.goto(server.url)
                        page.locator('#register input[name=alias]').fill(name)
                        page.locator('#register button').click()
                        with page.expect_download() as pending: page.locator('#backup').click()
                        assert json.loads(Path(pending.value.path()).read_text())['format'] == 'eksmu.recovery.v1'
                        page.locator('#backup-confirmed').check(); page.locator('#confirm-account').click()
                        expect(page.locator('#who')).to_contain_text(name)

                    register(a, '演示甲'); register(b, '演示乙')
                    a.locator('#publish [name=title]').fill('演示：普通问题')
                    a.locator('#publish [name=body]').fill('只有一个问题也能发表。')
                    a.get_by_role('button', name='发表', exact=True).click()
                    expect(a.locator('#detail-title')).to_have_text('演示：普通问题')
                    expect(a.locator('#paper-metadata')).to_be_hidden()
                    report['checks'].append('simple question publishes without paper fields')

                    a.locator('#publish [name=title]').fill('演示：前 n 个整数的和')
                    a.locator('#publish [name=body]').fill('# 演示研究\n\n配对可得 $1+2+\\cdots+n=n(n+1)/2$。\n\n仅用于软件验收，不宣称新的数学成果。')
                    a.locator('#publish-paper summary').click(); a.locator('#publish-paper-enabled').check()
                    abstract = '演示摘要 <img src=x onerror="window.paperInjected=true">'
                    a.locator('#publish-paper-abstract').fill(abstract)
                    for name, contribution in [('演示甲', '整理配对推导'), ('演示乙', '手工复算示例')]:
                        a.locator('#publish-paper-add-author').click()
                        a.locator('#publish-paper [data-paper-field=name]').last.fill(name)
                        a.locator('#publish-paper [data-paper-field=contribution]').last.fill(contribution)
                    a.locator('#publish-paper-references').fill('演示材料 A（非真实文献）\n演示材料 B（非真实文献）')
                    a.locator('#publish-paper-source').fill('合成旧稿来源，仅用于测试')
                    a.locator('#publish-paper-prior_publication_date').fill('2020-01-01')
                    a.reload(); expect(a.locator('#who')).to_contain_text('演示甲')
                    a.locator('#restore-draft').click(); a.locator('#publish-paper summary').click()
                    expect(a.locator('#publish-paper-enabled')).to_be_checked()
                    expect(a.locator('#publish-paper-abstract')).to_have_value(abstract)
                    expect(a.locator('#publish-paper [data-paper-field=name]')).to_have_count(2)
                    expect(a.locator('#publish-paper-prior_publication_date')).to_have_value('2020-01-01')
                    expect(a.locator('#publish-paper-license')).to_have_value('')
                    report['checks'].append('paper draft survives reload with authors, references, empty license and prior source/date')
                    a.get_by_role('button', name='发表', exact=True).click()
                    expect(a.locator('#detail-title')).to_have_text('演示：前 n 个整数的和')
                    expect(a.locator('#paper-metadata')).to_contain_text(abstract)
                    expect(a.locator('#paper-metadata')).to_contain_text('未声明许可')
                    expect(a.locator('#paper-metadata')).to_contain_text('既往发表日期（作者自述）')
                    assert a.locator('#paper-metadata img').count() == 0
                    assert a.evaluate('window.paperInjected === undefined')
                    report['checks'].append('structured metadata renders as text; no automatic license or verified-date claim')
                    old_id = a.locator('#versions').input_value()
                    _, state = server.api('/api/state')
                    old_rev = next(r for r in state['records'] if r['id'] == old_id)
                    wid = old_rev['payload']['exploration_id']
                    old_url = server.url + '/#exploration=' + wid + '&revision=' + old_id

                    b.goto(old_url); expect(b.locator('#paper-metadata')).to_contain_text('演示乙')
                    expect(b.locator('#edit-box')).to_be_hidden()
                    b.locator('#comment [name=body]').fill('演示评论：请补充 n 为奇数的说明。')
                    b.locator('#comment button').click()
                    expect(b.locator('#comments')).to_contain_text('请补充 n 为奇数的说明')
                    report['checks'].append('named coauthor can discuss the exact revision without gaining edit rights')

                    a.locator('#edit-box > summary').click(); a.locator('#revise-paper summary').click()
                    expect(a.locator('#revise-paper-abstract')).to_have_value(abstract)
                    a.locator('#revise-paper-abstract').fill('修订摘要：补充奇数情形')
                    a.locator('#revise-paper-license').fill('演示许可声明；仅作测试，不代表实际授权')
                    a.locator('#revise [name=note]').fill('演示：仅修改论文资料')
                    a.get_by_role('button', name='保存新版本', exact=True).click()
                    expect(a.locator('#paper-metadata')).to_contain_text('修订摘要')
                    expect(a.locator('#versions option')).to_have_count(2)
                    new_id = a.locator('#versions').input_value()
                    expect(a.locator('#comments')).not_to_contain_text('请补充 n 为奇数的说明')
                    a.locator('#revision-diff-box > summary').click()
                    a.locator('#compare-versions').click()
                    expect(a.locator('#diff-output')).to_contain_text('修订摘要')
                    expect(a.locator('#diff-output')).to_contain_text('演示许可声明')
                    report['checks'].append('metadata-only revision is visible in diff and does not move old comments')

                    a.locator('#versions').select_option(old_id)
                    expect(a.locator('#paper-metadata')).to_contain_text(abstract)
                    expect(a.locator('#paper-metadata')).to_contain_text('未声明许可')
                    expect(a.locator('#comments')).to_contain_text('请补充 n 为奇数的说明')
                    _, updated = server.api('/api/state')
                    assert next(r for r in updated['records'] if r['id'] == old_id) == old_rev
                    assert new_id != old_id
                    report['checks'].append('old version retains its original metadata and immutable record bytes')

                    export_details = a.locator('#export').locator('..')
                    export_details.locator('summary').click()
                    with a.expect_download() as pending: a.locator('#export').click()
                    bundle = json.loads(Path(pending.value.path()).read_text())
                    assert bundle['format'] == 'eksmu.publication-bundle.v1'
                    assert next(r for r in bundle['records'] if r['id'] == old_id) == old_rev
                    report['checks'].append('downloaded publication bundle preserves the selected old version metadata')

                    a.set_viewport_size({'width': 390, 'height': 844})
                    a.locator('#paper-metadata').scroll_into_view_if_needed()
                    assert a.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    expect(a.locator('#paper-metadata')).to_be_visible()
                    report['checks'].append('paper reading fits a 390px viewport without page-level horizontal overflow')
                    server.stop(); server.start()
                    a.goto(old_url); expect(a.locator('#who')).to_contain_text('演示甲')
                    expect(a.locator('#paper-metadata')).to_contain_text(abstract)
                    expect(a.locator('#comments')).to_contain_text('请补充 n 为奇数的说明')
                    assert not errors
                    report['checks'].append('service restart and fixed revision URL preserve metadata, session and comments; no JS errors')
                    report['status'] = 'PASS'
                finally: browser.close()
        finally:
            server.stop()
            Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__': main()
