import base64
import http.client
from html.parser import HTMLParser
from io import BytesIO
import json
from pathlib import Path
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch

from PIL import Image
from eksmu.server import make_server


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.server=make_server(self.root/'community.sqlite',0)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.a=self.req('/api/actors',{'alias':'问题提出者'})[1]
        self.b=self.req('/api/actors',{'alias':'参与者'})[1]
        self.rev=self.req('/api/explorations',{'body':'# 问题\n\n$\\frac{1}{2}$','title':'公式与代码'},self.a['token'])[1]['revision']

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.tmp.cleanup()

    def req(self,path,data=None,token=None,extra=None):
        client=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=10)
        headers={'Content-Type':'application/json','X-Eksmu-Client':'local-prototype'}
        if token:headers['Authorization']='Bearer '+token
        headers.update(extra or {})
        try:
            client.request('GET' if data is None else 'POST',path,json.dumps(data) if data is not None else None,headers)
            resp=client.getresponse();raw=resp.read();h=dict(resp.getheaders());value=json.loads(raw) if h.get('Content-Type','').startswith('application/json') else raw
            return resp.status,value,h
        finally:client.close()

    def upload(self,name='data.csv',raw=b'x,y\n1,2\n',token=None):
        return self.req('/api/research/upload',{'revision_id':self.rev['id'],'filename':name,'data_b64':base64.b64encode(raw).decode()},token or self.a['token'])

    def task(self):
        return self.req('/api/research/create-task',{'revision_id':self.rev['id'],'title':'一起复现','description':'请检查计算','category':'reproduce'},self.b['token'])[1]

    def test_anonymous_reader_sees_rendered_workspace(self):
        code,body,_=self.req('/api/research/workspace?revision_id='+self.rev['id'])
        self.assertEqual(code,200);self.assertIn('<mfrac>',body['rendered']['html']);self.assertEqual(body['revision_id'],self.rev['id'])

    def test_preview_does_not_require_identity_or_publish(self):
        before=self.req('/api/export')[1]
        code,body,_=self.req('/api/research/preview',{'body':'# 草稿\n\n$x^2$'})
        self.assertEqual(code,200);self.assertEqual(body['mode'],'formatted');self.assertEqual(before,self.req('/api/export')[1])

    def test_private_lean_enrollment_and_withdrawal_http(self):
        proof=self.upload('Proof.lean',b'theorem demo : True := by trivial\n')[1]
        data={'revision_id':self.rev['id'],'attachment_id':proof['id'],'target':'demo',
              'problem_reference':'作者来源说明','operation_id':str(uuid.uuid4()),
              'consent':{'local_check':True,'result_visibility':'private','external_transfer':False}}
        before=self.req('/api/export')[1]
        self.assertEqual(self.req('/api/research/lean-enroll',data,self.b['token'])[0],403)
        self.assertEqual(self.req('/api/research/lean-enroll',{**data,'consent':{**data['consent'],'external_transfer':True}},self.a['token'])[0],400)
        code,item,_=self.req('/api/research/lean-enroll',data,self.a['token'])
        self.assertEqual(code,200)
        self.assertEqual(self.req('/api/research/lean-enroll',data,self.a['token'])[1]['id'],item['id'])
        self.assertEqual(self.req('/api/research/lean-enrollments',token=self.a['token'])[1]['items'][0]['id'],item['id'])
        self.assertEqual(self.req('/api/research/lean-enrollments',token=self.b['token'])[1]['items'],[])
        self.assertEqual(self.req('/api/research/lean-enrollments')[0],401)
        self.assertEqual(self.req('/api/research/lean-withdraw',{'enrollment_id':item['id']},self.b['token'])[0],403)
        self.assertEqual(self.req('/api/research/lean-withdraw',{'enrollment_id':item['id']},self.a['token'])[1]['state'],'withdrawn')
        self.assertEqual(before,self.req('/api/export')[1])

    def test_full_two_person_research_workflow(self):
        before=self.req('/api/export')[1];attachment=self.upload()[1];t=self.task()
        code,joined,_=self.req('/api/research/task-action',{'task_id':t['id'],'action':'join','body':'','expected_version':t['version']},self.a['token']);self.assertEqual(code,200)
        code,progress,_=self.req('/api/research/task-action',{'task_id':t['id'],'action':'progress','body':'我重现了结果，并发现一种不同解释','expected_version':joined['version']},self.a['token']);self.assertEqual(code,200)
        self.assertEqual(self.req('/api/research/file?attachment_id='+attachment['id'])[1],b'x,y\n1,2\n')
        self.assertEqual(before,self.req('/api/export')[1])
        new=self.req('/api/revisions',{'exploration_id':self.rev['payload']['exploration_id'],'parent_revision_id':self.rev['id'],'title':'根据协作修订','body':'# 新版本\n\n$\\frac{2}{4}$'},self.a['token'])[1]
        diff=self.req('/api/research/diff?before_id='+self.rev['id']+'&after_id='+new['id'])[1];self.assertTrue(diff['lines'])
        old=self.req('/api/research/workspace?revision_id='+self.rev['id'])[1];self.assertEqual(len(old['materials']['items']),1);self.assertEqual(old['tasks'][0]['events'][-1]['kind'],'progress')
        latest=self.req('/api/research/workspace?revision_id='+new['id'])[1];self.assertEqual(latest['materials']['items'],[])
        self.assertEqual(self.req('/api/comments',{'revision_id':self.rev['id'],'body':'继续讨论'},self.b['token'])[0],201)

    def test_upload_and_task_writes_require_credentials(self):
        code,_,_=self.req('/api/research/upload',{'revision_id':self.rev['id'],'filename':'a.txt','data_b64':'YQ=='})
        self.assertEqual(code,401)

    def test_other_actor_cannot_upload_authors_material(self):
        self.assertEqual(self.upload(token=self.b['token'])[0],403)

    def test_download_is_attachment_with_unicode_filename(self):
        item=self.upload('实验记录.csv')[1]
        code,raw,headers=self.req('/api/research/file?attachment_id='+item['id'])
        self.assertEqual(code,200);self.assertIn('attachment;',headers['Content-Disposition']);self.assertIn("filename*=UTF-8''",headers['Content-Disposition']);self.assertEqual(headers['X-Content-Type-Options'],'nosniff');self.assertEqual(headers['Content-Type'],'application/octet-stream')

    def test_image_preview_uses_regenerated_png(self):
        image=Image.new('RGB',(10,10));out=BytesIO();image.save(out,format='JPEG')
        item=self.upload('image.jpg',out.getvalue())[1]
        code,raw,headers=self.req('/api/research/preview-file?attachment_id='+item['id'])
        self.assertEqual(code,200);self.assertTrue(raw.startswith(b'\x89PNG'));self.assertEqual(headers['Content-Type'],'image/png')
        self.assertNotIn('Content-Disposition',headers)

    def test_bad_query_duplicate_fields_and_actor_injection(self):
        for path in ['/api/research/workspace','/api/research/workspace?revision_id=x&revision_id=y']:
            self.assertEqual(self.req(path)[0],400)
        self.assertEqual(self.req('/api/research/preview',{'body':'hello','trusted':True})[0],400)
        self.assertEqual(self.req('/api/research/create-task',{'actor_id':self.a['actor']['id']},self.b['token'])[0],400)

    def test_cross_origin_post_is_still_refused(self):
        self.assertEqual(self.req('/api/research/preview',{'body':'hi'},extra={'Origin':'https://other.example'})[0],403)

    def test_research_failure_not_publication_failure(self):
        with patch('eksmu.research.Research.files',side_effect=OSError('fault')):
            self.assertEqual(self.req('/api/research/workspace?revision_id='+self.rev['id'])[0],503)
            self.assertEqual(self.req('/api/explorations',{'body':'服务故障仍然发表'},self.a['token'])[0],201)

    def test_materials_and_tasks_can_be_exported(self):
        self.upload();self.task()
        code,bundle,_=self.req('/api/research/export-materials?revision_id='+self.rev['id']);self.assertEqual(code,200)
        self.assertEqual(self.req('/api/research/import-materials',{'bundle':bundle},self.a['token'])[0],200)
        task_bundle=self.req('/api/research/export-tasks?revision_id='+self.rev['id'])[1];self.assertEqual(len(task_bundle['tasks']),1)
        self.assertNotIn(self.a['token'],json.dumps(task_bundle))

    def test_collaboration_task_close_is_not_article_author_privilege(self):
        t=self.task()
        payload={'task_id':t['id'],'action':'close','body':'','expected_version':t['version']}
        self.assertEqual(self.req('/api/research/task-action',payload,self.a['token'])[0],403)
        self.assertEqual(self.req('/api/research/task-action',payload,self.b['token'])[0],200)
        self.assertEqual(self.req('/api/explorations',{'body':'继续自己的研究'},self.a['token'])[0],201)

    def test_static_markup_has_unique_ids_and_expected_controls(self):
        code,html,headers=self.req('/');self.assertEqual(code,200)
        class IDs(HTMLParser):
            def __init__(self):super().__init__();self.ids=[];self.assets=[]
            def handle_starttag(self,t,a):
                d=dict(a)
                if 'id' in d:self.ids.append(d['id'])
                if t in {'script','link'}:self.assets.append(d.get('src',d.get('href','')))
        parser=IDs();parser.feed(html.decode());self.assertEqual(len(parser.ids),len(set(parser.ids)))
        for control in ['preview-publish','upload-material','create-collaboration','diff-before','restore-draft']:self.assertIn(control,parser.ids)
        self.assertTrue(all(x.startswith('/') for x in parser.assets));self.assertIn("img-src 'self'",headers['Content-Security-Policy'])
        for path in ['/research.js','/research.css']:self.assertEqual(self.req(path)[0],200)
