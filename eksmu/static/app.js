"use strict";
const $ = (id) => document.getElementById(id);
let records = [], profiles = {}, current = null, selected = null, revision = null, reply = null;
let recordsLoaded = false, recordsPromise = null, workspaceRetry = null;
let behaviorEpoch = 0;
let accountScriptIssue = "";
window.addEventListener("error", event => {
  if (event.target instanceof HTMLScriptElement && event.target.src.endsWith("/accounts.js")) {
    accountScriptIssue = tr("浏览器未取得账户脚本，可能是请求失败或被扩展拦截。");
  } else if (event.filename?.endsWith("/accounts.js")) {
    const kind = event.error?.name || tr("脚本错误");
    accountScriptIssue = tr("账户脚本执行失败（{kind}，第 {line} 行）。",{kind,line:event.lineno || "?"});
  }
}, true);
const kinds = {question:tr("问题"),conjecture:tr("猜想"),finding:tr("发现"),experiment:tr("实验"),computation:tr("计算"),failed_attempt:tr("失败的尝试"),other:tr("其他")};
const statuses = {unreviewed:tr("未评定"),supported:tr("支持"),challenged:tr("提出质疑"),inconclusive:tr("无法判定"),resource_limited:tr("资源不足")};
function node(tag, value, css) { const e = document.createElement(tag); if (value !== undefined) e.textContent = value; if (css) e.className = css; return e; }
function formatSize(bytes) {
  const n = Number(bytes) || 0;
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KiB`;
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MiB`;
  return `${(n / (1024 * 1024 * 1024)).toFixed(1)} GiB`;
}
function formatSizeTitle(bytes) {
  return tr("{size} 字节", {size: (Number(bytes) || 0).toLocaleString("en-US")});
}
function tell(text) { $("message").textContent = text; }
// The home template serves several views; switch between them without a reload.
const VIEW_PATHS = {discover:"", publish:"publish", account:"account", activity:"activity", workspace:"workspace"};
function viewFromPath(pathname) { const last = pathname.split("/").filter(Boolean).pop() || ""; return Object.keys(VIEW_PATHS).find(v => VIEW_PATHS[v] === last) || "discover"; }
function showView(view, hash = "", replace = false) {
  if (!document.body.dataset.view || !(view in VIEW_PATHS)) return;
  if (document.body.dataset.view === view && !hash) return;
  if (document.body.dataset.view !== view) { document.body.dataset.view = view; window.scrollTo(0, 0); }
  if (view === "publish") {
    const sp = $("publish-success"), cp = $("compose-panel");
    if (sp) sp.hidden = true;
    if (cp) cp.hidden = false;
  }
  history[replace ? "replaceState" : "pushState"]({view}, "", `/${window.EKSMU_LANG || "zh"}/${VIEW_PATHS[view]}${hash}`);
  markView(view);
  for (const a of document.querySelectorAll("a.lang-switch")) { const u = new URL(a.href); u.pathname = u.pathname.replace(/^\/(zh|en)\/.*$/, `/$1/${VIEW_PATHS[view]}`); u.hash = location.hash; a.href = u.href; }
}
function markView(view) {
  const titles = {discover:tr("Eksmu · 开放探索共同体"), publish:tr("发表 · Eksmu"), account:tr("账户 · Eksmu"), activity:tr("我的动态 · Eksmu"), workspace:tr("工作区 · Eksmu")};
  document.title = titles[view];
  const here = `/${window.EKSMU_LANG || "zh"}/${VIEW_PATHS[view]}`;
  for (const a of document.querySelectorAll(".topnav a")) { if (new URL(a.href).pathname === here) a.setAttribute("aria-current","page"); else a.removeAttribute("aria-current"); }
}
window.addEventListener("popstate", () => {
  if (!document.body.dataset.view) return;
  document.body.dataset.view = viewFromPath(location.pathname);
  markView(document.body.dataset.view);
  if (document.body.dataset.view === "workspace") {
    const id = new URLSearchParams(location.hash.slice(1)).get("exploration") || selected;
    const ver = new URLSearchParams(location.hash.slice(1)).get("revision") || revision;
    if (id && (!recordsLoaded || $("detail").hidden) && window.EksmuCommunity) {
      void window.EksmuCommunity.openWork(id, ver, false);
    }
  }
});
function alias(id) {
  const key = String(id || "");
  const r = records.find(x => x.id === key) || (current?.id === key ? current : null);
  return profiles[key]?.display_name || (r ? r.payload.alias : key.slice(0,8));
}
function label(id) {
  const key = String(id || "");
  return `${alias(key)}${profiles[key]?.affiliation ? tr(" · {affiliation}（作者自述）",{affiliation:profiles[key].affiliation}) : ""} · ${key.slice(0,8)}`;
}
function hasExplorationHash() {
  return Boolean(new URLSearchParams(location.hash.slice(1)).get("exploration"));
}
function inWorkspace() {
  const view = document.body?.dataset?.view;
  if (view) return view === "workspace" && Boolean(selected);
  return Boolean(selected);
}
function invalidateRecords() { recordsLoaded = false; }
function showWorkspaceLoading() {
  const box = $("workspace-state"), text = $("workspace-state-text"), retry = $("workspace-retry");
  if (!box) return;
  if (!selected) $("detail").hidden = true;
  box.hidden = false;
  if (text) text.textContent = tr("正在加载作品与讨论记录…");
  if (retry) retry.hidden = true;
  workspaceRetry = null;
}
function showWorkspaceError(error, retryAction = null) {
  const box = $("workspace-state"), text = $("workspace-state-text"), retry = $("workspace-retry");
  if (!box) return;
  if (!selected) $("detail").hidden = true;
  box.hidden = false;
  if (text) text.textContent = tr("作品记录加载失败：{error}。请检查连接后重试。", {error: error.message});
  workspaceRetry = retryAction;
  if (retry) retry.hidden = !retryAction;
}
function clearWorkspaceState() {
  const box = $("workspace-state"), text = $("workspace-state-text"), retry = $("workspace-retry");
  if (!box) return;
  box.hidden = true;
  if (text) text.textContent = "";
  if (retry) retry.hidden = true;
  workspaceRetry = null;
}
async function ensureRecords(force = false) {
  if (recordsLoaded && !force) return records;
  if (recordsPromise && !force) return recordsPromise;
  const task = api("/api/state").then(state => {
    records = state.records;
    recordsLoaded = true;
    return records;
  }).finally(() => {
    if (recordsPromise === task) recordsPromise = null;
  });
  recordsPromise = task;
  return task;
}
async function api(path, data) {
  const headers = {"X-Eksmu-Lang": window.EKSMU_LANG || "zh"};
  const opts = {headers, credentials:"same-origin"};
  if (data !== undefined) { opts.method = "POST"; headers["Content-Type"] = "application/json"; headers["X-Eksmu-Client"] = "local-prototype"; if(current) headers['X-Eksmu-Actor']=current.id; opts.body = JSON.stringify(data); }
  let response;
  try { response = await fetch(path, opts); }
  catch (error) { if (data !== undefined) error.writeUncertain = true; throw error; }
  let result;
  try { result = await response.json(); }
  catch (error) { if (data !== undefined) error.writeUncertain = true; throw error; }
  if (!response.ok) { const error = new Error(tr(result.error || `HTTP ${response.status}`)); error.status=response.status; throw error; } return result;
}
function uncertainWrite(error) { return error.writeUncertain === true || (typeof error.status === "number" && error.status >= 500); }
function download(value, name) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:"application/json"}));
  const a = document.createElement("a"); a.href = url; a.download = name; a.click(); setTimeout(() => URL.revokeObjectURL(url),1000);
}
function requireIdentity() { if (!current) throw new Error(tr("请先注册或登录账户。无需任何学术资格。")); }
async function refresh() {
  const previous = current?.id;
  const account = await api("/api/auth/status");
  current = account.actor;
  document.body.classList.toggle("signed-in", !!current);
  if (previous && previous !== current?.id) {
    behaviorEpoch++;
    $("behavior-cases").replaceChildren();
    for (const id of ["publish","revise","comment","evidence","behavior-report"]) $(id).reset();
    for (const id of ["publish", "revise"]) window.EksmuPapers.fill(id, null);
    $("revise").dataset.dirty="false";
    for (const id of ["publish-preview","revise-preview"]) { $(id).hidden=true; $(id).replaceChildren(); }
    $("draft-state").textContent="";
  }
  // Account controls need the auth status, not the public feed. Show them even
  // when a later, unrelated read fails during page startup.
  if (window.EksmuAccounts) await window.EksmuAccounts.render(account);
  const loadStateNow = inWorkspace() || !window.EksmuCommunity;
  if (loadStateNow) await ensureRecords(true);
  else recordsLoaded = false;
  profiles = (await api("/api/profiles")).profiles;
  if (!current && ["following","bookmarks","mine"].includes($("view").value)) $("view").value="all";
  $("who").textContent = current ? tr("当前参与者：{label}。本设备会话可单独退出。",{label:label(current.id)}) : tr("浏览无需登录。发表时可用真实姓名或笔名；不要求学历、机构或推荐人。");
  await renderFeed(); if (selected && loadStateNow) renderDetail();
  await renderBehaviorCases();
  document.dispatchEvent(new CustomEvent("eksmu:state"));
}
function renderFeed() {
  if (window.EksmuCommunity) return window.EksmuCommunity.renderFeed();
  const feed = $("feed"); feed.replaceChildren();
  let works = records.filter(r => r.type === "exploration").reverse();
  if ($("view").value === "unanswered") works = works.filter(e => {
    const ids = new Set(records.filter(r => r.type === "revision" && r.payload.exploration_id === e.id).map(r => r.id));
    return !records.some(r => ["comment","evidence"].includes(r.type) && ids.has(r.payload.revision_id));
  });
  if (!works.length) feed.append(node("p",tr("这里还没有作品，或者所有作品都已有回应。第一个问题也值得被写下来。"),"muted"));
  for (const work of works) {
    const versions = records.filter(r => r.type === "revision" && r.payload.exploration_id === work.id);
    const latest = versions[versions.length-1], box = node("article",undefined,"entry");
    const open = node("button",latest.payload.title,"link");
    open.addEventListener("click",() => { selected = work.id; revision = latest.id; reply = null; showView("workspace", `#${new URLSearchParams({exploration:selected,revision})}`); renderDetail(); $("detail").scrollIntoView({behavior:"smooth"}); });
    box.append(node("span",kinds[work.payload.kind],"badge"),node("span",tr("版本 {n} · {author}",{n:versions.length,author:label(work.actor_id)}),"muted"),open,node("p",(latest.payload.paper?.abstract || latest.payload.body).slice(0,180)));
    feed.append(box);
  }
}
async function renderDetail() {
  if (!selected) return;
  if (!recordsLoaded && !records.length) {
    try { await ensureRecords(); } catch (error) { tell(error.message); return; }
  }
  const work = records.find(r => r.id === selected); if (!work) return;
  const versions = records.filter(r => r.type === "revision" && r.payload.exploration_id === selected);
  if (!versions.length) return;
  if (!versions.some(r => r.id === revision)) revision = versions[versions.length-1].id;
  const rev = records.find(r => r.id === revision), chooser = $("versions"); chooser.replaceChildren();
  versions.forEach((r,i) => { const option = node("option",tr("版本 {n} · {time}",{n:i+1,time:r.created_at})); option.value = r.id; chooser.append(option); }); chooser.value = revision;
  $("detail").hidden = false; $("detail-title").textContent = rev.payload.title;
  $("work-page-link").href = `/${window.EKSMU_LANG || "zh"}/w/${encodeURIComponent(selected)}${revision === versions[versions.length-1].id ? "" : `?revision=${encodeURIComponent(revision)}`}`;
  $("detail-author").textContent = tr("作者：{author} · {kind}",{author:label(work.actor_id),kind:kinds[work.payload.kind]});
  $("detail-body").textContent = rev.payload.body;
  $("version-meta").textContent = tr("记录 {id} · 内容校验摘要 {digest}…（不是原创性或正确性证明）",{id:rev.id,digest:rev.sha256.slice(0,16)});
  const latest = versions[versions.length-1];
  $("edit-box").hidden = !current || current.id !== work.actor_id;
  const editForm = $("revise"), editIdentity = `${selected}:${current?.id || "guest"}`;
  if (editForm.dataset.identity !== editIdentity || editForm.dataset.dirty !== "true") {
    editForm.elements.title.value = latest.payload.title; editForm.elements.body.value = latest.payload.body;
    editForm.elements.note.value = "";
    window.EksmuPapers.fill("revise", latest.payload.paper || null);
    editForm.dataset.identity = editIdentity; editForm.dataset.parentRevision = latest.id; editForm.dataset.dirty = "false";
  }
  const reports = $("reports"), comments = $("comments"); reports.replaceChildren(); comments.replaceChildren();
  const behaviorTarget = $("behavior-target"); behaviorTarget.replaceChildren();
  const behaviorForm = $("behavior-report"), behaviorContext = `${revision}:${current?.id || "guest"}`;
  if (behaviorForm.dataset.context !== behaviorContext) {
    behaviorForm.elements.reason.value = "";
    behaviorForm.dataset.context = behaviorContext;
  }
  for (const item of [rev, ...records.filter(r => ["comment","evidence"].includes(r.type) && r.payload.revision_id === revision)]) {
    const option = node("option",`${item.type === "revision" ? tr("正文版本") : item.type === "comment" ? tr("评论") : tr("分析")} · ${item.id.slice(0,8)}`);
    option.value = item.id; behaviorTarget.append(option);
  }
  for (const type of ["evidence","comment"]) {
    const entries = records.filter(r => r.type === type && r.payload.revision_id === revision);
    const target = type === "evidence" ? reports : comments;
    if (!entries.length) target.append(node("p",type === "evidence" ? tr("此版本尚无证据报告。这不影响它的发表和讨论。") : tr("还没有回应。可以从一句问题开始。"),"muted"));
    for (const entry of entries) {
      const p = entry.payload, card = node("article",undefined,"card");
      card.append(node("h4",`${label(entry.actor_id)} · ${type === "evidence" ? tr("参与者提交的报告") : tr("评论")}`));
      if (type === "evidence") {
        card.append(node("p",tr("来源声明：{source} {tool} · 报告者判断：{status} · 本站未核验",{source:p.source,tool:p.tool,status:statuses[p.status]}),"muted"));
        for (const [key,name] of [["scope",tr("范围")],["method",tr("方法")],["result",tr("结果")],["limitations",tr("局限")]]) card.append(node("pre",tr("{label}：{value}",{label:name,value:p[key] || tr("未说明")})));
      } else { if (p.reply_to) card.append(node("p",tr("回复记录 {id}",{id:p.reply_to.slice(0,8)}),"muted")); card.append(node("pre",p.body)); }
      const respond = node("button",tr("回复这条记录"),"secondary"); respond.addEventListener("click",() => { reply = entry.id; renderReply(); $("comment").elements.body.focus(); });
      card.append(respond); target.append(card);
    }
  }
  renderReply();
  document.dispatchEvent(new CustomEvent("eksmu:revision", {detail:{revision:rev,actor_id:current?.id || null}}));
}
async function renderBehaviorCases({clearDraftCaseId=null,clearDraftPurpose="response"}={}) {
  const stamp=++behaviorEpoch, actorId=current?.id;
  const target = $("behavior-cases");
  if (!current) { target.replaceChildren(node("p",tr("登录后可查看与你相关的私下反馈。"),"muted")); return false; }
  const drafts=new Map([...target.querySelectorAll("article[data-case-id]")].flatMap(card=>
    [...card.querySelectorAll("textarea[data-purpose]")].map(field=>[`${card.dataset.caseId}:${field.dataset.purpose}`,field.value])));
  try {
    const result = await api("/api/community/behavior-cases");
    if (stamp!==behaviorEpoch || current?.id!==actorId) return false;
    target.replaceChildren();
    if (!result.items.length) { target.append(node("p",tr("目前没有与你相关的行为反馈。"),"muted")); return true; }
    for (const item of result.items) {
      const card=node("article",undefined,"card");card.dataset.caseId=item.id;
      card.append(node("p",tr("{category} · {state} · 记录 {id}",{category:item.category,state:item.state,id:item.target.slice(0,8)}),"muted"),node("p",item.reason));
      const eventLabels={response:tr("相关作者回应"),review_no_action:tr("本地复核：本轮无行为处理"),
        review_follow_up:tr("本地复核：仍需后续核对"),appeal_reporter:tr("报告者申请复议"),
        appeal_author:tr("相关作者申请复议"),appeal_review_uphold:tr("离线复议记录：维持原记录"),
        appeal_review_reopen:tr("离线复议记录：需要重新核对")};
      for (const entry of item.events) card.append(node("p",tr("{label}：{value}",{label:eventLabels[entry.kind]||tr("案件记录"),value:entry.body})));
      if (item.target_author === current.id && item.events.filter(e=>e.kind==="response").length < 3) {
        const form=node("form"), field=node("textarea"); field.required=true; field.maxLength=2000;
        field.dataset.purpose="response";
        if(item.id!==clearDraftCaseId||clearDraftPurpose!=="response")field.value=drafts.get(`${item.id}:response`)||"";
        const button=node("button",tr("回应这项反馈")); form.append(field,button);
        form.addEventListener("submit",async event=>{ event.preventDefault(); button.disabled=true;
          try {
            try { await api("/api/community/behavior-response",{case_id:item.id,body:field.value}); }
            catch(error){
              if(uncertainWrite(error)){const refreshed=await renderBehaviorCases();tell(refreshed?tr("回应结果未确认，已刷新私下反馈。请先核对是否已有这条回应；草稿仍在。"):tr("回应结果未确认，反馈暂时无法刷新。请恢复连接后先核对；草稿仍在。"));}
              else tell(error.message);
              return;
            }
            field.value="";const refreshed=await renderBehaviorCases({clearDraftCaseId:item.id});
            tell(refreshed?tr("回应已记录。"):tr("回应已写入，但反馈列表暂时无法刷新；请稍后查看。"));
          } finally {button.disabled=false;}
        }); card.append(form);
      }
      const role=item.reporter===current.id?"reporter":"author";
      if (item.state==="reviewed" && item.events.some(e=>e.kind==="review_no_action"||e.kind==="review_follow_up") &&
          !item.events.some(e=>e.kind===`appeal_${role}`)) {
        const form=node("form"), field=node("textarea"), button=node("button",tr("申请复议"));
        field.required=true;field.maxLength=2000;field.dataset.purpose="appeal";
        if(item.id!==clearDraftCaseId||clearDraftPurpose!=="appeal")field.value=drafts.get(`${item.id}:appeal`)||"";
        form.append(node("p",tr("复议只补充行为处理理由，不改变发表状态。请避免重复写入敏感资料。"),"muted"),field,button);
        form.addEventListener("submit",async event=>{event.preventDefault();button.disabled=true;
          try {
            try {await api("/api/community/behavior-appeal",{case_id:item.id,body:field.value});}
            catch(error){
              if(uncertainWrite(error)){const refreshed=await renderBehaviorCases();tell(refreshed?tr("复议提交结果未确认，已刷新案件。请核对是否已记录，再决定是否重试。"):tr("复议提交结果未确认，案件暂时无法刷新。恢复连接后先核对；当前输入仍在。"));}
              else tell(error.message);
              return;
            }
            field.value="";const refreshed=await renderBehaviorCases({clearDraftCaseId:item.id,clearDraftPurpose:"appeal"});
            tell(refreshed?tr("复议已私下记录，等待离线复核。"):tr("复议已写入，但案件列表暂时无法刷新；请稍后查看。"));
          } finally {button.disabled=false;}
        });card.append(form);
      }
      target.append(card);
    }
    return true;
  } catch(error) { if (stamp===behaviorEpoch && current?.id===actorId) tell(tr("暂时无法读取私下反馈：{error}",{error:error.message}));return false; }
}
function renderReply() { $("reply-target").textContent = reply ? tr("正在回复记录 {id}",{id:reply}) : tr("评论绑定当前查看的版本；欢迎质疑报告和提出不同解释。"); $("clear-reply").hidden = !reply; }
document.addEventListener("eksmu:research-loaded",event=>{
  if(event.detail.revision_id!==revision)return;
  const chooser=$("behavior-target"),before=chooser.value;
  const known=new Set([...chooser.options].map(option=>option.value));
  for(const file of event.detail.materials){if(known.has(file.id))continue;const option=node("option",tr("附件 · {filename} · {id}",{filename:file.filename,id:file.id.slice(0,8)}));option.value=file.id;chooser.append(option);known.add(file.id);}
  for(const task of event.detail.tasks)for(const entry of task.events){
    if(known.has(entry.id))continue;
    const option=node("option",tr("协作记录 · {title} · {id}",{title:task.title,id:entry.id.slice(0,8)}));option.value=entry.id;chooser.append(option);
    known.add(entry.id);
  }
  if([...chooser.options].some(option=>option.value===before))chooser.value=before;
});
function submit(id, action) {
  $(id).addEventListener("submit",async event => {
    event.preventDefault(); const button = $(id).querySelector('button[type="submit"], button:not([type])'); button.disabled = true;
    try {
      try { await action(Object.fromEntries(new FormData($(id)))); }
      catch (error) {
        if (uncertainWrite(error)) {
          let refreshed = false;
          try { await refresh(); refreshed = true; } catch {}
          tell(refreshed
            ? tr("提交结果未确认，已刷新当前记录。请先核对是否已有这次提交，再决定是否重试；当前输入已保留。")
            : tr("提交结果未确认，记录也暂时无法刷新。请恢复连接后先核对，再决定是否重试；当前输入已保留。"));
        } else tell(error.message);
        return;
      }
      try { await refresh(); }
      catch { tell(tr("写入已确认，但页面暂时无法刷新。请稍后刷新查看已保存的记录。")); }
    } finally { button.disabled = false; }
  });
}
submit("publish",async d => {
  requireIdentity();
  const archive = (typeof window !== "undefined") ? window.EksmuPublishArchive : null;
  const r = await api("/api/explorations",{...d,paper:window.EksmuPapers.read("publish")});
  $("publish").reset();
  window.EksmuPapers.fill("publish", null);
  document.dispatchEvent(new CustomEvent("eksmu:published"));

  if(archive){
    let uploadOk=false, uploadErr="";
    try{
      await api("/api/research/upload",{
        revision_id: r.revision.id,
        filename: archive.filename,
        data_b64: archive.data_b64
      });
      uploadOk=true;
    }catch(err){
      uploadErr=err.message||tr("上传失败");
    }
    const successPanel=$("publish-success"), composePanel=$("compose-panel");
    if(successPanel&&composePanel){
      composePanel.hidden=true;
      successPanel.hidden=false;
      const workUrl=`/${window.EKSMU_LANG || "zh"}/w/${encodeURIComponent(r.exploration.id)}#published`;
      if($("published-work-link"))$("published-work-link").href=workUrl;
      if($("published-workspace-link"))$("published-workspace-link").href=`/${window.EKSMU_LANG || "zh"}/workspace`;
      const msg=$("publish-success-message");
      if(uploadOk){
        if(msg)msg.textContent=tr("作品发表成功！已附上源码包，编译已排队。");
        tell(tr("作品发表成功！已附上源码包，编译已排队。"));
      }else{
        if(msg)msg.textContent=tr("作品已经发表成功，但源码包上传失败（{error}），可以到工作区重试。",{error:uploadErr});
        tell(tr("作品已经发表成功，但源码包上传失败，可以到工作区重试。"));
      }
      return;
    }
  }

  const successPanel=$("publish-success"), composePanel=$("compose-panel");
  if(successPanel&&composePanel){
    composePanel.hidden=true;
    successPanel.hidden=false;
    const workUrl=`/${window.EKSMU_LANG || "zh"}/w/${encodeURIComponent(r.exploration.id)}#published`;
    if($("published-work-link"))$("published-work-link").href=workUrl;
    if($("published-workspace-link"))$("published-workspace-link").href=`/${window.EKSMU_LANG || "zh"}/workspace`;
    const msg=$("publish-success-message");
    if(msg)msg.textContent=tr("作品发表成功！");
    tell(tr("作品发表成功！"));
    return;
  }

  location.assign(`/${window.EKSMU_LANG || "zh"}/w/${encodeURIComponent(r.exploration.id)}#published`);
});
$("publish-another")?.addEventListener("click", () => {
  const successPanel = $("publish-success"), composePanel = $("compose-panel");
  if (successPanel) successPanel.hidden = true;
  if (composePanel) composePanel.hidden = false;
  window.scrollTo(0, 0);
});
submit("revise",async d => { requireIdentity(); await ensureRecords(); const all = records.filter(r => r.type === "revision" && r.payload.exploration_id === selected); const r = await api("/api/revisions",{...d,exploration_id:selected,parent_revision_id:$("revise").dataset.parentRevision || all[all.length-1].id,paper:window.EksmuPapers.read("revise")}); $("revise").dataset.dirty="false"; revision = r.id; reply = null; tell(tr("新版本已保存。旧版本、论文资料及其评论、报告保持不变。")); });
submit("comment",async d => { requireIdentity(); await api("/api/comments",{...d,revision_id:revision,reply_to:reply}); $("comment").reset(); reply = null; tell(tr("回应已记录。")); });
submit("evidence",async d => { requireIdentity(); await api("/api/evidence",{...d,revision_id:revision}); $("evidence").reset(); tell(tr("报告已附加；这是参与者的主张，不是本站核验结论。")); });
submit("behavior-report",async d => { requireIdentity(); const result=await api("/api/community/behavior-report",d); $("behavior-report").elements.reason.value=""; tell(tr("私下反馈已记录：{id}。提交不代表已核实或已处理。",{id:result.id})); });
$("revise").addEventListener("input",()=>{$("revise").dataset.dirty="true";});
$("view").addEventListener("change",renderFeed);
$("versions").addEventListener("change",() => { revision = $("versions").value; reply = null; renderDetail(); });
$("clear-reply").addEventListener("click",() => { reply = null; renderReply(); });
$("export").addEventListener("click",async () => { try { download(await api("/api/export"),"eksmu-bundle.json"); tell(tr("记录已导出，不包含身份密钥。请勿公开私有资料。")); } catch(e) { tell(e.message); } });
$("workspace-retry")?.addEventListener("click", () => { if (workspaceRetry) workspaceRetry(); });
window.addEventListener('DOMContentLoaded', () => {
  const legacy = {"#compose-panel":"publish", "#inbox-panel":"activity", "#account-panel":"account"};
  if (document.body.dataset.view === "discover" && legacy[location.hash]) showView(legacy[location.hash], "", true);
  if (hasExplorationHash()) {
    if (document.body.dataset.view === "discover") showView("workspace", location.hash, true);
    showWorkspaceLoading();
  }
  const loading = $("account-loading");
  if (!window.EksmuAccounts) {
    loading.textContent = (accountScriptIssue || tr("账户脚本未能启动，原因尚未确定。")) + tr("请报告浏览器名称和控制台的第一条错误（不要包含凭据）。");
    return;
  }
  window.EksmuAccounts.start().catch(e => {
    if (!loading.hidden) loading.textContent = tr("账户服务未就绪。请检查 SSH 隧道并强制刷新；若仍失败，请报告浏览器控制台的第一条错误（不要包含凭据）。");
    tell(e.message);
  });
});
