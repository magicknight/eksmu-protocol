// Isolated DOM/API interaction tests; no browser network acceptance is claimed.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
class Element{
  constructor(tag='',text=''){this.tag=tag;this.textContent=text;this.children=[];this.handlers={};this.dataset={};this.value='';this.hidden=false;this.disabled=false;this.elements={};}
  append(...xs){this.children.push(...xs);}replaceChildren(...xs){this.children=[...xs];}
  addEventListener(name,fn){this.handlers[name]=fn;}scrollIntoView(){}focus(){this.focused=true;}select(){}
  get firstElementChild(){return this.children[0];}get options(){return this.children;}
  querySelector(){if(!this.button)this.button=new Element('button');return this.button;}
  querySelectorAll(){return [];}reset(){}
}
function fixture(){
  const nodes={},events={},calls=[],messages=[],downloads=[],starters=[];
  const get=id=>nodes[id]||(nodes[id]=new Element());
  get('view').value='all';get('feed-order').value='latest';get('topic-filter').value='';
  for(const id of ['community-details','publish'])for(const field of ['topics','help','body','kind'])get(id).elements[field]=new Element('input');
  for(const kind of ['question','finding','failed_attempt']){const b=new Element('button');b.dataset.starter=kind;starters.push(b);}
  const item={id:'work',revision_id:'rev',author_id:'author',author:'探索者',kind:'question',versions:1,title:'数学问题',excerpt:'我的观察',topics:['数学'],help:'想找反例',comments:0,reports:0,following:false,bookmarked:false};
  let responses={feed:{items:[item],total:1,next_offset:null,offset:0},topics:{items:[{name:'数学',explorations:1}]},details:{topics:['数学'],help:'想找反例',version:1,following:false,bookmarked:false},notifications:{items:[],unread:0}};
  const env={tr:(s,p)=>p?s.replace(/\{(\w+)\}/g,(m,k)=>k in p?String(p[k]):m):s,console,URL,URLSearchParams,Map,Set,JSON,Error,encodeURIComponent,
    document:{getElementById:get,addEventListener:(key,fn)=>events[key]=fn,querySelectorAll:()=>starters},
    window:{addEventListener:(key,fn)=>events[key]=fn},location:{hash:'',href:'http://127.0.0.1:8765/'},
    history:{replaceState:(_,__,url)=>calls.push({history:url})},navigator:{clipboard:{writeText:async value=>calls.push({copied:value})}},
    node:(tag,text,css)=>{const e=new Element(tag,text);e.className=css;return e;},tell:s=>messages.push(s),
    download:(data,name)=>downloads.push({data,name}),confirm:()=>true,
    current:{id:'author'},selected:'work',revision:'rev',reply:null,
    records:[{id:'work',type:'exploration',actor_id:'author'},{id:'rev',type:'revision',payload:{exploration_id:'work'}}],
    renderDetail:()=>calls.push({renderDetail:true}),refresh:async()=>calls.push({refresh:true}),
    requireIdentity:()=>{if(!env.current)throw new Error('请先创建身份');},
    FormData:class{constructor(form){return Object.entries(form.data);}},
    api:async(path,data)=>{calls.push({path,data});const name=path.split('?')[0].split('/').at(-1);return responses[name]||{};}
  };
  vm.createContext(env);vm.runInContext(fs.readFileSync('eksmu/static/community.js','utf8'),env);
  return {env,nodes,get,events,calls,messages,downloads,item,responses,starters,
    load:()=>env.window.EksmuCommunity.renderFeed(),detail:()=>events['eksmu:revision']({detail:{revision:env.records[1],actor_id:env.current?.id}})};
}
function strings(e){return [e.textContent,...e.children.flatMap(strings)].filter(Boolean).join(' ');}
async function tick(){for(let i=0;i<10;i++)await Promise.resolve();}
(async()=>{
let n=0;
{const t=fixture();await t.load();assert(strings(t.get('feed')).includes('数学问题'));assert(strings(t.get('feed')).includes('一起推进'));assert(!t.get('page-prev').disabled===false);const authorLink=t.get('feed').children[0].children[0].children[1].children[0];assert.equal(authorLink.tag,'a');assert.equal(authorLink.textContent,'探索者');assert.equal(authorLink.href,'/zh/u/author');n++;}
{const t=fixture();t.item.title='<img src=x onerror=alert(1)>';await t.load();assert(strings(t.get('feed')).includes(t.item.title));assert(!('innerHTML' in t.get('feed')));n++;}
{const t=fixture();t.get('search-query').value='数学 反例';await t.load();const url=new URL(t.calls[0].path,'http://local');assert.equal(url.searchParams.get('q'),'数学 反例');assert(!url.searchParams.has('actor_id'));n++;}
{const t=fixture();let first;const original=t.env.api;t.env.api=(path,data)=>{if(!first)return new Promise(resolve=>{first=resolve;});return original(path,data);};const old=t.load();t.item.title='较新的结果';await t.load();first({items:[{...t.item,title:'过时结果'}],total:1,offset:0,next_offset:null});await old;assert(strings(t.get('feed')).includes('较新的结果'));assert(!strings(t.get('feed')).includes('过时结果'));n++;}
{const t=fixture();await t.detail();await t.get('follow-work').handlers.click();const sent=t.calls.find(c=>c.path==='/api/community/mark');assert.deepEqual(JSON.parse(JSON.stringify(sent.data)),{exploration_id:'work',kind:'follow',enabled:true});assert(t.calls.some(c=>c.refresh));n++;}
{const t=fixture();t.responses.details.bookmarked=true;await t.detail();await t.get('bookmark-work').handlers.click();assert.equal(t.calls.find(c=>c.path==='/api/community/mark').data.enabled,false);n++;}
{const t=fixture();await t.detail();const form=t.get('community-details');form.data={topics:' 数学， AI ',help:'一起计算'};await form.handlers.submit({preventDefault(){},currentTarget:form});const sent=t.calls.find(c=>c.path==='/api/community/details'&&c.data);assert.equal(sent.data.expected_version,1);assert.equal(sent.data.topics.join(','),'数学,AI');n++;}
{const t=fixture();await t.detail();const form=t.get('community-details');form.elements.help.value='未保存的草稿';form.handlers.input();t.responses.details.help='服务器内容';await t.detail();assert.equal(form.elements.help.value,'未保存的草稿');n++;}
{const t=fixture();t.env.current={id:'other'};await t.detail();assert(t.get('community-edit').hidden);n++;}
{const t=fixture();t.env.current=null;t.events['eksmu:state']();await tick();assert(!t.calls.some(c=>c.path==='/api/community/notifications'));assert(t.get('notice-count').textContent.includes('创建账户'));n++;}
{const t=fixture();t.responses.notifications={items:[{id:'notice-1',exploration_id:'work',revision_id:'rev',type:'comment',from:'乙',title:'数学问题',excerpt:'回应',read:false}],unread:1};t.events['eksmu:state']();await tick();assert(t.get('notice-count').textContent.includes('1 条'));await t.get('read-notices').handlers.click();assert.equal(t.calls.find(c=>c.path==='/api/community/read').data.record_ids[0],'notice-1');n++;}
{const t=fixture();t.responses.notifications={items:[{id:'task-event',exploration_id:'work',revision_id:'rev',type:'collaboration',action:'progress',from:'协作者',title:'复核计算',excerpt:'已复算',read:false}],unread:1};t.events['eksmu:state']();await tick();assert(strings(t.get('notifications')).includes('记录了协作进展'));assert(strings(t.get('notifications')).includes('复核计算'));n++;}
{const t=fixture();t.starters[1].handlers.click();assert.equal(t.get('publish').elements.kind.value,'finding');t.get('publish').elements.body.value='自己的草稿';t.starters[0].handlers.click();assert.equal(t.get('publish').elements.body.value,'自己的草稿');n++;}
{const t=fixture();await t.get('copy-work-link').handlers.click();const url=new URL(t.calls.find(c=>c.copied).copied);assert.equal(url.pathname,'/w/work');assert.equal(url.searchParams.get('revision'),'rev');assert.equal(url.hash,'');assert(!url.href.includes('token'));n++;}
{const t=fixture();t.env.window.EksmuCommunity.openWork('work','wrong');assert(t.messages.some(x=>x.includes('版本不属于')));assert(!t.calls.some(c=>c.renderDetail));n++;}
{const t=fixture();t.env.location.hash='#exploration=new-work&revision=new-rev';t.env.refresh=async()=>{t.env.records.push({id:'new-work',type:'exploration'},{id:'new-rev',type:'revision',payload:{exploration_id:'new-work'}});};await t.events.hashchange();assert.equal(t.env.selected,'new-work');assert.equal(t.env.revision,'new-rev');assert(t.calls.some(c=>c.renderDetail));n++;}
{const t=fixture();let done;t.env.refresh=()=>new Promise(resolve=>done=resolve);t.env.location.hash='#exploration=work&revision=rev';const pending=t.events.hashchange();t.env.location.hash='#compose-panel';await t.events.hashchange();done();await pending;assert(!t.calls.some(c=>c.renderDetail));n++;}
{const t=fixture();await t.load();t.env.api=async()=>{throw new Error('offline');};await t.load();assert(strings(t.get('feed')).includes('数学问题'));assert(t.get('feed-summary').textContent.includes('仍可发表'));n++;}
{const t=fixture();const original=t.env.api;t.env.api=async(p,d)=>p.endsWith('/export')?{format:'personal',bookmarks:[]}:original(p,d);await t.get('export-personal').handlers.click();assert.equal(t.downloads[0].name,'eksmu-personal.json');n++;}
{const t=fixture();t.item.bitcoin_height=971056;t.events['eksmu:state']();await tick();await t.load();assert(strings(t.get('feed')).includes('971056'));assert(strings(t.get('topic-pills')).includes('# 数学 (1)'));n++;}
{const t=fixture();t.responses.feed={items:[],total:0,next_offset:null,offset:0};await t.load();assert(strings(t.get('feed')).includes('还没有作品，你的问题可以成为第一篇'));assert(strings(t.get('feed')).includes('发表我的问题'));assert.equal(t.get('feed').children[0].children[1].href,'/publish');n++;}
{const t=fixture();t.responses.feed={items:[],total:0,next_offset:null,offset:0};t.get('search-query').value='不存在';await t.load();assert(t.get('feed-summary').textContent.includes('这里还没有匹配的作品。你的问题可以成为起点。'));n++;}
{
  const nodes={},events={},calls=[],replyBtns=[];
  const get=id=>nodes[id]||(nodes[id]=new Element());
  const box=get('author-actions');box.dataset={author:'author-1',revision:'rev-1'};
  const form=get('work-comment');form.elements.body=new Element('textarea');
  const submitBtn=new Element('button');form.button=submitBtn;
  const guest=get('work-comment-guest'),state=get('work-comment-state');
  const replying=get('work-comment-replying');replying.hidden=true;
  const target=get('work-comment-reply-target'),cancel=get('work-comment-cancel-reply');
  const card=get('c-comment-1');card.after=(...xs)=>calls.push({afterCard:xs.map(x=>x===form?'form':'state')});
  guest.before=(...xs)=>calls.push({beforeGuest:xs.map(x=>x===form?'form':'state')});
  const btn=new Element('button');btn.hidden=true;btn.dataset={commentId:'comment-1',commentAuthor:'乙'};
  replyBtns.push(btn);
  let reloaded=0;
  const wenv={
    window:{EKSMU_LANG:'zh'},
    tr:(s,p)=>p?s.replace(/\{(\w+)\}/g,(m,k)=>k in p?String(p[k]):m):s,
    document:{
      getElementById:get,
      addEventListener:(k,fn)=>events[k]=fn,
      querySelectorAll:sel=>sel==='.comment-reply-btn'?replyBtns:[]
    },
    location:{hash:'',pathname:'/zh/w/work-1',search:'',reload:()=>reloaded++},
    history:{replaceState(){}},
    fetch:async(path,opts)=>{
      const body=opts?.body?JSON.parse(opts.body):undefined;
      calls.push({path,body});
      if(path==='/api/auth/status')return {ok:true,status:200,json:async()=>({actor:{id:'reader-2'}})};
      return {ok:true,status:201,json:async()=>({id:'comment-2'})};
    },
    encodeURIComponent,setInterval:()=>1,clearInterval(){}
  };
  wenv.window.tr=wenv.tr;
  vm.createContext(wenv);
  vm.runInContext(fs.readFileSync('eksmu/static/work-page.js','utf8'),wenv);
  events.DOMContentLoaded();
  await tick();
  assert.equal(form.hidden,false);
  assert.equal(guest.hidden,true);
  assert.equal(btn.hidden,false);
  btn.handlers.click();
  assert.equal(form.dataset.replyTo,'comment-1');
  assert.equal(replying.hidden,false);
  assert.equal(target.textContent,'正在回复 乙');
  assert(calls.some(c=>c.afterCard));
  form.elements.body.value='针对第一条评论的直接回复';
  await form.handlers.submit({preventDefault(){}});
  const post1=calls.find(c=>c.path==='/api/comments');
  assert.deepEqual(post1.body,{revision_id:'rev-1',body:'针对第一条评论的直接回复',reply_to:'comment-1'});
  assert.equal(reloaded,1);
  cancel.handlers.click();
  assert.equal(form.dataset.replyTo,'');
  assert.equal(replying.hidden,true);
  assert(calls.some(c=>c.beforeGuest));
  form.elements.body.value='取消后顶层评论';
  await form.handlers.submit({preventDefault(){}});
  const post2=calls.filter(c=>c.path==='/api/comments')[1];
  assert.deepEqual(post2.body,{revision_id:'rev-1',body:'取消后顶层评论',reply_to:null});
  n++;
}
function appFixture(view='discover',hash=''){
  const nodes={},docEvents={},winEvents={},calls=[];
  const get=id=>{
    if(!nodes[id]){
      const e=new Element('div');
      if(['publish','revise','comment','evidence','behavior-report','community-details','search-form'].includes(id)){
        for(const f of ['title','body','note','kind','reason','topics','help','source','tool','scope','method','result','status','limitations'])e.elements[f]=new Element('input');
      }
      nodes[id]=e;
    }
    return nodes[id];
  };
  get('view').value='all';get('feed-order').value='latest';get('topic-filter').value='';
  get('detail').hidden=true;get('workspace-state').hidden=true;get('workspace-retry').hidden=true;
  const stateRecords=[
    {id:'author-11112222',type:'actor',actor_id:'author-11112222',created_at:'2026-10-01T00:00:00Z',payload:{alias:'旧别名'}},
    {id:'work-1',type:'exploration',actor_id:'author-11112222',created_at:'2026-10-01T00:01:00Z',payload:{kind:'question'}},
    {id:'rev-1',type:'revision',actor_id:'author-11112222',created_at:'2026-10-01T00:02:00Z',sha256:'a'.repeat(64),payload:{exploration_id:'work-1',parent_revision_id:null,title:'按需加载测试作品',body:'第一版正文内容',note:'init'}},
    {id:'c-1',type:'comment',actor_id:'reader-99998888',created_at:'2026-10-01T00:03:00Z',payload:{revision_id:'rev-1',reply_to:null,body:'初始评论'}}
  ];
  let failState=false;
  const body={dataset:{view},classList:{toggle(){}}};
  const env={
    tr:(s,p)=>p?s.replace(/\{(\w+)\}/g,(m,k)=>k in p?String(p[k]):m):s,
    console,URL,URLSearchParams,Map,Set,JSON,Error,String,Number,Boolean,Object,Array,Promise,encodeURIComponent,setTimeout:()=>1,
    CustomEvent:class{constructor(type,opts){this.type=type;this.detail=opts?.detail;}},
    Event:class{constructor(type){this.type=type;}},
    HTMLScriptElement:class{},
    FormData:class{constructor(form){return Object.entries(form.data||{});}},
    location:{hash,pathname:`/zh/${view==='discover'?'':view}`,href:`http://127.0.0.1:8765/zh/${view==='discover'?'':view}${hash}`,assign:url=>calls.push({assign:url})},
    history:{pushState:(_,__,u)=>{env.location.pathname=u.split('#')[0];env.location.hash=u.includes('#')?'#'+u.split('#')[1]:'';},replaceState:(_,__,u)=>{if(u.startsWith('#'))env.location.hash=u;else{env.location.pathname=u.split('#')[0];env.location.hash=u.includes('#')?'#'+u.split('#')[1]:'';}}},
    navigator:{clipboard:{writeText:async()=>{}}},
    document:{
      body,
      getElementById:get,
      createElement:tag=>new Element(tag),
      addEventListener:(k,fn)=>(docEvents[k]||(docEvents[k]=[])).push(fn),
      dispatchEvent:ev=>{for(const fn of docEvents[ev.type]||[])fn(ev);},
      querySelectorAll:()=>[]
    },
    window:{
      EKSMU_LANG:'zh',
      addEventListener:(k,fn)=>(winEvents[k]||(winEvents[k]=[])).push(fn),
      scrollTo(){},
      EksmuPapers:{fill(){},read:()=>null},
      EksmuAccounts:{render:async()=>{}}
    }
  };
  env.window.tr=env.tr;
  vm.createContext(env);
  vm.runInContext(fs.readFileSync('eksmu/static/app.js','utf8'),env);
  env.api=async(path,data)=>{
    calls.push({path,data});
    if(path==='/api/auth/status')return {actor:{id:'author-11112222',payload:{alias:'旧别名'}}};
    if(path==='/api/profiles')return {profiles:{'author-11112222':{display_name:'张探索',affiliation:'独立研究所'}}};
    if(path==='/api/state'){if(failState)throw new Error('网络中断');return {records:stateRecords.slice()};}
    if(path.startsWith('/api/community/feed'))return {items:[{id:'work-1',revision_id:'rev-1',author_id:'author-11112222',author:'张探索',kind:'question',versions:1,title:'按需加载测试作品',excerpt:'摘要',topics:[],help:'',comments:1,reports:0}],total:1,offset:0,next_offset:null};
    if(path==='/api/community/topics')return {items:[]};
    if(path==='/api/community/notifications')return {items:[],unread:0};
    if(path==='/api/community/behavior-cases')return {items:[]};
    if(path.startsWith('/api/community/details'))return {topics:['数论'],help:'',version:1,following:false,bookmarked:false};
    if(path==='/api/explorations')return {exploration:{id:'work-2'},revision:{id:'rev-2'}};
    if(path==='/api/comments'){
      const c={id:'c-new',type:'comment',actor_id:'author-11112222',created_at:'2026-10-01T00:04:00Z',payload:{revision_id:data.revision_id,reply_to:data.reply_to,body:data.body}};
      stateRecords.push(c);return c;
    }
    if(path==='/api/revisions'){
      const r={id:'rev-2',type:'revision',actor_id:'author-11112222',created_at:'2026-10-01T00:05:00Z',sha256:'b'.repeat(64),payload:{exploration_id:data.exploration_id,parent_revision_id:data.parent_revision_id,title:data.title,body:data.body,note:data.note}};
      stateRecords.push(r);return r;
    }
    return {};
  };
  vm.runInContext(fs.readFileSync('eksmu/static/community.js','utf8'),env);
  return {env,get,calls,stateRecords,setFail:v=>{failState=v;}};
}
{
  // Discover, publish, account and activity views do not request /api/state on startup; alias/label fall back cleanly.
  for(const view of ['discover','publish','account','activity']){
    const app=appFixture(view,'');
    await app.env.refresh();
    await tick();
    assert(!app.calls.some(c=>c.path==='/api/state'),`unexpected /api/state on ${view}`);
    assert(app.calls.some(c=>c.path==='/api/community/feed?q=&topic=&view=all&order=latest&offset=0&limit=12'));
  }
  const app=appFixture('discover','');
  await app.env.refresh();
  await tick();
  assert.equal(app.env.alias('author-11112222'),'张探索');
  assert.equal(app.env.label('author-11112222'),'张探索 · 独立研究所（作者自述） · author-1');
  assert.equal(app.env.alias('unknown-abcdef123456'),'unknown-');
  assert.equal(app.env.label('unknown-abcdef123456'),'unknown- · unknown-');
  n++;
}
{
  // Opening a work from Discover or via deep link loads /api/state on demand and renders detail.
  const app=appFixture('discover','');
  await app.env.refresh();
  await tick();
  assert(!app.calls.some(c=>c.path==='/api/state'));
  await app.env.window.EksmuCommunity.openWork('work-1');
  await tick();
  assert.equal(app.calls.filter(c=>c.path==='/api/state').length,1);
  assert.equal(app.get('detail').hidden,false);
  assert.equal(app.get('detail-title').textContent,'按需加载测试作品');
  assert(strings(app.get('comments')).includes('初始评论'));

  const deep=appFixture('workspace','#exploration=work-1&revision=rev-1');
  await deep.env.refresh();
  await tick();
  assert.equal(deep.calls.filter(c=>c.path==='/api/state').length,1);
  assert.equal(deep.get('detail').hidden,false);
  assert.equal(deep.get('workspace-state').hidden,true);
  assert.equal(deep.get('detail-title').textContent,'按需加载测试作品');
  n++;
}
{
  // Failure when loading /api/state shows retryable error state without crashing; retry recovers and renders.
  const app=appFixture('workspace','#exploration=work-1&revision=rev-1');
  app.setFail(true);
  await app.env.refresh();
  await tick();
  assert.equal(app.get('detail').hidden,true);
  assert.equal(app.get('workspace-state').hidden,false);
  assert.equal(app.get('workspace-retry').hidden,false);
  assert(app.get('workspace-state-text').textContent.includes('作品记录加载失败'));
  assert(app.get('message').textContent.includes('作品记录加载失败'));
  app.setFail(false);
  app.get('workspace-retry').handlers.click();
  await tick();
  assert.equal(app.get('workspace-state').hidden,true);
  assert.equal(app.get('detail').hidden,false);
  assert.equal(app.get('detail-title').textContent,'按需加载测试作品');
  n++;
}
{
  // Post-write refresh: publishing outside workspace does not pull /api/state; commenting and revising in workspace reload /api/state and redraw.
  const pub=appFixture('publish','');
  await pub.env.refresh();
  await tick();
  pub.get('publish').data={title:'新问题',body:'正文',kind:'question'};
  await pub.get('publish').handlers.submit({preventDefault(){}});
  await tick();
  assert(pub.calls.some(c=>c.path==='/api/explorations'));
  assert(!pub.calls.some(c=>c.path==='/api/state'));

  const ws=appFixture('workspace','#exploration=work-1&revision=rev-1');
  await ws.env.refresh();
  await tick();
  assert.equal(ws.calls.filter(c=>c.path==='/api/state').length,1);
  ws.get('comment').data={body:'新增的工作区评论'};
  await ws.get('comment').handlers.submit({preventDefault(){}});
  await tick();
  assert.equal(ws.calls.filter(c=>c.path==='/api/state').length,2);
  assert(strings(ws.get('comments')).includes('新增的工作区评论'));

  ws.get('revise').data={title:'按需加载测试作品（第二版）',body:'第二版正文',note:'修订'};
  await ws.get('revise').handlers.submit({preventDefault(){}});
  await tick();
  assert.equal(ws.calls.filter(c=>c.path==='/api/state').length,3);
  assert.equal(ws.get('detail-title').textContent,'按需加载测试作品（第二版）');
  n++;
}
{
  // Accounts UI: display name hint in index.html and client-side rejection of email-like or matching-email display names.
  const indexHtml=fs.readFileSync('eksmu/static/index.html','utf8');
  assert(indexHtml.includes('会永久公开，并写入公开记录，请勿填写邮箱'));
  const nodes={},calls=[],messages=[];
  const get=id=>{
    if(!nodes[id]){
      const e=new Element('form');
      for(const f of ['alias','email','password','code','display_name','affiliation','token','credential','current_password','new_password']){
        e.elements[f]=new Element('input');
      }
      nodes[id]=e;
    }
    return nodes[id];
  };
  get('device-label').value='测试设备';
  const aenv={
    tr:(s,p)=>p?s.replace(/\{(\w+)\}/g,(m,k)=>k in p?String(p[k]):m):s,
    $:get,
    current:{id:'actor-1'},
    uncertainWrite:()=>false,
    requireIdentity:()=>{},
    tell:s=>messages.push(s),
    download:()=>{},
    confirm:()=>true,
    node:(tag,text,css)=>{const e=new Element(tag,text);e.className=css;return e;},
    refresh:async()=>{},
    api:async(path,data)=>{
      calls.push({path,data});
      if(path==='/api/auth/profile'&&!data)return {display_name:'原名',affiliation:'',version:1,email:'owner@example.org',email_verified:true,verification_available:true};
      if(path==='/api/auth/sessions')return {sessions:[]};
      return {actor:{id:'actor-1'}};
    },
    crypto:{randomUUID:()=>'00000000-0000-4000-8000-000000000001',getRandomValues:a=>a},
    btoa:()=>'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=',
    sessionStorage:{getItem:()=>null,setItem:()=>{},removeItem:()=>{}},
    localStorage:{getItem:()=>null,removeItem:()=>{}},
    document:{documentElement:{lang:'zh'},querySelector:()=>new Element('p')},
    window:{},
    FormData:class{constructor(form){return Object.entries(form.data||{});}},
    Error,String,Number,Boolean,Object,Array,JSON,Date
  };
  vm.createContext(aenv);
  vm.runInContext(fs.readFileSync('eksmu/static/accounts.js','utf8'),aenv);
  await aenv.window.EksmuAccounts.render({public_registration:false,recovery_review_required:false,email_recovery_available:true});

  // 1. email-register rejects email-like alias before calling API
  calls.length=0;messages.length=0;
  get('email-register').data={alias:'user@example.org',email:'user@example.org',password:'longpassword123'};
  await get('email-register').handlers.submit({preventDefault(){}});
  assert.equal(calls.length,0);
  assert(messages.some(m=>m.includes('不能使用邮箱地址')));

  // 2. confirm-registration rejects email-like alias before calling API
  calls.length=0;messages.length=0;
  get('confirm-registration').data={alias:'someone@domain.com',email:'user@example.org',code:'eksmu_123',password:'longpassword123'};
  await get('confirm-registration').handlers.submit({preventDefault(){}});
  assert.equal(calls.length,0);
  assert(messages.some(m=>m.includes('不能使用邮箱地址')));

  // 3. profile-form rejects email-like display_name before calling API, allows normal name like "J. Smith"
  calls.length=0;messages.length=0;
  get('profile-form').data={display_name:'owner@example.org',affiliation:''};
  await get('profile-form').handlers.submit({preventDefault(){}});
  assert.equal(calls.length,0);
  assert(messages.some(m=>m.includes('不能使用邮箱地址')));

  calls.length=0;messages.length=0;
  get('profile-form').data={display_name:'J. Smith (张)',affiliation:'Lab'};
  await get('profile-form').handlers.submit({preventDefault(){}});
  assert(calls.some(c=>c.path==='/api/auth/profile'&&c.data?.display_name==='J. Smith (张)'));
  n++;
}
console.log(JSON.stringify({status:'PASS',checks:n,mode:'isolated DOM/API interaction tests, not browser E2E'}));
})().catch(error=>{console.error(error);process.exit(1);});
