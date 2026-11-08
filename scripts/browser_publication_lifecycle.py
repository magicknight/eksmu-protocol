"""M3 complete-paper acceptance on temporary databases with reusable DEMO files.

Use --app-root to exercise an installed runtime without touching its live data.
Reports contain fixture/runtime hashes, never sessions or recovery credentials.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

from browser_accounts import ROOT, Server

FIXTURE = ROOT / 'examples/publication-demo'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-python', required=True)
    parser.add_argument('--app-root', type=Path, default=ROOT)
    parser.add_argument('--browser', required=True)
    parser.add_argument('--library-path')
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    app_root = args.app_root.resolve()
    scenario = json.loads((FIXTURE / 'scenario.json').read_text())
    manifest = json.loads((FIXTURE / 'manifest.json').read_text())
    for name, expected in manifest['files'].items():
        assert sha(FIXTURE / name) == expected, name
    source = json.loads((ROOT / 'deployment/google-cloud/source-manifest.json').read_text())
    for name, expected in source['files'].items():
        assert sha(app_root / name) == expected, name
    report = {'status': 'FAIL', 'demo': True, 'identities': 2,
              'account_mode': 'private recovery-file compatibility; no public proxy configuration',
              'runtime': 'checkout' if app_root == ROOT else 'installed runtime in isolated temporary instance',
              'runtime_files_matched': len(source['files']),
              'runtime_manifest_sha256': sha(ROOT / 'deployment/google-cloud/source-manifest.json'),
              'fixture_manifest_sha256': sha(FIXTURE / 'manifest.json'),
              'transport': 'real Chromium and loopback HTTP; no mocked responses',
              'actual_user_data_used': False, 'ssh_tunnel_tested': False,
              'external_timestamp_verified': False, 'lean_checked': False, 'checks': []}
    from playwright.sync_api import sync_playwright, expect
    with tempfile.TemporaryDirectory(prefix='eksmu-publication-lifecycle-') as directory:
        work = Path(directory); data = work / 'source'; data.mkdir()
        database = data / 'community.sqlite'
        server = Server(args.app_python, database, app_root); restored = None

        def cli(*parts):
            result = subprocess.run([args.app_python, '-m', 'eksmu', *map(str, parts)], cwd=app_root,
                                    text=True, capture_output=True, timeout=45)
            assert result.returncode == 0, result.stderr
            return result

        def saved_files():
            return {p.name: sha(p) for p in data.iterdir() if p.is_file() and '.sqlite' in p.name}

        try:
            server.start()
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
                        page.on('pageerror', lambda _: errors.append('JavaScript page error'))

                    def view(page, name):
                        # Same in-page view switch the app uses for starter cards and new works.
                        page.evaluate('name => showView(name)', name)

                    def register(page, alias):
                        page.goto(server.url + '/account')
                        page.locator('#legacy-account > summary').click()
                        page.locator('#register [name=alias]').fill(alias)
                        page.locator('#register button').click()
                        with page.expect_download() as event:
                            page.locator('#backup').click()
                        recovery = json.loads(Path(event.value.path()).read_text())
                        assert recovery['format'] == 'eksmu.recovery.v1'
                        page.locator('#backup-confirmed').check()
                        page.locator('#confirm-account').click()
                        expect(page.locator('#who')).to_contain_text(alias)
                        return recovery['credential']

                    author_recovery = register(a, scenario['author'])
                    register(b, scenario['reader'])
                    view(b, 'publish')
                    b.locator('#publish [name=title]').fill('怎样复查素数见证')
                    b.locator('#publish [name=body]').fill('演示普通问题：怎样复查一行素数见证？')
                    b.get_by_role('button', name='发表', exact=True).click()
                    expect(b.locator('#detail-body')).to_contain_text('怎样复查')
                    expect(b.locator('#paper-metadata')).to_be_hidden()
                    report['checks'].append('simple question publishes without metadata, AI or Lean checks')

                    v1, v2 = scenario['versions']
                    def paper(version):
                        folder = FIXTURE / version['directory']
                        return (folder / 'paper.md').read_text(), json.loads((folder / 'metadata.json').read_text())

                    body, metadata = paper(v1)
                    view(a, 'publish')
                    a.locator('#publish [name=kind]').select_option(scenario['kind'])
                    a.locator('#publish [name=title]').fill(v1['title'])
                    a.locator('#publish [name=body]').fill(body)
                    a.locator('#publish-paper summary').click()
                    a.locator('#publish-paper-enabled').check()
                    a.locator('#publish-paper-abstract').fill(metadata['abstract'])
                    for author in metadata['authors']:
                        a.locator('#publish-paper-add-author').click()
                        a.locator('#publish-paper [data-paper-field=name]').last.fill(author['name'])
                        a.locator('#publish-paper [data-paper-field=contribution]').last.fill(author['contribution'])
                    a.locator('#publish-paper-references').fill('\n'.join(metadata['references']))
                    a.locator('#publish-paper-source').fill(metadata['provenance']['source'])
                    a.get_by_role('button', name='发表', exact=True).click()
                    expect(a.locator('#detail-title')).to_have_text(v1['title'])
                    expect(a.locator('#download-citation')).to_be_enabled()
                    rid1 = a.locator('#versions').input_value()
                    records = server.api('/api/state')[1]['records']
                    rev1 = next(r for r in records if r['id'] == rid1)
                    wid = rev1['payload']['exploration_id']
                    assert rev1['payload']['body'] == body and rev1['payload']['paper'] == metadata

                    def open_details(selector):
                        if a.locator(selector).get_attribute('open') is None:
                            a.locator(selector + ' > summary').click()

                    def upload(name, raw, mime='text/plain'):
                        open_details('#attachment-editor')
                        a.locator('#material-input').set_input_files({'name': name, 'mimeType': mime, 'buffer': raw})
                        a.locator('#upload-material button').click()
                        expect(a.locator('#snapshot-materials')).to_contain_text(name)

                    def upload_version(version):
                        for name in version['attachments']:
                            path = FIXTURE / name
                            upload(path.name, path.read_bytes(), 'application/pdf' if path.suffix == '.pdf' else 'text/plain')

                    def snapshot(revision_id, version):
                        open_details('#snapshot-editor')
                        expect(a.locator('#snapshot-materials input')).to_have_count(len(version['attachments']))
                        for checkbox in a.locator('#snapshot-materials input').all():
                            checkbox.check()
                        a.locator('#snapshot-create button').click()
                        expect(a.locator('#publication-snapshots a')).to_have_count(1)
                        workspace = server.api('/api/publications/workspace?revision_id=' + revision_id)[1]
                        selected = [s for s in workspace['snapshots'] if s['revision_id'] == revision_id]
                        assert len(selected) == 1
                        sid = selected[0]['id']
                        package = server.api('/api/publications/export?snapshot_id=' + sid)[1]
                        assert {m['filename'] for m in package['snapshot']['materials']} == {
                            Path(name).name for name in version['attachments']}
                        return sid, package

                    upload_version(v1)
                    with a.expect_download() as event:
                        a.locator('#download-citation').click()
                    bib = Path(event.value.path()).read_text()
                    assert rid1 in bib and 'snapshot SHA-256' not in bib
                    sid1, package1 = snapshot(rid1, v1)
                    report['checks'].append('full v1 paper, metadata, PDF, TeX, CSV, report and both programs publish with exact-version citation and selected snapshot')

                    def read_snapshot(page, instance, sid, package, version):
                        page.goto(instance.url + '/?snapshot=' + sid)
                        expect(page.locator('#snapshot-title')).to_have_text(version['title'])
                        expect(page.locator('#snapshot-files a')).to_have_count(len(version['attachments']))
                        for name in version['attachments']:
                            with page.expect_download() as event:
                                page.locator('#snapshot-files a', has_text=Path(name).name).click()
                            assert Path(event.value.path()).read_bytes() == (FIXTURE / name).read_bytes(), name
                        with page.expect_download() as event:
                            page.locator('#snapshot-bibtex').click()
                        bib = Path(event.value.path()).read_text()
                        assert package['snapshot']['sha256'] in bib and package['snapshot']['revision']['id'] in bib
                        with page.expect_download() as event:
                            page.locator('#snapshot-export').click()
                        download = work / (version['directory'] + '-publication.json')
                        event.value.save_as(download)
                        assert json.loads(download.read_text()) == package
                        checked = json.loads(cli('--db', work / 'unused/db.sqlite', 'verify-publication', download,
                                                 '--expect-sha256', package['snapshot']['sha256']).stdout)
                        assert checked['content_integrity'] and not (work / 'unused').exists()

                    read_snapshot(b, server, sid1, package1, v1)
                    bad = json.loads(json.dumps(package1))
                    bad['blobs'][0]['data_b64'] = base64.b64encode(b'tampered').decode()
                    corrupt = work / 'corrupt.json'; corrupt.write_text(json.dumps(bad))
                    assert subprocess.run([args.app_python, '-m', 'eksmu', 'verify-publication', str(corrupt)],
                                          cwd=app_root, capture_output=True, timeout=15).returncode != 0
                    report['checks'].append('reader downloads eight exact original attachments and snapshot BibTeX; offline package verification accepts original and rejects changed bytes')

                    b.locator('#snapshot-discuss').click()
                    expect(b.locator('#versions')).to_have_value(rid1)
                    expect(b.locator('#edit-box')).to_be_hidden()
                    b.locator('#comment [name=body]').fill(scenario['comment'])
                    b.locator('#comment button').click()
                    expect(b.locator('#comments')).to_contain_text(scenario['comment'])
                    late = scenario['late_material']
                    upload(late['filename'], late['body'].encode())
                    expect(a.locator('#snapshot-materials label', has_text=late['filename']).locator('input')).not_to_be_checked()
                    assert server.api('/api/publications/export?snapshot_id=' + sid1)[1] == package1
                    report['checks'].append('second identity comments on v1; later attachment leaves v1 snapshot bytes and selected scope unchanged')

                    body2, metadata2 = paper(v2)
                    open_details('#edit-box')
                    a.locator('#revise [name=body]').fill(body2)
                    open_details('#revise-paper details')
                    a.locator('#revise-paper-abstract').fill(metadata2['abstract'])
                    a.locator('#revise [name=note]').fill(scenario['revision_note'])
                    a.get_by_role('button', name='保存新版本', exact=True).click()
                    expect(a.locator('#versions option')).to_have_count(2)
                    expect(a.locator('#download-citation')).to_be_enabled()
                    rid2 = a.locator('#versions').input_value()
                    assert rid2 != rid1
                    rev2 = next(r for r in server.api('/api/state')[1]['records'] if r['id'] == rid2)
                    assert rev2['payload']['body'] == body2 and rev2['payload']['paper'] == metadata2
                    assert rev2['payload']['parent_revision_id'] == rid1
                    expect(a.locator('#comments')).not_to_contain_text(scenario['comment'])
                    expect(a.locator('#snapshot-materials input')).to_have_count(0)
                    open_details('#revision-diff-box')
                    a.locator('#compare-versions').click()
                    expect(a.locator('#diff-output')).to_contain_text('4500')
                    upload_version(v2)
                    sid2, package2 = snapshot(rid2, v2)
                    assert package2['snapshot']['previous_snapshot_id'] == sid1
                    assert package2['snapshot']['sha256'] != package1['snapshot']['sha256']
                    open_details('#publication-author-editor')
                    a.locator('#publication-event [name=kind]').select_option('note')
                    a.locator('#publication-event [name=body]').fill(scenario['author_note'])
                    a.locator('#publication-event button').click()
                    expect(a.locator('#publication-events')).to_contain_text(scenario['author_note'])
                    report['checks'].append('author responds with full v2, changed abstract/body and revision note; attachments are separately uploaded and snapshotted, with retained predecessor and author notice')

                    read_snapshot(b, server, sid2, package2, v2)
                    b.set_viewport_size({'width': 390, 'height': 844})
                    assert b.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    a.locator('#versions').select_option(rid1)
                    expect(a.locator('#comments')).to_contain_text(scenario['comment'])
                    expect(a.locator('#paper-metadata')).to_contain_text('499 行')
                    assert next(r for r in server.api('/api/state')[1]['records'] if r['id'] == rid1) == rev1
                    assert server.api('/api/publications/export?snapshot_id=' + sid1)[1] == package1
                    report['checks'].append('both PDFs and all v2 material bytes download correctly; 390px fixed page fits; v1 body, metadata, snapshot and comment remain intact')

                    server.stop()
                    before = saved_files()
                    backup, target = work / 'backup', work / 'restored'
                    cli('--db', database, 'backup', backup, '--offline')
                    cli('verify-backup', backup)
                    cli('restore', backup, '--into', target)
                    cli('--db', target / 'community.sqlite', 'reconcile-accounts', '--from-db', database, '--offline')
                    assert saved_files() == before
                    restored = Server(args.app_python, target / 'community.sqlite', app_root)
                    restored.start()
                    for rid, sid, package, version in ((rid1, sid1, package1, v1), (rid2, sid2, package2, v2)):
                        read_snapshot(b, restored, sid, package, version)
                        b.locator('#snapshot-discuss').click()
                        expect(b.locator('#versions')).to_have_value(rid)
                        expect(b.locator('#publication-events')).to_contain_text(scenario['author_note'])
                        if rid == rid1:
                            expect(b.locator('#comments')).to_contain_text(scenario['comment'])
                        else:
                            expect(b.locator('#comments')).not_to_contain_text(scenario['comment'])
                    report['checks'].append('offline backup verifies and independently restores both fixed IDs, byte-identical material packages, version-bound comments and author notice')

                    a.goto(restored.url + '/account')
                    a.locator('#legacy-account > summary').click()
                    a.locator('#restore [name=token]').fill(author_recovery)
                    a.locator('#restore button').click()
                    expect(a.locator('#who')).to_contain_text(scenario['author'])
                    a.goto(restored.url + '/#exploration=' + wid + '&revision=' + rid2)
                    expect(a.locator('#edit-box')).to_be_visible()
                    a.locator('#comment [name=body]').fill('演示恢复后回访：两个版本与材料仍可阅读。')
                    a.locator('#comment button').click()
                    expect(a.locator('#comments')).to_contain_text('演示恢复后回访')
                    restored.stop(); restored.start()
                    a.reload()
                    expect(a.locator('#comments')).to_contain_text('演示恢复后回访')
                    assert saved_files() == before and not errors
                    report['checks'].append('original author logs into restored instance after credential reconciliation, continues discussion across restart, and leaves every source database byte unchanged')
                    report['status'] = 'PASS'
                finally:
                    browser.close()
        finally:
            server.stop()
            if restored:
                restored.stop()
            args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
