// Isolated UI unit tests with an in-memory DOM/API. Not browser HTTP acceptance.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
class Element {
  constructor(tag='',text=''){this.tag=tag;this.textContent=text;this.children=[];this.handlers={};this.disabled=false;this.value='';}
  append(...items){this.children.push(...items);}replaceChildren(...items){this.children=[...items];}
  addEventListener(name,fn){this.handlers[name]=fn;}
  querySelector(){if(!this.button)this.button=new Element('button');return this.button;}
}
function fixture(){
  const nodes={},events={},calls=[],messages=[];
  const task={id:'00000000-0000-4000-8000-000000000001',actor_id:'author',cancelled_at:null,
    prepared_request:{scope:'只核对输入',input:{left:'fixture',right:'fixture'}}};
  const item={task,state:'prepared',attempts:[]};
  const env={tr:(s,p)=>p?s.replace(/\{(\w+)\}/g,(m,k)=>k in p?String(p[k]):m):s,console,BigInt,Map,JSON,Error,encodeURIComponent,FormData:class{constructor(x){return Object.entries(x.data);}},
    crypto:{randomUUID:()=> '00000000-0000-4000-8000-000000000099'},
    $:id=>nodes[id]||(nodes[id]=new Element()),node:(t,v)=>new Element(t,v),label:x=>x,tell:x=>messages.push(x),
    requireIdentity:()=>{},download:()=>{},document:{addEventListener:(e,f)=>events[e]=f},
    api:async(path,data)=>{calls.push({path,data});if(path.includes('/health'))return {enabled:true,public_key:'ab'.repeat(32)};
      if(path.includes('/tasks?'))return {tasks:[item]};if(path.includes('/recheck'))return {checks:[{}]};return {};}};
  vm.createContext(env);vm.runInContext(fs.readFileSync('eksmu/static/bridge.js','utf8'),env);
  const evaluate=code=>vm.runInContext(code,env);
  evaluate('verificationContext={revision:{id:"version"},actor_id:"author"}');
  return {nodes,calls,messages,item,env,evaluate};
}
function button(card,text){return card.children.find(x=>x.tag==='button'&&x.textContent.includes(text));}
(async()=>{
  let count=0;
  {const t=fixture();assert.equal(JSON.stringify(t.evaluate('rational("02/006")')),JSON.stringify({op:'rational',numerator:'2',denominator:'6'}));assert.throws(()=>t.evaluate('rational("eval(1)")'));count++;}
  {const t=fixture();await t.evaluate('loadVerification()');assert.deepEqual(t.calls.map(c=>c.path),['/api/verification/health','/api/verification/tasks?revision_id=version']);assert.equal(t.nodes['expected-key'],undefined);count++;}
  {const t=fixture();await t.evaluate('loadVerification()');const c=t.nodes['verification-tasks'].children[0];assert(button(c,'明确允许本机计算'));assert(!button(c,'验签并重算'));count++;}
  {const t=fixture();await t.evaluate('loadVerification()');t.nodes['select-local-key'].handlers.click();assert.equal(t.nodes['expected-key'].value,'ab'.repeat(32));count++;}
  {const t=fixture();t.item.attempts=[{receipt:{body:{run_id:'run',result:{status:'failed',reason:'exact_comparison'}}}}];await t.evaluate('loadVerification()');const c=t.nodes['verification-tasks'].children[0];assert(c.children.some(x=>x.textContent?.includes('等式不成立')));assert(button(c,'明确允许重试'));count++;}
  {const t=fixture();t.item.task.cancelled_at='now';t.item.state='cancelled';t.item.attempts=[{receipt:{body:{run_id:'run',result:{status:'passed',reason:'exact_comparison'}}}}];await t.evaluate('loadVerification()');const c=t.nodes['verification-tasks'].children[0];assert(!button(c,'明确允许'));assert(button(c,'导出任务'));count++;}
  {const t=fixture();await t.evaluate('loadVerification()');let failures=1;const real=t.env.api;t.env.api=async(p,d)=>{if(p.endsWith('/execute')){t.calls.push({path:p,data:d});if(failures--)throw new Error('uncertain delivery');return {};}return real(p,d);};const c=t.nodes['verification-tasks'].children[0],b=button(c,'明确允许本机计算');await b.handlers.click();await b.handlers.click();const exec=t.calls.filter(c=>c.path.endsWith('/execute'));assert.equal(exec.length,2);assert.equal(exec[0].data.operation_id,exec[1].data.operation_id);count++;}
  {const t=fixture();t.env.api=async()=>({enabled:false,public_key:null});await t.evaluate('loadVerification()');assert(t.nodes['prepare-check'].querySelector().disabled);assert(t.nodes['verification-status'].textContent.includes('继续发表'));count++;}
  console.log(JSON.stringify({status:'PASS',checks:count,mode:'isolated DOM/API unit tests, not browser HTTP E2E'}));
})().catch(e=>{console.error(e);process.exit(1);});
