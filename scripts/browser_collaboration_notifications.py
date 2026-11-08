"""Two-identity browser drill for collaboration notices on temporary data."""
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
    app_root = Path(args.app_root)
    report = {'status': 'FAIL', 'demo': True, 'actual_user_data_used': False,
              'ssh_tunnel_tested': False, 'checks': [],
              'runtime': 'checkout' if app_root == ROOT else 'installed app in isolated temporary instance',
              'fault_injection': 'One committed task-action and one committed create-task response intentionally aborted; all other HTTP is real.'}
    from playwright.sync_api import sync_playwright, expect
    with tempfile.TemporaryDirectory(prefix='eksmu-collaboration-browser-') as directory:
        root = Path(directory)
        db = root / 'source' / 'community.sqlite'
        db.parent.mkdir()
        source = Server(args.app_python, db, app_root=app_root)
        restored = None
        def cli(*parts):
            return subprocess.run([args.app_python, '-m', 'eksmu', *map(str, parts)],
                                  cwd=app_root, text=True, capture_output=True, timeout=30)
        try:
            source.start()
            with sync_playwright() as pw:
                env = {'PATH': os.environ.get('PATH', os.defpath)}
                if args.library_path:
                    env['LD_LIBRARY_PATH'] = args.library_path
                browser = pw.chromium.launch(executable_path=args.browser, headless=True,
                                             chromium_sandbox=True, env=env)
                try:
                    report['browser_version'] = browser.version
                    a, b = [browser.new_context(accept_downloads=True, locale='zh-CN').new_page() for _ in range(2)]
                    errors = []
                    for page in (a, b):
                        page.on('pageerror', lambda _: errors.append('JavaScript error'))
                    def register(page, alias):
                        page.goto(source.url)
                        page.locator('#register [name=alias]').fill(alias)
                        page.locator('#register button').click()
                        with page.expect_download() as download:
                            page.locator('#backup').click()
                        bundle = json.loads(Path(download.value.path()).read_text())
                        assert bundle['format'] == 'eksmu.recovery.v1'
                        page.locator('#backup-confirmed').check()
                        page.locator('#confirm-account').click()
                        expect(page.locator('#who')).to_contain_text(alias)
                        return bundle['credential']
                    register(a, '协作演示甲')
                    b_credential = register(b, '协作演示乙')
                    a.locator('#publish [name=title]').fill('演示：协作提醒')
                    a.locator('#publish [name=body]').fill('这是合成的浏览器验收探索。')
                    a.get_by_role('button', name='发表', exact=True).click()
                    expect(a.locator('#detail-title')).to_have_text('演示：协作提醒')
                    rid = a.locator('#versions').input_value()
                    work = next(x for x in source.api('/api/state')[1]['records'] if x['id'] == rid)
                    wid = work['payload']['exploration_id']
                    b.goto(source.url + '/#exploration=' + wid + '&revision=' + rid)
                    expect(b.locator('#versions')).to_have_value(rid)
                    b.locator('#collaboration-section > details > summary').click()
                    b.locator('#create-collaboration [name=title]').fill('复算演示数据')
                    b.locator('#create-collaboration [name=description]').fill('请检查示例中的求和')
                    b.locator('#create-collaboration button').click()
                    expect(b.locator('#research-tasks')).to_contain_text('复算演示数据')
                    a.locator('#refresh-community').click()
                    expect(a.locator('#notifications')).to_contain_text('协作演示乙 提出了协作')
                    expect(a.locator('#notice-count')).to_contain_text('1 条未读动态')
                    report['checks'].append('task creation reaches work author but not task creator')
                    a.locator('#research-tasks').get_by_role('button', name='我来参与').click()
                    expect(a.locator('#research-tasks')).to_contain_text('协作演示甲')
                    a.locator('#research-tasks textarea').fill('合成数据的复算结果一致')
                    a.locator('#research-tasks').get_by_role('button', name='提交进展').click()
                    b.locator('#refresh-community').click()
                    expect(b.locator('#notifications')).to_contain_text('协作演示甲 加入了协作')
                    expect(b.locator('#notifications')).to_contain_text('合成数据的复算结果一致')
                    report['checks'].append('join and progress reach task creator in separate browser context')
                    b.locator('#read-notices').click()
                    expect(b.locator('#notice-count')).to_contain_text('暂时没有未读动态')
                    b.locator('#notifications').get_by_role('button', name='复算演示数据').first.click()
                    expect(b.locator('#versions')).to_have_value(rid)
                    report['checks'].append('read state and notice navigation preserve exact revision')
                    b.locator('#research-tasks').get_by_role('button', name='我来参与').click()
                    a.locator('#research-tasks textarea').fill('并发冲突时保留的草稿')
                    a.locator('#research-tasks').get_by_role('button', name='提交进展').click()
                    expect(a.locator('#message')).to_contain_text('列表已刷新')
                    expect(a.locator('#research-tasks textarea')).to_have_value('并发冲突时保留的草稿')
                    a.locator('#research-tasks').get_by_role('button', name='提交进展').click()
                    expect(a.locator('#message')).to_contain_text('记录进展已记录')
                    expect(a.locator('#research-tasks textarea')).to_have_value('')
                    expect(a.locator('#research-tasks .collab-event pre', has_text='并发冲突时保留的草稿')).to_have_count(1)
                    report['checks'].append('stale version conflict refreshes task and retains progress draft for explicit retry')
                    def lose(route):
                        response = route.fetch()
                        assert response.status == 200
                        route.abort('failed')
                    a.route('**/api/research/task-action', lose, times=1)
                    a.locator('#research-tasks textarea').fill('响应丢失后的待确认进展')
                    a.locator('#research-tasks').get_by_role('button', name='提交进展').click()
                    expect(a.locator('#message')).to_contain_text('提交结果未确认')
                    expect(a.locator('#research-tasks textarea')).to_have_value('响应丢失后的待确认进展')
                    expect(a.locator('#research-tasks .collab-event pre', has_text='响应丢失后的待确认进展')).to_have_count(1)
                    report['checks'].append('lost committed response refreshes history, retains draft and does not silently resubmit')
                    b.locator('#refresh-community').click()
                    expect(b.locator('#notice-count')).to_contain_text('未读动态')
                    b.locator('#read-notices').click()
                    expect(b.locator('#notice-count')).to_contain_text('暂时没有未读动态')
                    b.locator('#create-collaboration [name=title]').fill('响应丢失后的协作入口')
                    b.locator('#create-collaboration [name=description]').fill('创建请求已到达服务端')
                    b.route('**/api/research/create-task', lose, times=1)
                    b.locator('#create-collaboration button').click()
                    expect(b.locator('#message')).to_contain_text('提出协作的结果未确认')
                    expect(b.locator('#create-collaboration [name=title]')).to_have_value('响应丢失后的协作入口')
                    expect(b.locator('#research-tasks h4', has_text='响应丢失后的协作入口')).to_have_count(1)
                    report['checks'].append('lost committed task creation keeps input and shows one refreshed task without resubmission')
                    source.stop()
                    backup = root / 'backup'
                    target = root / 'restored'
                    for command in (('--db', db, 'backup', backup, '--offline'),
                                    ('restore', backup, '--into', target),
                                    ('--db', target / 'community.sqlite', 'reconcile-accounts', '--from-db', db, '--offline')):
                        result = cli(*command)
                        assert result.returncode == 0, result.stderr
                    restored = Server(args.app_python, target / 'community.sqlite', app_root=app_root)
                    restored.start()
                    b.goto(restored.url + '/#exploration=' + wid + '&revision=' + rid)
                    b.locator('#restore [name=token]').fill(b_credential)
                    b.locator('#restore button').click()
                    expect(b.locator('#who')).to_contain_text('协作演示乙')
                    expect(b.locator('#notifications')).to_contain_text('协作演示甲 记录了协作进展')
                    expect(b.locator('#notice-count')).to_contain_text('暂时没有未读动态')
                    assert not errors, errors
                    report['checks'].append('independent instance restore retains event notices and read state')
                    report['status'] = 'PASS'
                finally:
                    browser.close()
        finally:
            source.stop()
            if restored:
                restored.stop()
            Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
