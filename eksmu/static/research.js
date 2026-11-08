"use strict";
// One research workspace on the existing community. No CDN or third-party calls.
(() => {
  let context=null, epoch=0, previewEpoch=0, loadedContext=null, pendingLeanOperation=null;
  const kinds={reproduce:tr("复现计算"),counterexample:tr("寻找反例"),explain:tr("整理解释"),formalize:tr("形式化证明"),data:tr("数据与资料"),other:tr("其他协作")};
  const actions={created:tr("提出协作"),join:tr("加入协作"),leave:tr("退出协作"),progress:tr("记录进展"),close:tr("结束本项协作"),reopen:tr("重新开放")};
  const get=id=>document.getElementById(id);
  const uncertainWrite=error=>error.writeUncertain===true||(typeof error.status==="number"&&error.status>=500);
  const act=(caption,fn)=>{
    const button=node("button",caption,"secondary"); button.type="button";
    button.addEventListener("click",async()=>{button.disabled=true;try{await fn();}catch(e){tell(e.message);}finally{button.disabled=false;}});
    return button;
  };
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
  function mountHTML(target,response){
    if(response.renderer!=="eksmu.markdown-mathml.v1")throw new Error(tr("未知排版格式"));
    // Only the server's reconstructed reading vocabulary is used here.
    const template=document.createElement("template");template.innerHTML=response.html;
    target.replaceChildren(template.content.cloneNode(true));
  }
  function draftKey(){return `eksmu-research-draft:${current?.id || "guest"}`;}
  function saveDraft(){
    try{
      const form=get("publish");const saved={title:form.elements.title.value,body:form.elements.body.value,kind:form.elements.kind.value};
      if(typeof window!=="undefined"&&window.EksmuPapers)saved.paper=window.EksmuPapers.read("publish");
      localStorage.setItem(draftKey(),JSON.stringify(saved));
      get("draft-state").textContent=tr("草稿保存在此设备");
    }catch{get("draft-state").textContent=tr("此设备无法保存草稿，请自行复制");}
  }
  async function preview(formId,outputId){
    const stamp=++previewEpoch, body=get(formId).elements.body.value;
    const paper=(typeof window!=="undefined"&&window.EksmuPapers)?window.EksmuPapers.read(formId):null;
    const abstract=paper?.abstract||"";
    const target=get(outputId);target.hidden=false;target.textContent=tr("正在排版…");
    try{
      const result=await api("/api/research/preview",abstract?{body,abstract}:{body});
      if(stamp!==previewEpoch)return;
      mountHTML(target,result);
      if(result.rendered_abstract?.renderer==="eksmu.markdown-mathml.v1"&&result.rendered_abstract.html){
        const absBox=node("section",undefined,"work-abstract");
        absBox.append(node("h2",tr("摘要")));
        const tpl=document.createElement("template");tpl.innerHTML=result.rendered_abstract.html;
        absBox.append(tpl.content.cloneNode(true));
        target.prepend(absBox);
      }
      if(result.notice)tell(result.notice);
    }
    catch(e){if(stamp===previewEpoch)target.textContent=body;tell(e.message);}
  }
  function renderTasks(items,{clearTaskDraft=null}={}){
    const target=get("research-tasks");
    const drafts=new Map([...target.querySelectorAll("form[data-task-id], pre[data-task-id]")]
      .filter(item=>item.dataset.taskId!==clearTaskDraft)
      .map(item=>[item.dataset.taskId,item.tagName==="FORM"?item.querySelector("textarea").value:item.textContent]));
    target.replaceChildren();
    if(!items.length){target.append(node("p",tr("把下一步写成一个小任务，邀请别人一起完成。"),"muted"));return;}
    for(const task of items){
      const card=node("article",undefined,"collab-card");
      card.append(node("span",`${kinds[task.category]} · ${task.state==="open"?tr("欢迎加入"):tr("已结束")}`,"badge"),node("h4",task.title),node("p",task.description));
      card.append(node("p",tr("发起者 {who} · {n} 人正在参与",{who:label(task.actor_id),n:task.participants.length}),"muted"));
      if(task.participants.length)card.append(node("p",task.participants.map(label).join(tr("、")),"participants"));
      const buttons=node("div",undefined,"work-actions");
      const joined=context.actor_id&&task.participants.includes(context.actor_id);
      const operation=async(action,body="")=>{
        requireIdentity();
        try{await api("/api/research/task-action",{task_id:task.id,action,body,expected_version:task.version});}
        catch(error){await recoverTaskFailure(error,action==="progress");return;}
        const refreshed=await load({clearTaskDraft:action==="progress"?task.id:null});
        tell(refreshed?tr("{action}已记录。",{action:actions[action]}):tr("{action}已写入，但列表暂未刷新；请稍后刷新查看。",{action:actions[action]}));
      };
      if(task.state==="open"&&!joined)buttons.append(act(tr("我来参与"),()=>operation("join")));
      if(joined)buttons.append(act(tr("退出这项协作"),()=>operation("leave")));
      if(context.actor_id===task.actor_id)buttons.append(act(task.state==="open"?tr("结束这项任务"):tr("重新开放"),()=>operation(task.state==="open"?"close":"reopen")));
      card.append(buttons);
      if(joined&&task.state==="open"){
        const form=node("form"),labelNode=node("label",tr("我的新进展")),area=node("textarea");
        form.dataset.taskId=task.id;
        area.maxLength=4000;area.required=true;area.rows=3;area.placeholder=tr("写下复现结果、反例、链接，或这次尝试留下的问题。");labelNode.append(area);
        if(drafts.has(task.id))area.value=drafts.get(task.id);
        const submit=node("button",tr("提交进展"));submit.type="submit";form.append(labelNode,submit);
        form.addEventListener("submit",async e=>{e.preventDefault();submit.disabled=true;try{await operation("progress",area.value);}catch(error){tell(error.message);}finally{submit.disabled=false;}});card.append(form);
      }
      else if(drafts.get(task.id)){
        const draft=node("pre",drafts.get(task.id));draft.dataset.taskId=task.id;
        card.append(node("p",tr("任务状态已变化；未提交草稿留在当前页面，可复制后另行处理。"),"muted"),draft);
      }
      const details=node("details"),summary=node("summary",tr("协作记录 · {n}",{n:task.events.length}));details.append(summary);
      for(const event of task.events){const item=node("div",undefined,"collab-event");item.append(node("p",`${label(event.actor_id)} · ${actions[event.kind] || event.kind}`,"muted"));if(event.body)item.append(node("pre",event.body));details.append(item);}
      card.append(details);target.append(card);
    }
  }
  function renderLeanEnrollments(items){
    const target=get("lean-enrollments");target.replaceChildren();
    if(!items.length){target.append(node("p",tr("此版本尚无自愿 Lean 预检报名。"),"muted"));return;}
    for(const item of items){
      const card=node("article",undefined,"card"),r=item.request;
      card.append(node("p",tr("目标 {target} · {state} · 附件 {attachment} · 摘要 {digest}…",{target:r.target,state:item.state,attachment:r.attachment_id.slice(0,8),digest:r.attachment_sha256.slice(0,16)})));
      card.append(node("p",tr("问题来源：{ref}。本站预检不判断原题覆盖；官方状态：未提交。",{ref:r.problem_reference||tr("未关联")}),"muted"));
      if(item.report){
        card.append(node("p",tr("本机 Lean：{status}；sorryAx：{sorry}；语义对应：未判定。",{status:item.report.status,sorry:item.report.sorry_axiom_observed?tr("观察到"):tr("未在本次输出中观察到")})));
        const details=node("details"),summary=node("summary",tr("查看这次私有工具输出"));
        details.append(summary,node("pre",item.report.log||tr("没有输出")));card.append(details);
        card.append(act(tr("下载本地提交准备草稿"),()=>{
          const lines=[tr("# 数学成果提交准备草稿（仅本机，未发送）"),"",
            tr("请作者核对署名、原题与形式命题的对应、完整覆盖、原始证明来源和准备公开的确切文件。本站预检不是官方验收。"),"",
            tr("问题来源（作者自述）：{ref}",{ref:r.problem_reference||tr("待补")}),
            tr("本站版本 ID：{id}",{id:r.revision_id}),tr("本站版本 SHA-256：{digest}",{digest:r.revision_sha256}),
            tr("Lean 附件 ID：{id}",{id:r.attachment_id}),tr("Lean 附件 SHA-256：{digest}",{digest:r.attachment_sha256}),
            tr("检查目标：{target}",{target:r.target}),tr("本站工具：{tool}",{tool:item.report.tool}),
            tr("本站机械状态：{status}",{status:item.report.status}),tr("观察到 sorryAx：{observed}",{observed:item.report.sorry_axiom_observed}),
            tr("原题对应与完整覆盖：未判定"),tr("官方提交与审核：未进行"),"",
            tr("## 作者需补全后自行决定是否提交"),"",
            tr("- 原始证明仓库、完整提交 SHA、许可和可复现构建步骤：待补。"),
            tr("- 原题到形式命题的定义、假设、量词及边界逐项映射：待补。"),
            tr("- 全部未解决部分、额外公理和人工复核证据：待补。"),
            tr("- 准备公开的文件清单、署名和贡献说明：待补。"),
            tr("- 按提交时官方规则核对格式与渠道：待补。"),""];
          const url=URL.createObjectURL(new Blob([lines.join("\n")],{type:"text/markdown;charset=utf-8"}));
          const link=document.createElement("a");link.href=url;link.download=`lean-submission-draft-${item.id}.md`;
          link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
        }));
      }
      if(item.state!=="withdrawn")card.append(act(tr("退出这项私有检查"),async()=>{
        try {await api("/api/research/lean-withdraw",{enrollment_id:item.id});}
        catch(error){
          if(uncertainWrite(error)){await loadLean();tell(tr("退出结果未确认。请先核对当前报名状态，再决定是否重试。"));}
          else tell(error.message);
          return;
        }
        await loadLean();tell(tr("已记录退出；已有私有检查日志仍保留供核对，不会对外发送。"));
      }));
      target.append(card);
    }
  }
  async function loadLean(materials=null){
    const owner=!!context&&context.actor_id===context.revision.actor_id;
    const panel=get("lean-activity"),selector=get("lean-attachment");panel.hidden=!owner;
    if(!owner){selector.replaceChildren();get("lean-enrollments").replaceChildren();return false;}
    if(materials){
      const previous=selector.value;selector.replaceChildren();
      for(const item of materials.filter(file=>file.filename.toLowerCase().endsWith(".lean"))){
        const option=node("option",`${item.filename} · ${item.sha256.slice(0,16)}…`);option.value=item.id;selector.append(option);
      }
      if([...selector.querySelectorAll("option")].some(option=>option.value===previous))selector.value=previous;
    }
    const stamp=epoch,actor=context.actor_id,revision=context.revision.id;
    try{
      const result=await api("/api/research/lean-enrollments");
      if(stamp!==epoch||context.actor_id!==actor||context.revision.id!==revision)return false;
      renderLeanEnrollments((result.items||[]).filter(item=>item.request.revision_id===revision));return true;
    }catch(error){
      if(stamp===epoch)get("lean-enrollments").textContent=tr("私有 Lean 报名暂时无法读取：{error}",{error:error.message});
      return false;
    }
  }
  async function load({clearTaskDraft=null}={}){
    if(!context)return false;
    const stamp=++epoch, rev=context.revision, owner=context.actor_id===rev.actor_id;
    const contextKey=`${rev.id}:${context.actor_id||"guest"}`;
    if(loadedContext!==contextKey){get("research-tasks").replaceChildren();get("lean-enrollments").replaceChildren();
      get("lean-attachment").replaceChildren();pendingLeanOperation=null;loadedContext=contextKey;}
    get("attachment-editor").hidden=!owner;
    get("research-files").replaceChildren(node("p",tr("正在读取材料…"),"muted"));
    if(typeof ensureRecords==="function"&&!records.length)await ensureRecords();
    if(stamp!==epoch)return false;
    const versions=records.filter(r=>r.type==="revision"&&r.payload.exploration_id===rev.payload.exploration_id), chooser=get("diff-before");
    chooser.replaceChildren();for(const [i,r] of versions.entries()){const o=node("option",tr("版本 {n}",{n:i+1}));o.value=r.id;chooser.append(o);}
    const index=versions.findIndex(r=>r.id===rev.id);if(versions.length)chooser.value=versions[Math.max(0,index-1)].id;
    get("compare-versions").disabled=versions.length<2;get("diff-output").replaceChildren();
    try{
      const result=await api(`/api/research/workspace?revision_id=${encodeURIComponent(rev.id)}`);if(stamp!==epoch)return;
      mountHTML(get("detail-body"),result.rendered);get("render-state").textContent=result.rendered.notice?tr(result.rendered.notice):"";
      const compileInfo=result.compile||{enabled:false,archives:[]};
      const archiveByAttId=new Map((compileInfo.archives||[]).map(a=>[a.attachment_id,a]));
      const compileStateLabels={queued:tr("排队中"),running:tr("编译中"),succeeded:tr("编译成功"),failed:tr("编译失败"),cancelled:tr("已取消")};
      const isSourceName=name=>/\.(?:zip|tar\.gz|tgz|tar)$/i.test(String(name||""));
      const list=get("research-files");list.replaceChildren();
      for(const item of result.materials.items){
        const card=node("article",undefined,"material-card");
        if(item.has_preview){const image=document.createElement("img");image.src=`/api/research/preview-file?attachment_id=${item.id}`;image.alt=item.filename;image.loading="lazy";card.append(image);}
        const link=node("a",item.filename);link.href=`/api/research/file?attachment_id=${item.id}`;link.rel="ugc nofollow noopener noreferrer";card.append(link);
        if(item.origin==="compiled"){
          const ci=item.compile_info||{};
          if(item.mime==="text/html"||String(item.filename||"").toLowerCase().endsWith(".html")){
            card.append(node("p",tr("本站从源码转换 · LaTeXML {version}",{version:ci.latexml||"0.8"}),"badge"));
          }else{
            card.append(node("p",tr("本站从源码编译 · TeX Live {year} · {engine}",{year:ci.texlive||"2026",engine:ci.engine||"pdflatex"}),"badge"));
          }
        }
        const arch=archiveByAttId.get(item.id);
        if(arch||isSourceName(item.filename)){
          card.append(node("span",tr("LaTeX 源码包"),"badge"));
        }
        const meta=node("p",undefined,"muted"),sizeSpan=node("span",formatSize(item.size));sizeSpan.title=formatSizeTitle(item.size);
        meta.append(sizeSpan,node("span",` · ${tr("SHA-256 {sha}",{sha:item.sha256.slice(0,16)+"…"})}`));card.append(meta);
        if(item.has_preview){const markup=node("code",`![${item.filename.replace(/[\[\]]/g,"")}](/api/research/preview-file?attachment_id=${item.id})`);card.append(node("p",tr("可复制到下一版本的正文："),"muted"),markup);}
        if(arch||isSourceName(item.filename)){
          const compileBox=node("div",undefined,"compile-controls");
          const latest=arch?arch.latest_job:null;
          if(latest){
            const stText=compileStateLabels[latest.state]||latest.state;
            compileBox.append(node("p",tr("编译状态：{state}（主文件：{main}）",{state:stText,main:latest.main_file||arch.default_main_file||"main.tex"})));
            if(latest.state==="failed"&&owner){
              if(latest.error)compileBox.append(node("p",latest.error,"muted"));
              const logOut=node("pre",undefined,"compile-log");logOut.hidden=true;
              compileBox.append(act(tr("查看编译日志"),async()=>{
                const logData=await api(`/api/research/compile-log?job_id=${encodeURIComponent(latest.id)}`);
                logOut.textContent=logData.log_text||logData.error||tr("没有输出");
                logOut.hidden=false;
              }),logOut);
            }
          }
          if(!compileInfo.enabled){
            compileBox.append(node("p",tr("本站编译暂未开放"),"muted"));
          }else if(owner&&arch){
            const mainLabel=node("label",tr("主文件 ")),mainSelect=node("select");
            const candidates=(arch.tex_files&&arch.tex_files.length)?arch.tex_files:[arch.default_main_file||"main.tex"];
            for(const tf of candidates){const opt=node("option",tf);opt.value=tf;mainSelect.append(opt);}
            mainSelect.value=(latest&&latest.main_file)||arch.default_main_file||candidates[0];
            mainLabel.append(mainSelect);
            const btnLabel=latest?tr("重新编译"):tr("请求本站编译");
            const compileBtn=act(btnLabel,async()=>{
              requireIdentity();
              await api("/api/research/compile",{revision_id:rev.id,attachment_id:item.id,main_file:mainSelect.value});
              await load();
              tell(tr("已加入本站编译队列。"));
            });
            compileBox.append(mainLabel,compileBtn);
          }
          card.append(compileBox);
        }
        list.append(card);
      }
      if(!result.materials.items.length)list.append(node("p",tr("这版还没有附件。文字本身也可以是完整的探索起点。"),"muted"));
      get("materials-manifest").textContent=tr("本版材料清单 {digest}… · 新版本不自动继承附件",{digest:result.materials.manifest_sha256.slice(0,16)});
      renderTasks(result.tasks,{clearTaskDraft});
      document.dispatchEvent(new CustomEvent("eksmu:research-loaded",{detail:{revision_id:rev.id,
        materials:result.materials.items,tasks:result.tasks,rendered_abstract:result.rendered_abstract||null}}));
      void loadLean(result.materials.items);
      return true;
    }catch(e){if(stamp===epoch){get("render-state").textContent=e.message;get("research-files").textContent=tr("材料服务暂不可用，原文和讨论仍保留。");}return false;}
  }
  async function recoverTaskFailure(error,hasDraft){
    const draft=hasDraft?tr("进展草稿已保留。"):"";
    if(error.status===409){
      const refreshed=await load();
      tell(refreshed?tr("任务已有新进展，列表已刷新。请查看记录后重新操作。")+draft:tr("任务已有新进展，但列表刷新失败；请稍后刷新。")+draft);
    }else if(error.status===undefined){
      const refreshed=await load();
      tell(refreshed?tr("提交结果未确认，已刷新协作记录。请先检查是否已有这条操作，再决定是否重试。")+draft:tr("提交结果未确认，协作记录也暂时无法刷新；请恢复连接后先检查记录。")+draft);
    }else tell(tr("{error}。",{error:error.message})+draft);
  }
  function parseManuscript(filename,raw){
    const text=String(raw||"").replace(/\r\n?/g,"\n");
    const isTex=filename.toLowerCase().endsWith(".tex")||/\\(?:documentclass|begin\{document\})/.test(text);
    if(isTex){
      const cleanTex=s=>s.replace(/%.*$/gm,"").trim();
      const titleMatch=text.match(/\\title(?:\[[^\]]*\])?\{((?:[^{}]|\{[^{}]*\})*)\}/);
      const title=titleMatch?cleanTex(titleMatch[1]).replace(/\\thanks\{[^{}]*\}/g,"").replace(/\\\\/g," ").replace(/\s+/g," ").trim():"";
      const absMatch=text.match(/\\begin\{abstract\}([\s\S]*?)\\end\{abstract\}/);
      const abstract=absMatch?cleanTex(absMatch[1]).replace(/\n{3,}/g,"\n\n").trim():"";
      const docMatch=text.match(/\\begin\{document\}([\s\S]*?)\\end\{document\}/);
      let body=docMatch?docMatch[1]:text;
      body=body.replace(/\\begin\{abstract\}[\s\S]*?\\end\{abstract\}/g,"")
        .replace(/\\(?:maketitle|tableofcontents)\b/g,"")
        .replace(/\\section\*?\{([^{}]*)\}/g,"\n## $1\n")
        .replace(/\\subsection\*?\{([^{}]*)\}/g,"\n### $1\n")
        .replace(/\\subsubsection\*?\{([^{}]*)\}/g,"\n#### $1\n")
        .replace(/\\textbf\{([^{}]*)\}/g,"**$1**")
        .replace(/\\(?:emph|textit)\{([^{}]*)\}/g,"*$1*")
        .replace(/\\begin\{(?:itemize|enumerate)\}|\\end\{(?:itemize|enumerate)\}/g,"")
        .replace(/^\s*\\item\s+/gm,"- ")
        .trim();
      return {title:title.slice(0,200),body:body.slice(0,50000),abstract:abstract.slice(0,6000)};
    }
    const lines=text.split("\n");
    let title="",body=text.trim();
    const firstIdx=lines.findIndex(l=>l.trim().length>0);
    if(firstIdx>=0&&/^#\s+/.test(lines[firstIdx].trim())){
      title=lines[firstIdx].trim().replace(/^#\s+/,"").trim();
      body=lines.slice(firstIdx+1).join("\n").trim()||title;
    }else{
      title=filename.replace(/\.(?:md|markdown|txt|tex)$/i,"").trim();
    }
    return {title:title.slice(0,200),body:body.slice(0,50000),abstract:""};
  }
  function isSourceArchive(name){
    return /\.(?:zip|tar\.gz|tgz|tar)$/i.test(String(name||""));
  }
  async function fileToBase64(file){
    if(typeof file.arrayBuffer==="function"){
      const buf=await file.arrayBuffer();
      const bytes=new Uint8Array(buf);
      let binary="";
      const chunk=0x8000;
      for(let i=0;i<bytes.length;i+=chunk){
        binary+=String.fromCharCode.apply(null,bytes.subarray(i,i+chunk));
      }
      return btoa(binary);
    }
    if(typeof file.data_b64==="string")return file.data_b64;
    if(typeof file.text==="function"){
      const s=await file.text();
      return btoa(unescape(encodeURIComponent(s)));
    }
    throw new Error(tr("无法读取所选文件。"));
  }
  function clearPublishArchive(){
    if(typeof window!=="undefined")window.EksmuPublishArchive=null;
    const info=get("source-archive-info");if(info)info.hidden=true;
    const btn=get("import-manuscript-btn-text");if(btn)btn.textContent=tr("选择文件或源码包");
    const nameEl=get("source-archive-name");if(nameEl)nameEl.textContent="";
    const metaEl=get("source-archive-meta");if(metaEl)metaEl.textContent="";
    const input=get("import-manuscript");if(input)input.value="";
  }
  document.addEventListener("eksmu:revision",e=>{context=e.detail;load();});
  document.addEventListener("eksmu:published",()=>{
    try{localStorage.removeItem(draftKey());}catch{}
    get("publish-preview").hidden=true;
    get("draft-state").textContent="";
    clearPublishArchive();
  });
  get("publish").addEventListener("input",saveDraft);
  get("import-manuscript").addEventListener("change",async e=>{
    const file=e.target.files&&e.target.files[0];if(!file)return;
    const form=get("publish");
    try{
      if(isSourceArchive(file.name)){
        if(!current)throw new Error(tr("请先登录或注册后再上传源码包解析。"));
        if(file.size>32*1024*1024)throw new Error(tr("单个附件不能超过 32 MiB；文字发表不受影响"));
        tell(tr("正在解析源码包…"));
        const b64=await fileToBase64(file);
        const result=await api("/api/research/inspect-source",{filename:file.name,data_b64:b64});
        const parsed=parseManuscript(result.main_file||file.name,result.tex);
        if(!parsed.body&&!parsed.title)throw new Error(tr("源码包中没有可导入的正文内容。"));
        if(form.elements.body.value.trim()&&!confirm(tr("用所选文件中的内容替换当前标题与正文草稿？")))return;
        if(parsed.title)form.elements.title.value=parsed.title;
        if(parsed.body)form.elements.body.value=parsed.body;
        if(parsed.abstract&&typeof window!=="undefined"&&window.EksmuPapers){
          const currentPaper=window.EksmuPapers.read("publish")||{authors:[],references:[],license:"",provenance:{source:"",prior_publication_date:""}};
          currentPaper.abstract=parsed.abstract;
          window.EksmuPapers.fill("publish",currentPaper);
        }
        if(typeof window!=="undefined"){
          window.EksmuPublishArchive={filename:file.name,data_b64:b64,size:file.size,main_file:result.main_file};
        }
        const info=get("source-archive-info");
        if(info){
          info.hidden=false;
          const nameEl=get("source-archive-name");if(nameEl)nameEl.textContent=file.name;
          const metaEl=get("source-archive-meta");if(metaEl)metaEl.textContent=tr("{size} · 主文件 {main}",{size:formatSize(file.size),main:result.main_file||"main.tex"});
        }
        const btn=get("import-manuscript-btn-text");if(btn)btn.textContent=tr("更换文件或源码包");
        saveDraft();
        tell(tr("已解析 LaTeX 源码包并填入标题与正文；发表时将自动附上源码包并启动编译。"));
      }else{
        if(file.size>2*1024*1024)throw new Error(tr("导入稿件不能超过 2 MiB；较大文件请在发表后作为附件上传。"));
        const parsed=parseManuscript(file.name||"",await file.text());
        if(!parsed.body)throw new Error(tr("所选文件没有可导入的正文内容。"));
        if(form.elements.body.value.trim()&&!confirm(tr("用所选文件中的内容替换当前标题与正文草稿？")))return;
        clearPublishArchive();
        if(parsed.title)form.elements.title.value=parsed.title;
        form.elements.body.value=parsed.body;
        if(parsed.abstract&&typeof window!=="undefined"&&window.EksmuPapers){
          const currentPaper=window.EksmuPapers.read("publish")||{authors:[],references:[],license:"",provenance:{source:"",prior_publication_date:""}};
          currentPaper.abstract=parsed.abstract;
          window.EksmuPapers.fill("publish",currentPaper);
        }
        const btn=get("import-manuscript-btn-text");if(btn)btn.textContent=tr("更换文件或源码包");
        saveDraft();
        tell(tr("已从文件导入标题与正文，请核对或点击“预览排版”后发表。"));
      }
    }catch(error){tell(error.message);}finally{e.target.value="";}
  });
  const clearArchiveBtn=get("clear-source-archive");
  if(clearArchiveBtn&&clearArchiveBtn.addEventListener)clearArchiveBtn.addEventListener("click",()=>{
    clearPublishArchive();
    tell(tr("已清除所选源码包。"));
  });
  const materialInput=get("material-input");
  if(materialInput&&materialInput.addEventListener){
    materialInput.addEventListener("change",e=>{
      const f=e.target.files&&e.target.files[0];
      const nameEl=get("material-file-name");
      if(nameEl)nameEl.textContent=f?`${f.name} (${formatSize(f.size)})`:"";
    });
  }
  get("restore-draft").addEventListener("click",()=>{
    try{const saved=JSON.parse(localStorage.getItem(draftKey())||"null");if(!saved)return tell(tr("此身份在当前设备没有保存的草稿。"));const form=get("publish");if(form.elements.body.value.trim()&&!confirm(tr("用本设备保存的草稿替换当前输入？")))return;for(const key of ["title","body","kind"])if(typeof saved[key]==="string")form.elements[key].value=saved[key];if(typeof window!=="undefined"&&window.EksmuPapers)window.EksmuPapers.fill("publish",saved.paper||null);tell(tr("草稿已恢复。"));}catch{tell(tr("无法读取草稿。"));}
  });
  get("preview-publish").addEventListener("click",()=>preview("publish","publish-preview"));
  get("preview-revise").addEventListener("click",()=>preview("revise","revise-preview"));
  get("compare-versions").addEventListener("click",async()=>{
    if(!context)return;const rev=context.revision.id;
    try{const result=await api(`/api/research/diff?${new URLSearchParams({before_id:get("diff-before").value,after_id:rev})}`);if(context.revision.id!==rev)return;
      const out=get("diff-output");out.replaceChildren();if(result.title_before!==result.title_after)out.append(node("p",tr("标题：{before} → {after}",{before:result.title_before,after:result.title_after})));
      for(const change of result.paper_changes||[]){out.append(node("h4",tr("论文资料：{field}",{field:change.field})),node("pre",tr("之前：{before}\n之后：{after}",{before:JSON.stringify(change.before,null,2),after:JSON.stringify(change.after,null,2)})));}
      for(const line of result.lines)out.append(node("div",line,line.startsWith("+")?"diff-add":line.startsWith("-")?"diff-remove":"diff-context"));if(!result.lines.length)out.append(node("p",tr("正文没有变化。")));if(result.truncated)out.append(node("p",tr("差异较长，已显示前2000行。")));
    }catch(e){tell(e.message);}
  });
  get("upload-material").addEventListener("submit",async e=>{
    e.preventDefault();const file=get("material-input").files[0],b=e.currentTarget.querySelector("button");if(!file||!context)return;b.disabled=true;
    try{requireIdentity();if(file.size>32*1024*1024)throw new Error(tr("单个附件不能超过 32 MiB；文字发表不受影响"));const target=context.revision.id;
      const data=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result).split(",")[1]);reader.onerror=()=>reject(new Error(tr("读取附件失败")));reader.readAsDataURL(file);});
      try{await api("/api/research/upload",{revision_id:target,filename:file.name,data_b64:data});}
      catch(error){
        if(uncertainWrite(error)){
          const refreshed=await load();
          tell(refreshed?tr("附件提交结果未确认，已刷新本版材料。请核对文件名和摘要，再决定是否重试；所选文件仍保留。"):tr("附件提交结果未确认，材料列表暂时无法刷新。请恢复连接后先核对；所选文件仍保留。"));
        }else tell(error.message);
        return;
      }
      get("material-input").value="";
      const nameEl=get("material-file-name");if(nameEl)nameEl.textContent="";
      const refreshed=await load();document.dispatchEvent(new CustomEvent("eksmu:materials"));
      tell(refreshed?tr("材料已保存到所选版本；原始文件可下载，图片可预览。"):tr("附件已写入，但材料列表暂时无法刷新；请稍后查看。"));
    }catch(error){tell(error.message);}finally{b.disabled=false;}
  });
  get("create-collaboration").addEventListener("submit",async e=>{
    e.preventDefault();const form=e.currentTarget,b=form.querySelector("button");b.disabled=true;
    try{requireIdentity();if(!context)throw new Error(tr("请先选择一个作品版本"));await api("/api/research/create-task",{revision_id:context.revision.id,...Object.fromEntries(new FormData(form))});form.reset();const refreshed=await load();tell(refreshed?tr("新的协作入口已经建立，任何参与者都可以加入。"):tr("协作入口已写入，但任务列表暂未刷新；请稍后刷新查看。"));}
    catch(error){
      if(error.status===undefined){const refreshed=await load();tell(refreshed?tr("提出协作的结果未确认，已刷新任务列表。请先检查是否已创建，再决定是否重试；填写内容仍保留。"):tr("提出协作的结果未确认，任务列表暂时无法刷新；请恢复连接后检查。填写内容仍保留。"));}
      else tell(tr("{error}；填写内容仍保留。",{error:error.message}));
    }finally{b.disabled=false;}
  });
  get("lean-enroll").addEventListener("submit",async e=>{
    e.preventDefault();const form=e.currentTarget,button=form.querySelector("button");button.disabled=true;
    try{
      requireIdentity();if(!context||context.actor_id!==context.revision.actor_id)throw new Error(tr("只有本版作者可以报名自己的 Lean 附件。"));
      if(!form.elements.consent.checked)throw new Error(tr("请先确认固定材料的私有预检授权。"));
      const request={revision_id:context.revision.id,attachment_id:form.elements.attachment_id.value,
        target:form.elements.target.value.trim(),problem_reference:form.elements.problem_reference.value.trim(),
        consent:{local_check:true,result_visibility:"private",external_transfer:false}};
      const key=JSON.stringify(request);
      if(!pendingLeanOperation||pendingLeanOperation.key!==key)pendingLeanOperation={key,id:crypto.randomUUID()};
      try{await api("/api/research/lean-enroll",{...request,operation_id:pendingLeanOperation.id});}
      catch(error){
        if(uncertainWrite(error)){await loadLean();tell(tr("Lean 报名结果未确认，已尝试刷新私有清单。请先核对，再决定是否重试；当前输入与操作标识保留。"));}
        else tell(error.message);
        return;
      }
      pendingLeanOperation=null;await loadLean();tell(tr("已加入私有预检队列。运行需维护者在停服后执行；发表与讨论不受影响。"));
    }catch(error){tell(error.message);}finally{button.disabled=false;}
  });
  for(const [id,endpoint,name] of [["export-materials","export-materials","eksmu-materials"],["export-collaboration","export-tasks","eksmu-collaboration"]]){
    get(id).addEventListener("click",async()=>{if(!context)return;try{download(await api(`/api/research/${endpoint}?revision_id=${context.revision.id}`),`${name}-${context.revision.id}.json`);}catch(e){tell(e.message);}});
  }
  get("import-materials").addEventListener("change",async e=>{const file=e.target.files[0];if(!file)return;
    try{requireIdentity();if(file.size>128*1024*1024)throw new Error(tr("材料包不能超过 128 MiB；文字发表不受影响"));
      try{await api("/api/research/import-materials",{bundle:JSON.parse(await file.text())});}
      catch(error){
        if(uncertainWrite(error)){const refreshed=await load();tell(refreshed?tr("材料恢复结果未确认，已刷新列表。请核对文件和摘要；若需重试，请重新选择本机材料包。"):tr("材料恢复结果未确认，列表暂时无法刷新；请恢复连接后先核对。材料包仍在本机。"));}
        else tell(error.message);
        return;
      }
      const refreshed=await load();document.dispatchEvent(new CustomEvent("eksmu:materials"));tell(refreshed?tr("材料已恢复，原始字节和文件校验保持一致。"):tr("材料已写入，但列表暂时无法刷新；请稍后查看。"));
    }catch(error){tell(error.message);}finally{e.target.value="";}
  });
})();
