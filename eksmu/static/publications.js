"use strict";
(() => {
  let context=null, state=null, epoch=0, busy=false;
  const names={note:tr("作者说明"),withdraw:tr("作者撤回"),reinstate:tr("作者恢复")};
  function key(){return context?.actor_id ? `eksmu-publication-pending:${context.actor_id}:${context.revision.payload.exploration_id}` : null;}
  function pending(){const k=key();return k?JSON.parse(sessionStorage.getItem(k)||"null"):null;}
  function showPending(){
    const p=pending();$("publication-pending").hidden=!p;
    $("publication-pending-text").textContent=p?(p.path.endsWith("snapshot")?tr("有一项创建固定版本尚待确认，针对版本 {revision}。重试会使用原请求，不会重复登记。",{revision:p.payload.revision_id}):tr("有一项作者声明尚待确认，针对版本 {revision}。重试会使用原请求，不会重复登记。",{revision:p.payload.revision_id})):"";
  }
  function formatSize(bytes){
    const n=Number(bytes)||0;
    if(n<1024)return `${n} B`;
    if(n<1024*1024)return `${(n/1024).toFixed(1)} KiB`;
    if(n<1024*1024*1024)return `${(n/(1024*1024)).toFixed(1)} MiB`;
    return `${(n/(1024*1024*1024)).toFixed(1)} GiB`;
  }
  function formatSizeTitle(bytes){
    return tr("{size} 字节",{size:(Number(bytes)||0).toLocaleString("en-US")});
  }
  function bibtex(value,name){
    const url=URL.createObjectURL(new Blob([value],{type:"application/x-bibtex;charset=utf-8"}));
    const a=node("a");a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  }
  async function load(){
    if(!context)return;
    const stamp=++epoch,rev=context.revision;state=null;
    const choices=$("snapshot-materials");
    const selectedMaterials=new Set(choices.dataset.revision===rev.id?Array.from(choices.querySelectorAll("input:checked"),x=>x.value):[]);
    $("snapshot-editor").hidden=true;$("publication-author-editor").hidden=true;$("download-citation").disabled=true;
    $("publication-status").textContent=tr("正在核对作者撤回状态…");
    $("publication-load-state").textContent=tr("正在读取引用与发表记录…");
    $("revision-citation").textContent="";$("publication-snapshots").replaceChildren();$("publication-events").replaceChildren();
    try{
      showPending();
      const [result,materials]=await Promise.all([api(`/api/publications/workspace?revision_id=${rev.id}`),api(`/api/research/materials?revision_id=${rev.id}`)]);
      if(stamp!==epoch)return;state=result;
      $("publication-status").textContent=result.status.withdrawn?tr("作者已撤回这项作品（所有版本）。旧版、固定版本及讨论保留，原因见作者声明记录。"):tr("当前没有生效的作者撤回声明。");
      $("publication-load-state").textContent=tr("引用指向当前所选正文版本；固定版本有独立固定链接。");
      $("revision-citation").textContent=result.citation.text;$("download-citation").disabled=false;
      const selected=result.snapshots.filter(s=>s.revision_id===rev.id),list=$("publication-snapshots");
      if(!selected.length)list.append(node("p",tr("本版尚无固定版本。发表与讨论不受影响。"),"muted"));
      for(const snap of selected){const row=node("p"),a=node("a",tr("{time} · {count} 份材料 · 查看固定版本",{time:snap.created_at,count:snap.materials}));a.href=`/?snapshot=${snap.id}`;row.append(a);list.append(row);}
      choices.replaceChildren();choices.dataset.revision=rev.id;
      for(const file of materials.items){const labelNode=node("label"),check=node("input"),sizeSpan=node("span",formatSize(file.size));sizeSpan.title=formatSizeTitle(file.size);check.type="checkbox";check.value=file.id;check.checked=selectedMaterials.has(file.id);labelNode.append(check,node("span",`${file.filename} · `),sizeSpan);choices.append(labelNode);}
      if(!materials.items.length)choices.append(node("p",tr("本版没有附件，将固定正文与论文资料。"),"muted"));
      for(const event of result.status.events){const card=node("article",undefined,"card"),link=node("a",tr("对应版本 {revision}",{revision:event.revision_id}));link.href=`/#exploration=${event.exploration_id}&revision=${event.revision_id}`;card.append(node("h4",names[event.kind]),node("p",event.created_at,"muted"),node("pre",event.body),link);$("publication-events").append(card);}
      if(!result.status.events.length)$("publication-events").append(node("p",tr("还没有作者声明。")));
      const owner=context.actor_id===rev.actor_id;$("snapshot-editor").hidden=!owner;$("publication-author-editor").hidden=!owner;
    }catch(error){if(stamp===epoch){$("publication-status").textContent=tr("暂时无法核对作者撤回状态，请稍后刷新。");$("publication-load-state").textContent=error.message;}}
  }
  async function attempt(p,storageKey){
    requireIdentity();if(p.actor_id!==current.id)throw new Error(tr("登录身份已改变，请刷新后操作。"));
    const result=await api(p.path,p.payload);
    sessionStorage.removeItem(storageKey);
    if(p.path.endsWith("snapshot"))tell(tr("已固定版本：{id}。后添附件需另建固定版本。",{id:result.id}));
    else {$("publication-event").reset();tell(tr("作者声明已记录，原文与讨论保留。"));}
    await load();
  }
  async function start(path,payload){
    requireIdentity();if(!state)throw new Error(tr("发表记录尚未加载，请刷新后再试。"));
    if(pending())throw new Error(tr("已有待确认操作，请先重试或取消本机待确认。"));
    const p={actor_id:current.id,path,payload:{...payload,operation_id:crypto.randomUUID()}};
    const k=key();sessionStorage.setItem(k,JSON.stringify(p));showPending();await attempt(p,k);
  }
  function action(id,fn,type="submit"){
    $(id).addEventListener(type,async event=>{event.preventDefault();if(busy)return;busy=true;
      const button=type==="submit"?event.currentTarget.querySelector("button"):event.currentTarget;button.disabled=true;
      try{await fn();}catch(error){tell(error.message);}finally{busy=false;button.disabled=false;}
    });
  }
  action("snapshot-create",()=>start("/api/publications/snapshot",{revision_id:context.revision.id,
    attachment_ids:Array.from($("snapshot-materials").querySelectorAll("input:checked"),x=>x.value),expected_previous_id:state?.snapshot_head}));
  action("publication-event",()=>start("/api/publications/event",{revision_id:context.revision.id,
    ...Object.fromEntries(new FormData($("publication-event"))),expected_previous_id:state?.status.head}));
  action("publication-retry",async()=>{const p=pending();if(p)await attempt(p,key());},"click");
  $("publication-forget").addEventListener("click",()=>{if(busy)return;const k=key();if(k)sessionStorage.removeItem(k);showPending();load();tell(tr("本机待确认已取消。已经保存的服务器记录仍保留，请查看列表后再提交新操作。"));});
  $("download-citation").addEventListener("click",()=>{if(state)bibtex(state.citation.bibtex,`eksmu-revision-${state.revision_id}.bib`);});
  document.addEventListener("eksmu:revision",event=>{const before=context?.revision.id+":"+context?.actor_id;context=event.detail;if(before!==context.revision.id+":"+context.actor_id)$("publication-event").reset();load();});
  document.addEventListener("eksmu:materials",()=>load());
})();
