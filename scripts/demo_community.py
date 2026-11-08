"""A real two-participant HTTP journey using temporary records, no browser needed."""
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from eksmu.server import make_server


def main():
    checks = []
    with tempfile.TemporaryDirectory(prefix='eksmu-community-demo-') as directory:
        server = make_server(Path(directory) / 'records.sqlite', 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        def api(path, data=None, actor=None):
            client = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=8)
            headers = {'Content-Type': 'application/json', 'X-Eksmu-Client': 'local-prototype'}
            if actor: headers['Authorization'] = 'Bearer ' + actor['token']
            try:
                client.request('GET' if data is None else 'POST', path, json.dumps(data) if data is not None else None, headers)
                response = client.getresponse(); result = json.loads(response.read())
                if response.status >= 400: raise RuntimeError(result)
                return result
            finally: client.close()
        try:
            a = api('/api/actors', {'alias': '演示·提出问题的人'})
            b = api('/api/actors', {'alias': '演示·一起探索的人'})
            work = api('/api/explorations', {'title': '演示：怎样理解这个等式？', 'body': '这里可以记录观察、推导和尚未解决的问题。'}, a)
            wid, rid = work['exploration']['id'], work['revision']['id']
            api('/api/community/details', {'exploration_id': wid, 'topics': ['数学', '直觉'], 'help': '一起讨论另一种解释', 'expected_version': 0}, a)
            found = api('/api/community/feed?' + urlencode({'topic': '数学', 'view': 'collaborate', 'q': '等式'}))
            assert found['items'][0]['id'] == wid; checks.append('发表、主题搜索和寻找协作')
            for kind in ('follow', 'bookmark'):
                api('/api/community/mark', {'exploration_id': wid, 'kind': kind, 'enabled': True}, b)
            assert api('/api/community/feed?view=bookmarks', actor=b)['total'] == 1
            checks.append('个人关注和收藏')
            comment = api('/api/comments', {'revision_id': rid, 'body': '我来尝试另一种解释。'}, b)
            notices = api('/api/community/notifications', actor=a)
            assert notices['items'][0]['id'] == comment['id']; checks.append('作者收到新回应')
            api('/api/community/read', {'record_ids': [comment['id']]}, a)
            assert api('/api/community/notifications', actor=a)['unread'] == 0
            new = api('/api/revisions', {'exploration_id': wid, 'parent_revision_id': rid, 'title': '演示：新的解释', 'body': '根据讨论继续修订。'}, a)
            assert api('/api/community/notifications', actor=b)['items'][0]['revision_id'] == new['id']
            checks.append('关注者收到新版本，旧讨论仍绑定旧版本')
            bundle = api('/api/community/export', actor=b)
            api('/api/community/import', {'bundle': bundle}, b)
            assert api('/api/community/feed?view=bookmarks', actor=b)['total'] == 1
            checks.append('个人清单导出和恢复')
            api('/api/explorations', {'body': '新的问题继续出现。'}, b)
            checks.append('继续自由发表')
        finally:
            server.shutdown(); server.server_close(); thread.join()
    print(json.dumps({'status': 'PASS', 'checks': checks, 'transport': 'real loopback HTTP',
                      'temporary_demo_data': True, 'browser_used': False}, ensure_ascii=False, indent=2))


if __name__ == '__main__': main()
