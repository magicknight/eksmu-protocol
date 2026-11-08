from copy import deepcopy
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
import base64
import concurrent.futures
import json
import subprocess
import tempfile
import unittest
import uuid
from unittest.mock import patch

from PIL import Image
from eksmu.store import Store, Problem
from eksmu.research import Research, file_input, sha
from eksmu.research_render import render, sanitize, _render


def encoded(raw=b'example data\n'):
    return base64.b64encode(raw).decode()


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.store=Store(self.root/'community.sqlite');self.addCleanup(self.store.close)
        self.a=self.store.register('新人');self.b=self.store.register('协作者');self.c=self.store.register('其他人')
        self.aid=self.a['actor']['id'];self.bid=self.b['actor']['id'];self.cid=self.c['actor']['id']
        self.rev=self.store.publish(self.a['token'],'# 问题\n\n$x^2$','一个真实问题')['revision']
        self.r=Research(self.root/'research.sqlite',self.store.rows());self.addCleanup(self.r.close)

    def upload(self, filename='data.csv', raw=b'x,y\n1,2\n'):
        return self.r.upload(self.aid,self.rev['id'],filename,encoded(raw))

    def task(self, actor=None):
        return self.r.create_task(actor or self.bid,self.rev['id'],'复现结果','希望一起检查这段计算','reproduce')

    def test_upload_preserves_exact_bytes_and_bound_revision(self):
        m=self.upload();self.assertEqual(m['revision_id'],self.rev['id']);raw,mime,name=self.r.download(m['id'])
        self.assertEqual(raw,b'x,y\n1,2\n');self.assertEqual(mime,'application/octet-stream');self.assertEqual(m['sha256'],sha(raw))

    def test_no_identity_or_foreign_owner_cannot_change_materials(self):
        with self.assertRaises(Problem):self.r.upload(self.bid,self.rev['id'],'a.txt',encoded())
        self.assertEqual(self.r.files(self.rev['id'])['items'],[])

    def test_private_lean_enrollment_binds_author_revision_attachment_and_consent(self):
        before=self.store.export()
        proof=self.upload('Proof.lean',b'theorem demo : True := by trivial\n')
        consent={'local_check':True,'result_visibility':'private','external_transfer':False}
        operation=str(uuid.uuid4())
        args=(self.aid,self.rev['id'],proof['id'],'demo','作者填写的问题来源',operation,consent)
        enrollment=self.r.lean_enroll(*args)
        self.assertEqual(enrollment['state'],'queued')
        self.assertEqual(enrollment['request']['attachment_sha256'],proof['sha256'])
        self.assertEqual(self.r.lean_enroll(*args)['id'],enrollment['id'])
        self.assertEqual(self.r.lean_enroll(*args[:-2],str(uuid.uuid4()),consent)['id'],enrollment['id'])
        with self.assertRaises(Problem):self.r.lean_enroll(*args[:-1],{'local_check':True,'result_visibility':'public','external_transfer':False})
        with self.assertRaises(Problem):self.r.lean_enroll(self.bid,*args[1:])
        with self.assertRaises(Problem):self.r.lean_enrollment(self.bid,enrollment['id'])
        self.assertEqual(self.r.next_lean_job()['source'],b'theorem demo : True := by trivial\n')
        report={'format':'eksmu.lean-local-check.v1','source_sha256':proof['sha256'],'target':'demo',
                'sandbox_setup_completed':True,'status':'lean_process_ok','log':'synthetic report fixture'}
        with self.assertRaises(Problem):self.r.record_lean_run(enrollment['id'],{**report,'target':'other'})
        self.assertEqual(self.r.record_lean_run(enrollment['id'],report)['state'],'checked')
        self.assertIsNone(self.r.next_lean_job())
        self.assertEqual(self.r.lean_enrollment(self.aid,enrollment['id'])['report'],report)
        self.r.lean_withdraw(self.aid,enrollment['id'])
        self.assertEqual(self.r.lean_enrollment(self.aid,enrollment['id'])['state'],'withdrawn')
        self.assertEqual(self.store.export(),before)

    def test_lean_withdrawal_prevents_offline_execution_and_does_not_require_formalization(self):
        proof=self.upload('Proof.lean',b'theorem demo : True := by trivial\n')
        consent={'local_check':True,'result_visibility':'private','external_transfer':False}
        item=self.r.lean_enroll(self.aid,self.rev['id'],proof['id'],'demo','',str(uuid.uuid4()),consent)
        self.r.lean_withdraw(self.aid,item['id'])
        self.assertIsNone(self.r.next_lean_job())
        with self.assertRaises(Problem):self.r.record_lean_run(item['id'],{})
        self.store.publish(self.b['token'],'无需Lean仍可发表')

    def test_same_upload_is_idempotent(self):
        self.assertEqual(self.upload(),self.upload());self.assertEqual(len(self.r.files(self.rev['id'])['items']),1)

    def test_new_content_never_overwrites_old_attachment(self):
        a=self.upload(raw=b'old');b=self.upload(raw=b'new');self.assertNotEqual(a['id'],b['id'])
        self.assertEqual(self.r.download(a['id'])[0],b'old');self.assertEqual(len(self.r.files(self.rev['id'])['items']),2)

    def test_publication_records_unchanged_by_upload_or_tasks(self):
        before=self.store.export();self.upload();t=self.task();self.r.task_action(self.aid,t['id'],'join')
        self.r.task_action(self.aid,t['id'],'progress','发现一个值得再检查的步骤')
        self.assertEqual(self.store.export(),before)

    def test_filename_traversal_and_control_characters_rejected(self):
        for name in ['../a.txt','x\\a.py','/x.csv','a\nb.pdf','a\x00.txt']:
            with self.subTest(name=name),self.assertRaises(Problem):self.upload(name)

    def test_active_formats_not_accepted_as_inline_content(self):
        for name in ['a.svg','a.html','a.exe','a.sh']:
            with self.subTest(name=name),self.assertRaises(Problem):self.upload(name)

    def test_code_is_downloaded_not_executed(self):
        m=self.upload('code.py',b'raise Exception("never execute")')
        self.assertEqual(self.r.download(m['id'])[1],'application/octet-stream')

    def test_invalid_base64_empty_and_non_utf8_rejected(self):
        for value in ['not base64','',encoded(b'\xff'),encoded(b'a\x00b')]:
            with self.subTest(value=value),self.assertRaises(Problem):self.r.upload(self.aid,self.rev['id'],'a.txt',value)

    def test_file_limit_is_enforced(self):
        with patch('eksmu.research.MAX_FILE',3),self.assertRaises(Problem):self.upload(raw=b'1234')

    def test_storage_quota_does_not_stop_new_posts(self):
        with patch('eksmu.research.MAX_STORAGE',2),self.assertRaises(Problem):self.upload()
        self.store.publish(self.a['token'],'继续发表')

    def test_revision_attachment_quota(self):
        self.upload()
        with patch('eksmu.research.MAX_PER_REVISION',1),self.assertRaises(Problem):self.upload('other.csv')

    def test_pdf_kept_as_download_not_script_or_inline(self):
        raw=b'%PDF-1.7\nsoftware-test-fixture-not-a-complete-document'
        m=self.upload('paper.pdf',raw);self.assertEqual(m['mime'],'application/pdf')
        with self.assertRaises(Problem):self.r.download(m['id'],preview=True)
        with self.assertRaises(Problem):self.upload('fake.pdf',b'not a pdf')

    def test_image_preview_reencoded_original_preserved(self):
        image=Image.new('RGB',(12,8));out=BytesIO();image.save(out,format='PNG');raw=out.getvalue()
        m=self.upload('figure.png',raw);self.assertTrue(m['has_preview']);self.assertEqual(self.r.download(m['id'])[0],raw)
        preview,mime,_=self.r.download(m['id'],True);self.assertEqual(mime,'image/png')
        with Image.open(BytesIO(preview)) as img:self.assertEqual(img.size,(12,8));self.assertEqual(img.info,{})

    def test_invalid_or_mislabelled_image_rejected(self):
        with self.assertRaises(Problem):self.upload('bad.png',b'not an image')
        im=Image.new('RGB',(3,3));out=BytesIO();im.save(out,format='JPEG')
        with self.assertRaises(Problem):self.upload('bad.png',out.getvalue())

    def test_images_service_optional(self):
        with patch.dict('sys.modules',{'PIL':None}),self.assertRaises(Problem):self.upload('a.png',b'anything')
        self.store.publish(self.a['token'],'文字仍然可以发表')

    def test_unknown_attachment_rejected(self):
        import uuid
        with self.assertRaises(Problem):self.r.download(str(uuid.uuid4()))

    def test_materials_survive_database_reopen(self):
        m=self.upload();other=Research(self.root/'research.sqlite',self.store.rows())
        try:self.assertEqual(other.files(self.rev['id'])['items'][0],m)
        finally:other.close()

    def test_cannot_use_publication_database_for_research(self):
        with self.assertRaises(Problem):Research(self.root/'community.sqlite',self.store.rows())

    def test_material_manifest_changes_without_changing_source_hash(self):
        prior=self.r.files(self.rev['id'])['manifest_sha256'];self.upload()
        self.assertNotEqual(prior,self.r.files(self.rev['id'])['manifest_sha256'])
        self.assertEqual(self.r.index[self.rev['id']]['sha256'],self.rev['sha256'])

    def test_attachment_bundle_roundtrip_and_idempotence(self):
        item=self.upload();bundle=self.r.export_materials(self.rev['id']);other=Research(self.root/'other.sqlite',self.store.rows())
        try:
            other.import_materials(self.aid,bundle);other.import_materials(self.aid,bundle)
            self.assertEqual(self.r.files(self.rev['id']),other.files(self.rev['id']))
            self.assertEqual(other.download(item['id']),self.r.download(item['id']))
        finally:other.close()
        self.assertNotIn(self.a['token'],json.dumps(bundle))

    def test_tampered_bundle_rejected_without_partial_write(self):
        self.upload();self.upload('other.csv');bundle=self.r.export_materials(self.rev['id']);bundle['items'][1]['sha256']='0'*64
        other=Research(self.root/'other.sqlite',self.store.rows())
        try:
            with self.assertRaises(Problem):other.import_materials(self.aid,bundle)
            self.assertEqual(other.files(self.rev['id'])['items'],[])
        finally:other.close()

    def test_material_import_requires_same_actor_and_source(self):
        self.upload();bundle=self.r.export_materials(self.rev['id'])
        with self.assertRaises(Problem):self.r.import_materials(self.bid,bundle)
        bundle['revision_sha256']='0'*64
        with self.assertRaises(Problem):self.r.import_materials(self.aid,bundle)

    def test_material_import_does_not_silently_change_links(self):
        import uuid
        self.upload();bundle=self.r.export_materials(self.rev['id']);bundle['items'][0]['id']=str(uuid.uuid4())
        with self.assertRaises(Problem):self.r.import_materials(self.aid,bundle)
        self.assertEqual(len(self.r.files(self.rev['id'])['items']),1)

    def test_new_revision_does_not_inherit_materials_or_tasks(self):
        self.upload();self.task();rev2=self.store.revise(self.a['token'],self.rev['payload']['exploration_id'],self.rev['id'],'新版本','新的推导')
        other=Research(self.root/'research.sqlite',self.store.rows())
        try:self.assertEqual(other.files(rev2['id'])['items'],[]);self.assertEqual(other.tasks(rev2['id'])['items'],[])
        finally:other.close()

    def test_diff_same_work_only(self):
        rev2=self.store.revise(self.a['token'],self.rev['payload']['exploration_id'],self.rev['id'],'新标题','# 问题\n\n$x^3$')
        different=self.store.publish(self.a['token'],'不相关')['revision'];other=Research(self.root/'research.sqlite',self.store.rows())
        try:
            result=other.diff(self.rev['id'],rev2['id']);self.assertIn('-$x^2$',result['lines']);self.assertIn('+$x^3$',result['lines'])
            with self.assertRaises(Problem):other.diff(self.rev['id'],different['id'])
        finally:other.close()

    def test_any_participant_can_propose_task_on_anothers_work(self):
        self.assertEqual(self.task()['actor_id'],self.bid)

    def test_two_people_can_join_same_task_without_selection_committee(self):
        task=self.task();self.r.task_action(self.aid,task['id'],'join');result=self.r.task_action(self.cid,task['id'],'join')
        self.assertEqual(set(result['participants']),{self.aid,self.cid})

    def test_join_and_leave_are_idempotent(self):
        task=self.task();a=self.r.task_action(self.aid,task['id'],'join');b=self.r.task_action(self.aid,task['id'],'join');self.assertEqual(a,b)
        a=self.r.task_action(self.aid,task['id'],'leave');self.assertEqual(a,self.r.task_action(self.aid,task['id'],'leave'))

    def test_nonparticipant_cannot_impersonate_progress(self):
        with self.assertRaises(Problem):self.r.task_action(self.aid,self.task()['id'],'progress','我做了')

    def test_progress_is_attributed_and_preserved_after_leaving(self):
        t=self.task();self.r.task_action(self.aid,t['id'],'join');self.r.task_action(self.aid,t['id'],'progress','结果尚不确定')
        result=self.r.task_action(self.aid,t['id'],'leave');event=next(e for e in result['events'] if e['kind']=='progress')
        self.assertEqual(event['actor_id'],self.aid);self.assertEqual(event['body'],'结果尚不确定')

    def test_article_author_cannot_close_others_task(self):
        task=self.task()
        with self.assertRaises(Problem):self.r.task_action(self.aid,task['id'],'close',expected_version=task['version'])

    def test_close_reopen_and_optimistic_version(self):
        t=self.task();self.r.task_action(self.aid,t['id'],'join')
        with self.assertRaises(Problem):self.r.task_action(self.bid,t['id'],'close',expected_version=t['version'])
        t=self.r.task(t['id']);closed=self.r.task_action(self.bid,t['id'],'close',expected_version=t['version']);self.assertEqual(closed['state'],'closed')
        with self.assertRaises(Problem):self.r.task_action(self.cid,t['id'],'join')
        opened=self.r.task_action(self.bid,t['id'],'reopen',expected_version=closed['version']);self.assertEqual(opened['state'],'open')

    def test_task_quota_does_not_limit_publication(self):
        self.task()
        with patch('eksmu.research.MAX_TASKS_PER_WORK',1),self.assertRaises(Problem):self.task()
        self.store.publish(self.a['token'],'继续探索')

    def test_bad_input_cannot_insert_task(self):
        for title,cat in [('', 'reproduce'),('test','rank_people')]:
            with self.subTest(title=title),self.assertRaises(Problem):self.r.create_task(self.aid,self.rev['id'],title,'',cat)

    def test_concurrent_join_preserves_both_participants(self):
        task=self.task()
        def join(actor):
            other=Research(self.root/'research.sqlite',self.store.rows())
            try:return other.task_action(actor,task['id'],'join')
            finally:other.close()
        # Each connection obtains the same validated source snapshot before threads.
        snapshot=self.store.rows()
        def safejoin(actor):
            other=Research(self.root/'research.sqlite',snapshot)
            try:return other.task_action(actor,task['id'],'join')
            finally:other.close()
        with concurrent.futures.ThreadPoolExecutor(2) as pool:list(pool.map(safejoin,[self.aid,self.cid]))
        self.assertEqual(len(self.r.task(task['id'])['participants']),2)


