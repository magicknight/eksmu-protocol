"use strict";
(() => {
  const get=id=>document.getElementById(id);
  function node(tag,text){const n=document.createElement(tag);n.textContent=text;return n;}
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
  let citation=null;
  get("snapshot-refresh").addEventListener("click",()=>location.reload());
  get("snapshot-bibtex").addEventListener("click",()=>{
    if(!citation)return;const url=URL.createObjectURL(new Blob([citation.bibtex],{type:"application/x-bibtex;charset=utf-8"}));
    const a=node("a","");a.href=url;a.download=`eksmu-snapshot-${citation.snapshot_id}.bib`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  });
  async function load(){
    const id=new URLSearchParams(location.search).get("snapshot");
    const response=await fetch(`/api/publications/snapshot?${new URLSearchParams({snapshot_id:id})}`,{credentials:"same-origin",headers:{"X-Eksmu-Lang":window.EKSMU_LANG||"zh"}});
    const result=await response.json();if(!response.ok)throw new Error(result.error?tr(result.error):tr("固定版本读取失败"));
    const s=result.snapshot;citation=result.citation;
    const latest=result.status.events.filter(e=>e.kind!=="note").at(-1);
    get("snapshot-status").textContent=result.status.withdrawn?tr("作者已撤回这项作品（所有版本）。原因：{reason}。固定版本和讨论仍保留。",{reason:latest.body}):tr("当前没有生效的作者撤回声明。此状态来自本次加载时的记录。");
    get("snapshot-title").textContent=s.revision.payload.title;
    get("snapshot-identity").textContent=tr("提交账户：{alias} · {id}",{alias:s.publisher.alias,id:s.publisher.id});
    try {
      const profileResponse=await fetch('/api/profiles',{credentials:'same-origin'});
      if(profileResponse.ok){const profile=(await profileResponse.json()).profiles[s.publisher.id];
        if(profile&&(profile.display_name!==s.publisher.alias||profile.affiliation))
          get("snapshot-identity").textContent+=(profile.affiliation?tr(" · 当前公开资料：{name} · {affiliation}（作者自述）",{name:profile.display_name,affiliation:profile.affiliation}):tr(" · 当前公开资料：{name}",{name:profile.display_name}));
      }
    }catch(_){/* Fixed snapshot remains readable if live profiles are unavailable. */}
    get("snapshot-meta").textContent=tr("作品：{work}\n正文版本：{revision}\n固定版本：{id}\n固定版本 SHA-256：{sha}\n固定版本本地记录时间：{time}",{work:s.exploration.id,revision:s.revision.id,id:s.id,sha:s.sha256,time:s.created_at});
    const time=result.time_sources, declared=time.author_declaration;
    get("snapshot-time-author").textContent=declared?.prior_publication_date?tr("作者自述既往发表：{date}；来源：{source}。此日期未经本站独立核验。",{date:declared.prior_publication_date,source:declared.source}):declared?.source?tr("作者填写的既往来源：{source}；未填写既往发表日期。",{source:declared.source}):tr("作者未填写既往发表日期或来源。");
    get("snapshot-time-platform").textContent=tr("本站记录：正文版本 {revision}；固定版本 {snapshot}。均来自本实例时钟。",{revision:time.platform_record.revision_created_at,snapshot:time.platform_record.snapshot_created_at});
    get("snapshot-time-public").textContent=time.public_publication_event.state==="not_recorded_by_instance"?tr("公开发表事件：本站没有可核验的记录；私有固定版本链接不等于已向公众开放。"):tr("公开发表事件状态暂不可显示。");
    get("snapshot-time-local").textContent=time.local_commitment.state==="offline_optional"?tr("本地内容承诺：可在下载完整包后自行离线生成；本站未保存承诺或随机数。"):tr("本地内容承诺状态暂不可显示。");
    get("snapshot-time-external").textContent=time.external_anchor.state==="disabled"?tr("外部时间证明：未启用；没有提交、待确认或已核验的链上证明。"):tr("外部时间证明状态暂不可显示。");
    get("snapshot-body").textContent=s.revision.payload.body;
    const paper=s.revision.payload.paper, target=get("snapshot-paper");
    if(paper){
      target.append(node("h3",tr("本版论文资料（作者声明）")),node("h4",tr("摘要")));
      if(paper.abstract&&result.rendered_abstract?.renderer==="eksmu.markdown-mathml.v1"&&result.rendered_abstract.html){
        const absBox=document.createElement("div");absBox.className="work-abstract";
        const tpl=document.createElement("template");tpl.innerHTML=result.rendered_abstract.html;
        absBox.append(tpl.content.cloneNode(true));
        target.append(absBox);
      }else{
        target.append(node("pre",paper.abstract||tr("未填写")));
      }
      for(const a of paper.authors)target.append(node("p",tr("{name} · {contribution}",{name:a.name,contribution:a.contribution||tr("未填写贡献说明")})));
      target.append(node("h4",tr("许可声明")),node("pre",paper.license||tr("未声明许可")),node("h4",tr("参考文献")));
      const refs=node("ol","");for(const ref of paper.references)refs.append(node("li",ref));target.append(refs);
      if(paper.provenance.source)target.append(node("pre",tr("既往来源：{source}\n既往发表日期（作者自述）：{date}",{source:paper.provenance.source,date:paper.provenance.prior_publication_date||tr("未填写")})));}
    const files=get("snapshot-files");if(!s.materials.length)files.append(node("p",tr("此固定版本没有包含附件。")));
    for(const m of s.materials){const box=node("article",""),a=node("a",m.filename),meta=node("p",""),sizeSpan=node("span",formatSize(m.size));sizeSpan.title=formatSizeTitle(m.size);a.href=`/api/publications/file?${new URLSearchParams({snapshot_id:s.id,attachment_id:m.id})}`;a.rel="ugc nofollow noopener noreferrer";meta.append(sizeSpan,node("span",` · ${tr("SHA-256 {sha}",{sha:m.sha256})}`));box.append(a,meta);files.append(box);}
    get("snapshot-citation").textContent=citation.text;get("snapshot-bibtex").disabled=false;
    const exp=get("snapshot-export");exp.href=`/api/publications/export?snapshot_id=${s.id}`;exp.hidden=false;
    const discuss=get("snapshot-discuss");discuss.href=`/#${new URLSearchParams({exploration:s.exploration.id,revision:s.revision.id})}`;discuss.hidden=false;
  }
  load().catch(error=>{get("snapshot-status").textContent=tr("无法读取固定版本或核对作者状态：{error}",{error:error.message});});
})();
