"""Routes for an exploration's reading, materials, changes and collaboration."""
from urllib.parse import parse_qs, urlsplit
from .store import Problem, exact
from .research_render import render, render_abstract


def query(raw_path, fields):
    values = parse_qs(urlsplit(raw_path).query, keep_blank_values=True)
    if set(values) != fields or any(len(v) != 1 for v in values.values()):
        raise Problem('查询参数缺失或重复')
    return {k: v[0] for k, v in values.items()}


def get(workspace, raw_path, actor_id=None):
    name = urlsplit(raw_path).path.rsplit('/', 1)[-1]
    if name=='lean-enrollments':
        if urlsplit(raw_path).query:
            raise Problem('这个接口不接受查询参数')
        return workspace.lean_enrollments(actor_id)
    if name=='materials':
        return workspace.files(**query(raw_path, {'revision_id'}))
    if name=='workspace':
        args=query(raw_path, {'revision_id'})
        revision=workspace.get_record(args['revision_id'],'revision')
        paper = revision['payload'].get('paper') or {}
        return {'revision_id':revision['id'], 'revision_sha256':revision['sha256'],
                'rendered':render(revision['payload']['body']),
                'rendered_abstract':render_abstract(paper['abstract']) if paper.get('abstract') else None,
                'materials':workspace.files(revision['id']), 'tasks':workspace.tasks(revision['id'])['items'],
                'compile':workspace.compile_workspace(revision['id'], actor_id)}
    if name=='compile-log':
        return workspace.compile_log(actor_id, **query(raw_path, {'job_id'}))
    if name=='diff':
        return workspace.diff(**query(raw_path,{'before_id','after_id'}))
    if name=='export-materials':
        return workspace.export_materials(**query(raw_path,{'revision_id'}))
    if name=='export-tasks':
        args=query(raw_path,{'revision_id'})
        rev=workspace.get_record(args['revision_id'],'revision')
        return {'format':'eksmu.collaboration-snapshot.v1','revision':rev,'tasks':workspace.tasks(rev['id'])['items']}
    raise Problem('Not found',404)


def post(workspace, path, actor_id, payload):
    name=path.rsplit('/',1)[-1]
    if name=='compile':
        allowed={'revision_id','attachment_id'} | ({'main_file'} if isinstance(payload, dict) and 'main_file' in payload else set())
        exact(payload, allowed)
        return workspace.request_compile(actor_id=actor_id, **payload)
    routes={
        'inspect-source':({'filename','data_b64'},workspace.inspect_source),
        'upload':({'revision_id','filename','data_b64'},workspace.upload),
        'import-materials':({'bundle'},workspace.import_materials),
        'create-task':({'revision_id','title','description','category'},workspace.create_task),
        'task-action':({'task_id','action','body','expected_version'},workspace.task_action),
        'lean-enroll':({'revision_id','attachment_id','target','problem_reference','operation_id','consent'},workspace.lean_enroll),
        'lean-withdraw':({'enrollment_id'},workspace.lean_withdraw),
    }
    if name not in routes: raise Problem('Not found',404)
    fields,action=routes[name];exact(payload,fields)
    return action(actor_id=actor_id,**payload)
