// Interaction logic only: real research.js with an in-memory DOM/API, no browser.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
class Element{
 constructor(tag=''){this.tag=tag;this.tagName=tag.toUpperCase();this.children=[];this.handlers={};this.dataset={};this.value='';this.files=[];this.hidden=false;this.elements={title:new Field(),body:new Field(),kind:new Field()};this.content={cloneNode:()=>({tag:'fragment'})};}
 append(...x){this.children.push(...x);}replaceChildren(...x){this.children=[...x];}
 addEventListener(k,v){this.handlers[k]=v;}
 querySelector(selector){if(selector==='textarea')return this.descendants().find(x=>x.tag==='textarea');return this.button||(this.button=new Element('button'));}
 querySelectorAll(selector){if(selector.includes('[data-task-id]'))return this.descendants().filter(x=>x.dataset.taskId&&['form','pre'].includes(x.tag));return [];}
 descendants(){return this.children.flatMap(x=>x instanceof Element?[x,...x.descendants()]:[]);}
 reset(){}set textContent(v){this.text=v;this.children=[];}get textContent(){return this.text||'';}
}
class Field{constructor(){this.value='';}}
function setup(){
 const nodes={},events={},calls=[],messages=[],saved=new Map();
 const revision={id:'rev',type:'revision',actor_id:'author',payload:{exploration_id:'work',body:'source'}};
 const win={};
 const ctx={console,Map,URLSearchParams,JSON,Error,String,encodeURIComponent,
  btoa:s=>Buffer.from(s,'binary').toString('base64'),window:win,
  CustomEvent:class{constructor(type,options){this.type=type;this.detail=options?.detail;}},
  document:{getElementById:id=>nodes[id]||(nodes[id]=new Element()),createElement:t=>new Element(t),addEventListener:(t,f)=>events[t]=f,
    dispatchEvent:event=>events[event.type]?.(event)},
  node:(t,v)=>{const x=new Element(t);if(v!==undefined)x.textContent=v;return x;},tell:s=>messages.push(s),
  records:[revision],current:{id:'visitor'},label:x=>x,requireIdentity:()=>{},confirm:()=>true,
  localStorage:{setItem:(k,v)=>saved.set(k,v),getItem:k=>saved.get(k),removeItem:k=>saved.delete(k)},
  FormData:class{constructor(f){return Object.entries(f.data||{});}},download:()=>{},
  api:async(path,data)=>{calls.push({path,data});return {rendered:{renderer:'eksmu.markdown-mathml.v1',html:'<p>rendered</p>',notice:''},materials:{items:[],manifest_sha256:'abc'},tasks:[]};}};
 ctx.tr=(s,p)=>p?s.replace(/\{(\w+)\}/g,(m,k)=>k in p?String(p[k]):m):s;vm.createContext(ctx);vm.runInContext(fs.readFileSync('eksmu/static/research.js','utf8'),ctx);
 return {nodes,events,calls,messages,saved,ctx,revision};
}
async function flush(){await new Promise(r=>setImmediate(r));}
(async()=>{
 let n=0;
 {const f=setup();assert.equal(f.calls.length,0);assert(f.events['eksmu:revision']);n++;}
 {const f=setup();f.events['eksmu:revision']({detail:{revision:f.revision,actor_id:'visitor'}});await flush();assert.equal(f.nodes['attachment-editor'].hidden,true);assert.equal(f.calls[0].path,'/api/research/workspace?revision_id=rev');n++;}
 {const f=setup();f.events['eksmu:revision']({detail:{revision:f.revision,actor_id:'author'}});await flush();assert.equal(f.nodes['attachment-editor'].hidden,false);assert.equal(f.nodes['compare-versions'].disabled,true);n++;}
 {const f=setup();f.nodes.publish.elements.body.value='private draft';f.nodes.publish.handlers.input();assert([...f.saved.keys()][0].endsWith('visitor'));assert.equal(f.calls.length,0);n++;}
 {const f=setup();f.nodes.publish.elements.body.value='draft';f.nodes.publish.handlers.input();f.nodes.publish.elements.body.value='';f.nodes['restore-draft'].handlers.click();assert.equal(f.nodes.publish.elements.body.value,'draft');n++;}
 {const f=setup();f.events['eksmu:published']();assert.equal(f.nodes['publish-preview'].hidden,true);n++;}
 {const f=setup();f.ctx.api=async()=>{throw new Error('offline');};f.events['eksmu:revision']({detail:{revision:f.revision,actor_id:'author'}});await flush();assert(f.nodes['research-files'].textContent.includes('原文和讨论仍保留'));n++;}
 {const f=setup();f.events['eksmu:revision']({detail:{revision:f.revision,actor_id:'author'}});await flush();f.calls.length=0;f.ctx.document.getElementById('material-input').files=[{size:33*1024*1024}];await f.nodes['upload-material'].handlers.submit({preventDefault(){},currentTarget:f.nodes['upload-material']});assert.equal(f.calls.length,0);assert(f.messages.at(-1).includes('32 MiB'));n++;}
 {const f=setup(),task={id:'task',actor_id:'author',category:'reproduce',title:'复核',description:'核对',state:'open',version:1,participants:['visitor'],events:[]};let conflict=true;
  f.ctx.api=async(path,data)=>{f.calls.push({path,data});if(path.endsWith('/task-action')){if(conflict){const e=new Error('任务已有新进展');e.status=409;task.version=2;throw e;}task.version++;return task;}
    return {rendered:{renderer:'eksmu.markdown-mathml.v1',html:'<p>text</p>',notice:''},materials:{items:[],manifest_sha256:'abc'},tasks:[task]};};
  f.events['eksmu:revision']({detail:{revision:f.revision,actor_id:'visitor'}});await flush();
  const forms=()=>f.nodes['research-tasks'].querySelectorAll('form[data-task-id]');forms()[0].querySelector('textarea').value='尚未提交的证据';
  await forms()[0].handlers.submit({preventDefault(){}});assert.equal(forms()[0].querySelector('textarea').value,'尚未提交的证据');assert(f.messages.at(-1).includes('列表已刷新'));
  conflict=false;await forms()[0].handlers.submit({preventDefault(){}});assert.equal(f.calls.filter(x=>x.path.endsWith('/task-action')).at(-1).data.expected_version,2);assert.equal(forms()[0].querySelector('textarea').value,'');n++;}
 {const f=setup(),task={id:'task',actor_id:'author',category:'reproduce',title:'复核',description:'核对',state:'open',version:1,participants:['visitor'],events:[]};
  f.ctx.api=async(path)=>{if(path.endsWith('/task-action')){task.version++;throw new TypeError('Failed to fetch');}return {rendered:{renderer:'eksmu.markdown-mathml.v1',html:'<p>text</p>',notice:''},materials:{items:[],manifest_sha256:'abc'},tasks:[task]};};
  f.events['eksmu:revision']({detail:{revision:f.revision,actor_id:'visitor'}});await flush();const forms=()=>f.nodes['research-tasks'].querySelectorAll('form[data-task-id]');forms()[0].querySelector('textarea').value='未确认进展';await forms()[0].handlers.submit({preventDefault(){}});
  assert.equal(forms()[0].querySelector('textarea').value,'未确认进展');assert(f.messages.at(-1).includes('提交结果未确认'));n++;}
 {const f=setup(),tasks=['one','two'].map(id=>({id,actor_id:'author',category:'reproduce',title:id,description:'核对',state:'open',version:1,participants:['visitor','second'],events:[]}));
  f.ctx.api=async(path)=>{if(path.endsWith('/task-action'))return tasks[0];return {rendered:{renderer:'eksmu.markdown-mathml.v1',html:'<p>text</p>',notice:''},materials:{items:[],manifest_sha256:'abc'},tasks};};
  f.events['eksmu:revision']({detail:{revision:f.revision,actor_id:'visitor'}});await flush();const forms=()=>f.nodes['research-tasks'].querySelectorAll('form[data-task-id]');forms()[0].querySelector('textarea').value='已提交';forms()[1].querySelector('textarea').value='另一任务草稿';
  await forms()[0].handlers.submit({preventDefault(){}});assert.equal(forms()[0].querySelector('textarea').value,'');assert.equal(forms()[1].querySelector('textarea').value,'另一任务草稿');
  f.events['eksmu:revision']({detail:{revision:f.revision,actor_id:'second'}});await flush();assert.equal(forms()[1].querySelector('textarea').value,'');n++;}
 {const f=setup();const tex='\\documentclass{article}\n\\title{素数分布新探}\n\\begin{document}\n\\maketitle\n\\begin{abstract}一段摘要\\end{abstract}\n\\section{引言}\n设 \\textbf{定理} 成立。\n\\end{document}';
  await f.nodes['import-manuscript'].handlers.change({target:{files:[{name:'paper.tex',size:200,text:async()=>tex}],value:'paper.tex'}});
  assert.equal(f.nodes.publish.elements.title.value,'素数分布新探');assert(f.nodes.publish.elements.body.value.includes('## 引言'));assert(f.nodes.publish.elements.body.value.includes('**定理**'));n++;}
 {const f=setup();
  f.ctx.api=async(path,data)=>{
    f.calls.push({path,data});
    if(path==='/api/research/inspect-source') return {main_file:'main.tex',tex:'\\documentclass{article}\n\\title{源码包导入测试}\n\\begin{document}\n\\begin{abstract}压缩包摘要\\end{abstract}\n\\section{第一章}\n展开的正文\\end{document}',files:['main.tex','chap.tex']};
    return {rendered:{renderer:'eksmu.markdown-mathml.v1',html:'<p>text</p>',notice:''},materials:{items:[],manifest_sha256:'abc'},tasks:[]};
  };
  await f.nodes['import-manuscript'].handlers.change({target:{files:[{name:'paper.zip',size:1500,data_b64:'fakeb64'}],value:'paper.zip'}});
  assert.equal(f.nodes.publish.elements.title.value,'源码包导入测试');
  assert(f.nodes.publish.elements.body.value.includes('## 第一章'));
  assert.equal(f.ctx.window.EksmuPublishArchive?.filename,'paper.zip');
  assert.equal(f.nodes['source-archive-info'].hidden,false);
  f.nodes['clear-source-archive'].handlers.click();
  assert.equal(f.ctx.window.EksmuPublishArchive,null);
  assert.equal(f.nodes['source-archive-info'].hidden,true);
  n++;}
 {const f=setup();f.ctx.records=[];let ensured=0;f.ctx.ensureRecords=async()=>{ensured++;f.ctx.records.push(f.revision);};f.events['eksmu:revision']({detail:{revision:f.revision,actor_id:'author'}});await flush();assert.equal(ensured,1);assert.equal(f.nodes['diff-before'].value,'rev');n++;}
 {const f=setup();
  const sizes=[[0,'0 B','0 字节'],[1023,'1023 B','1,023 字节'],[1024,'1.0 KiB','1,024 字节'],[1536,'1.5 KiB','1,536 字节'],[6784541,'6.5 MiB','6,784,541 字节'],[1024**3,'1.0 GiB','1,073,741,824 字节']];
  f.ctx.api=async()=>({rendered:{renderer:'eksmu.markdown-mathml.v1',html:'<p>text</p>',notice:''},materials:{items:sizes.map(([s],idx)=>({id:`m-${idx}`,filename:`f${idx}.pdf`,size:s,sha256:'a'.repeat(64),has_preview:false})),manifest_sha256:'abc'},tasks:[]});
  f.events['eksmu:revision']({detail:{revision:f.revision,actor_id:'author'}});await flush();
  const cards=f.nodes['research-files'].children;assert.equal(cards.length,sizes.length);
  for(let i=0;i<sizes.length;i++){
   const meta=cards[i].children[1],sizeSpan=meta.children[0];
   assert.equal(sizeSpan.textContent,sizes[i][1]);
   assert.equal(sizeSpan.title,sizes[i][2]);
  }
  n++;}
 console.log(JSON.stringify({status:'PASS',checks:n,mode:'isolated research UI logic; not browser HTTP acceptance'}));
})().catch(e=>{console.error(e);process.exit(1);});
