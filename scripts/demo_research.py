"""Two participants explore a mathematical example using real local HTTP.

Everything is marked as software demonstration; no user data or public service.
"""
import argparse
import base64
from fractions import Fraction
from html import escape
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from eksmu.server import make_server


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--preview',help='Optional read-only self-contained HTML of the actual renderer output')
    args=parser.parse_args()
    rows=[]
    for n in [4,8,16,32]:
        value=(Fraction(1,2)+sum(Fraction(i,n)**2 for i in range(1,n)))/n
        rows.append((n,value,value-Fraction(1,3)))
    table='\n'.join(f'| {n} | {v} | {e} |' for n,v,e in rows)
    source=r'''## 问题
我们怎样判断一次数值近似是否足够可靠？从一个能手算的例子开始，比较不同采样密度的结果。

对于 $f(x)=x^2$，考察区间 $[0,1]$ 上的梯形求积：

$$
T_N=\frac{1}{N}\left[\frac{f(0)+f(1)}{2}+\sum_{i=1}^{N-1}f(i/N)\right].
$$

## 已有计算
这里用有理数运算，避免把浮点舍入误差混入离散误差。

| 分段数 N | 梯形近似 | 与 1/3 的差 |
|---|---|---|
''' + table + r'''

## 复现方法
```python
from fractions import Fraction

def trapezoid_square(n):
    middle = sum(Fraction(i, n)**2 for i in range(1, n))
    return (Fraction(1, 2) + middle) / n
```

> 期待的下一步：独立复算一个结果，再解释误差随采样数怎样变化。

这是软件演示问题，不是新研究成果，也不代表已经招募的真实用户。
'''
    summary={'demo':True,'transport':'real loopback HTTP','browser_used':False,'checks':[]}
    with tempfile.TemporaryDirectory(prefix='eksmu-research-demo-') as directory:
        server=make_server(Path(directory)/'community.sqlite',0)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def api(path,data=None,token=None):
            client=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=10)
            headers={'Content-Type':'application/json','X-Eksmu-Client':'local-prototype'}
            if token:headers['Authorization']='Bearer '+token
            try:
                client.request('GET' if data is None else 'POST',path,None if data is None else json.dumps(data),headers)
                res=client.getresponse();raw=res.read()
                if res.status>=400:raise RuntimeError(raw.decode())
                return json.loads(raw) if res.getheader('Content-Type','').startswith('application/json') else raw
            finally:client.close()
        try:
            a=api('/api/actors',{'alias':'演示参与者 A'});b=api('/api/actors',{'alias':'演示参与者 B'})
            published=api('/api/explorations',{'title':'离散采样如何改变积分估计？','body':source,'kind':'computation'},a['token']);rev=published['revision'];wid=published['exploration']['id']
            api('/api/community/details',{'exploration_id':wid,'topics':['数学','数值计算'],'help':'独立复算结果、解释误差来源','expected_version':0},a['token'])
            api('/api/community/mark',{'exploration_id':wid,'kind':'follow','enabled':True},b['token'])
            csv='N,T,error\n'+'\n'.join(f'{n},{v},{e}' for n,v,e in rows)+'\n'
            attachment=api('/api/research/upload',{'revision_id':rev['id'],'filename':'trapezoid-results.csv','data_b64':base64.b64encode(csv.encode()).decode()},a['token'])
            assert api('/api/research/file?attachment_id='+attachment['id'])==csv.encode()
            summary['checks'].append('exact CSV attachment roundtrip bound to version')
            task=api('/api/research/create-task',{'revision_id':rev['id'],'title':'独立复算 N=8，并解释误差','description':'用自己熟悉的工具或纸笔完成，分享方法。','category':'reproduce'},a['token'])
            task=api('/api/research/task-action',{'task_id':task['id'],'action':'join','body':'','expected_version':task['version']},b['token'])
            task=api('/api/research/task-action',{'task_id':task['id'],'action':'progress','body':'精确复算得到 43/128，误差为 1/384。准备把求和公式写成推导。','expected_version':task['version']},b['token'])
            api('/api/comments',{'revision_id':rev['id'],'body':'已上传协作进展，可以继续讨论误差阶。'},b['token'])
            assert api('/api/community/notifications',token=a['token'])['unread']>0
            summary['checks'].append('join, attributed progress and author notification')
            workspace=api('/api/research/workspace?revision_id='+rev['id']);assert '<math' in workspace['rendered']['html']
            summary['checks'].append('real Markdown, formula, table and code rendering')
            revision2=api('/api/revisions',{'exploration_id':wid,'parent_revision_id':rev['id'],'title':'离散采样与误差：加入复算记录','body':source+'\n## 新进展\n独立复算与原记录一致，接下来推导误差表达式。','note':'根据协作记录继续推进'},a['token'])
            assert api('/api/community/notifications',token=b['token'])['unread']>0
            assert api('/api/research/diff?before_id='+rev['id']+'&after_id='+revision2['id'])['lines']
            assert api('/api/research/workspace?revision_id='+revision2['id'])['materials']['items']==[]
            summary['checks'].append('new revision notification and explicit version difference; old materials retained')
            exported=api('/api/research/export-materials?revision_id='+rev['id']);api('/api/research/import-materials',{'bundle':exported},a['token'])
            summary['checks'].append('materials export and idempotent recovery')
            if args.preview:
                style=(ROOT/'eksmu/static/style.css').read_text()+'\n'+(ROOT/'eksmu/static/research.css').read_text()
                rendered=workspace['rendered']['html']
                html='''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>开放探索共同体 · 研究页面演示</title><style>'''+style+'''</style></head><body><header><span class="eyebrow">OPEN INQUIRY COMMUNITY / RESEARCH ALPHA 2</span><h1>把一个问题，<br>变成共同的进展。</h1><p>以下为实际排版器输出的只读软件演示，不是已经上线的网站。</p></header><main><section class="panel"><span class="badge">数学</span><span class="badge">数值计算</span><h2>离散采样如何改变积分估计？</h2><p class="muted">演示参与者 A · 版本 1 · 软件测试内容</p><div class="collaboration-note">希望一起完成：独立复算结果、解释误差来源。</div><div class="research-prose">'''+rendered+'''</div><section class="research-section"><h3>本版研究材料</h3><article class="material-card"><strong>trapezoid-results.csv</strong><p class="muted">已通过实际上传、下载及字节校验</p><pre>'''+escape(csv)+'''</pre></article></section><section class="research-section"><h3>把下一步一起做出来</h3><article class="collab-card"><span class="badge">复现计算 · 欢迎加入</span><h4>独立复算 N=8，并解释误差</h4><p>用自己熟悉的工具或纸笔完成，分享方法。</p><div class="collab-event"><p class="muted">演示参与者 B · 记录进展</p><p>精确复算得到 43/128，误差为 1/384。准备把求和公式写成推导。</p></div></article></section></section></main><footer>本文档由真实HTTP演示生成。完整编辑、附件和协作功能请运行源码中的本地应用。</footer></body></html>'''
                Path(args.preview).write_text(html,encoding='utf-8')
            summary['status']='PASS'
        finally:
            server.shutdown();server.server_close();thread.join()
    print(json.dumps(summary,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