class RendererTests(unittest.TestCase):
    def test_real_markdown_mathml_and_code_render(self):
        result=render('# 问题\n\n$\\frac{1}{2}+x^2$\n\n```python\nprint(1)\n```')
        self.assertEqual(result['mode'],'formatted');self.assertIn('<mfrac>',result['html']);self.assertIn('<h1',result['html']);self.assertIn('<code',result['html'])

    def test_table_and_block_math(self):
        result=render('| A | B |\n|---|---|\n| 1 | 2 |\n\n$$\n\\sum_{i=1}^n i\n$$')
        self.assertIn('<table',result['html']);self.assertIn('display="block"',result['html'])

    def test_html_script_and_event_attributes_never_survive(self):
        out=render('<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>')['html']
        self.assertNotIn('<script',out);self.assertNotIn('<img',out)

    def test_unsafe_links_and_remote_images_are_not_active(self):
        for raw in ['[x](javascript:alert(1))','[x](data:text/html,abc)','![track](https://example.com/t.png)']:
            with self.subTest(raw=raw):
                output=render(raw)['html'];self.assertNotIn('href="javascript:',output);self.assertNotIn('href="data:',output);self.assertNotIn('<img',output)

    def test_local_preview_image_is_allowed(self):
        output=render('![figure](/api/research/preview-file?attachment_id=00000000-0000-4000-8000-000000000001)')['html']
        self.assertIn('<img ',output)

    def test_internal_references_get_separate_ids(self):
        output=sanitize('<h2 id="publish">Methods</h2><a href="#publish">jump</a>')
        self.assertIn('id="eksmu-doc-publish"',output);self.assertIn('href="#eksmu-doc-publish"',output)
        self.assertNotIn('id="publish"',output)

    def test_user_links_get_ugc_nofollow_rel(self):
        output=render('[external](https://example.com/shop)')['html']
        self.assertIn('rel="ugc nofollow noopener noreferrer"',output)
        self.assertIn('target="_blank"',output)

    def test_no_dom_clobbering_attributes_or_styles(self):
        output=sanitize('<form id="publish"><img src="javascript:x" onerror="x"><p style="color:red" name="x">hi</p></form>')
        self.assertNotIn('<form',output);self.assertNotIn(' style=',output);self.assertNotIn('<img',output)

    def test_missing_renderer_falls_back_without_losing_source(self):
        with patch('eksmu.research_render.shutil.which',return_value=None):result=render('原文 <script> $x^2$')
        self.assertEqual(result['mode'],'plain');self.assertIn('原文',result['html']);self.assertNotIn('<script>',result['html'])

    def test_renderer_timeout_and_failure_are_recoverable(self):
        with patch('eksmu.research_render.subprocess.run',side_effect=subprocess.TimeoutExpired('pandoc',4)):
            self.assertEqual(render('timeout fixture')['mode'],'plain')

    def test_sanitizer_removes_math_external_attributes(self):
        output=sanitize('<math href="https://example.com"><mtext onclick="bad()"><a href="java&#x0a;script:bad()">x</a></mtext></math>')
        self.assertNotIn('onclick=',output);self.assertNotIn('href=',output)

    def test_converter_has_fixed_command_and_no_user_filenames(self):
        with patch('eksmu.research_render.subprocess.run') as call:
            call.return_value.returncode=0;call.return_value.stdout='<p>safe</p>';call.return_value.stderr=''
            result=render('$(shell command)')
            args,kw=call.call_args;self.assertIn('--sandbox',args[0]);self.assertNotIn('shell',kw);self.assertEqual(kw['input'],'$(shell command)');self.assertEqual(result['mode'],'formatted')
