"use strict";
// A single community experience on the existing app, not another prototype.
(() => {
  let offset = 0, next = null, epoch = 0, detailEpoch = 0, personalEpoch = 0, routeEpoch = 0, hashRouting = false;
  let metadataVersion = 0, metadataIdentity = "", noticeIds = [];
  const kindsText = {question:tr("问题"),conjecture:tr("猜想"),finding:tr("发现"),experiment:tr("实验"),computation:tr("计算"),failed_attempt:tr("失败的尝试"),other:tr("其他")};
  const labelsText = {commercial_promotion:tr("商业推广"),pending_review:tr("待复核")};
  const $c = id => document.getElementById(id);
  const uncertainWrite = error => error.writeUncertain === true || (typeof error.status === "number" && error.status >= 500);
  const button = (text, action, css="secondary") => {
    const b = node("button", text, css); b.type = "button";
    b.addEventListener("click", async () => {
      b.disabled = true;
      try { await action(); } catch (error) { tell(error.message); }
      finally { b.disabled = false; }
    });
    return b;
  };
  function route() {
    const query = new URLSearchParams(location.hash.slice(1));
    return {id:query.get("exploration"), version:query.get("revision"), section:query.get("section")};
  }
  // Links from the public work page can name the part of the page the reader wants.
  const SECTIONS={comments:"comment",analysis:"analysis-section",revise:"edit-box",publication:"publication-section",materials:"materials-section"};
  async function openWork(id, version=null, setHash=true, section=null) {
    const stamp = ++routeEpoch;
    const uncached = typeof recordsLoaded === "boolean" && !recordsLoaded;
    const missing = !records.some(r => r.id===id && r.type==="exploration") ||
      Boolean(version && !records.some(r => r.id===version && r.type==="revision"));
    const needsLoad = typeof ensureRecords === "function" && (uncached || missing);
    if (needsLoad) {
      const hash = `#${new URLSearchParams(version ? {exploration:id,revision:version} : {exploration:id})}`;
      if (typeof showView === "function") showView("workspace", hash, !setHash);
      if (typeof showWorkspaceLoading === "function") showWorkspaceLoading();
      try {
        await ensureRecords(!uncached && missing);
      } catch (error) {
        if (stamp !== routeEpoch) return;
        if (typeof showWorkspaceError === "function") {
          showWorkspaceError(error, () => openWork(id, version, setHash, section));
        }
        tell(tr("作品记录加载失败：{error}。请检查连接后重试。", {error: error.message}));
        return;
      }
      if (stamp !== routeEpoch) return;
    }
    if (typeof clearWorkspaceState === "function") clearWorkspaceState();
    const work = records.find(r => r.id===id && r.type==="exploration");
    if (!work) { tell(tr("本站找不到链接中的作品。")); return; }
    const versions = records.filter(r => r.type==="revision" && r.payload.exploration_id===id);
    const chosen = version ? versions.find(r => r.id===version) : versions.at(-1);
    if (!chosen) { tell(tr("这个版本不属于链接中的作品。")); return; }
    selected=id; revision=chosen.id; reply=null;
    if(typeof showView==="function")showView("workspace",`#${new URLSearchParams({exploration:id,revision:chosen.id})}`,!setHash);
    renderDetail();
    if (setHash) history.replaceState(null,"",`#${new URLSearchParams({exploration:id,revision:chosen.id})}`);
    const target=SECTIONS[section]&&$c(SECTIONS[section]);
    (target&&!target.hidden?target:$c("detail")).scrollIntoView({behavior:"smooth",block:"start"});
  }
  function queryParams() {
    return new URLSearchParams({q:$c("search-query").value.trim(),topic:$c("topic-filter").value,
      view:$c("view").value,order:$c("feed-order").value,offset:String(offset),limit:"12"});
  }
  let siteTotal=0;
  async function renderCommunityFeed() {
    const stamp=++epoch, feed=$c("feed");
    $c("feed-summary").textContent=tr("正在寻找作品…");
    try {
      const response=await api(`/api/community/feed?${queryParams()}`);
      if (stamp!==epoch) return;
      feed.replaceChildren(); next=response.next_offset;
      const empty=!response.total&&!$c("search-query").value.trim()&&!$c("topic-filter").value&&$c("view").value==="all";
      $c("feed-summary").textContent=response.total ? tr("共 {total} 项作品 · 显示 {from}—{to}",{total:response.total,from:response.offset+1,to:response.offset+response.items.length}) : empty ? "" : tr("这里还没有匹配的作品。你的问题可以成为起点。");
      $c("page-prev").disabled=offset===0; $c("page-next").disabled=next===null;
      // Only one page: no pager. Few works overall: the topic row adds noise, card chips still filter.
      $c("feed-pagination").hidden=offset===0&&next===null;
      if(!$c("search-query").value.trim()&&!$c("topic-filter").value&&$c("view").value==="all")siteTotal=response.total;
      $c("topic-pills").hidden=siteTotal<10&&!$c("topic-filter").value;
      const aiWork = response.items.find(w => w.ai_reports);
      const aiLink = $c("hero-ai-link");
      if (aiLink) {
        if (aiWork) aiLink.href = `/${window.EKSMU_LANG||"zh"}/w/${encodeURIComponent(aiWork.id)}#work-analysis`;
        else aiLink.href = `/${window.EKSMU_LANG||"zh"}/participation.html`;
      }
      const hasFilter = Boolean($c("topic-filter").value || ($c("view").value && $c("view").value !== "all") || ($c("feed-order").value && $c("feed-order").value !== "latest"));
      const fBox = $c("filter-box");
      if (hasFilter && fBox) {
        fBox.hidden = false;
        const toggle = $c("filter-toggle");
        if (toggle) {
          if (toggle.setAttribute) toggle.setAttribute("aria-expanded", "true");
          toggle.dataset.active = "true";
        }
      }
      for (const work of response.items) {
        const card=node("article",undefined,"entry discovery-card");
        const meta=node("div",undefined,"entry-meta");
        const authorLink=node("a",work.author,"author-link");authorLink.href=`/${window.EKSMU_LANG||"zh"}/u/${encodeURIComponent(work.author_id)}`;
        const byline=node("span",undefined,"muted author-byline");
        byline.append(authorLink,node("span",`${work.affiliation?tr(" · {affiliation}（作者自述）",{affiliation:work.affiliation}):""}`));
        meta.append(node("span",kindsText[work.kind],"badge kind-badge"),byline);
        const modLabel=work.moderation_label||work.label?.label||null;
        const modReason=work.moderation_reason||work.label?.reason||"";
        if(modLabel){
          meta.append(node("span",labelsText[modLabel]||modLabel,"badge badge-moderation"));
        }
        const title=node("a",work.title,"link entry-title");title.href=`/${window.EKSMU_LANG||"zh"}/w/${encodeURIComponent(work.id)}`;
        card.append(meta,title);
        if(work.excerpt) card.append(node("p",work.excerpt,"entry-excerpt"));
        if(modLabel&&modReason)card.append(node("p",tr("不予分发理由：{reason}",{reason:modReason}),"muted moderation-reason"));
        if(work.help) card.append(node("p",tr("一起推进：{help}",{help:work.help}),"collaboration-note"));
        if(work.topics && work.topics.length){
          const tags=node("div",undefined,"topic-chips");
          for(const topic of work.topics) tags.append(button(`# ${topic}`,async()=>{ $c("topic-filter").value=topic;offset=0;await renderCommunityFeed(); },"chip"));
          card.append(tags);
        }
        const hudMeta=node("div",undefined,"entry-hud-meta mono");
        hudMeta.append(node("span",tr("版本 {n}",{n:work.versions}),"hud-meta-item"));
        if(!modLabel){
          if(work.bitcoin_height)hudMeta.append(node("span",tr("⛓ 比特币区块 #{height} 存证",{height:work.bitcoin_height}),"hud-meta-item meta-accent"));
          else if(work.chain_seq)hudMeta.append(node("span",tr("⛓ SHA-256 链 #{seq}",{seq:work.chain_seq}),"hud-meta-item meta-accent"));
          if(work.ai_reports)hudMeta.append(node("span",tr("🤖 AI 审阅报告"),"hud-meta-item meta-hot"));
        }
        hudMeta.append(node("span",tr("{comments} 条讨论 · {reports} 份分析",{comments:work.comments,reports:work.reports+(work.ai_reports||0)})+(work.following?tr(" · 已关注"):"")+(work.bookmarked?tr(" · 已收藏"):""),"hud-meta-item"));
        card.append(hudMeta);
        feed.append(card);
      }
      if(!response.items.length){
        if(empty){
          const box=node("div",undefined,"card empty-state"),cta=node("a",tr("发表我的问题"),"button");
          cta.href="/publish";
          cta.addEventListener("click",event=>{if(typeof showView==="function"){event.preventDefault();showView("publish");$c("publish").elements.body.focus();$c("compose-panel").scrollIntoView({behavior:"smooth"});}});
          box.append(node("p",tr("还没有作品，你的问题可以成为第一篇")),cta);
          feed.append(box);
        }else feed.append(button(tr("写下我的第一个问题"),()=>{if(typeof showView==="function")showView("publish");$c("publish").elements.body.focus();$c("compose-panel").scrollIntoView({behavior:"smooth"});}));
      }
    } catch(error) {
      if(stamp!==epoch)return;
      $c("feed-summary").textContent=tr("发现服务暂不可用：{error}。仍可发表，也可导出已有记录。",{error:error.message});
    }
  }
  async function personal() {
    const stamp=++personalEpoch;
    const target=$c("notifications"); target.replaceChildren(); noticeIds=[];
    $c("read-notices").disabled=true;
    if(!current) {$c("notice-count").textContent=tr("创建账户后，讨论与协作动态会在这里出现。");return false;}
    try {
      const result=await api("/api/community/notifications");
      if(stamp!==personalEpoch)return;
      $c("notice-count").textContent=result.unread ? tr("{n} 条未读动态",{n:result.unread}) : tr("暂时没有未读动态，继续看看大家的问题。");
      noticeIds=result.items.filter(x=>!x.read).map(x=>x.id);
      $c("read-notices").disabled=!noticeIds.length;
      for(const item of result.items){
        const box=node("article",undefined,`notice-card ${item.read?"is-read":""}`);
        const collaboration={created:tr("提出了协作"),join:tr("加入了协作"),leave:tr("退出了协作"),progress:tr("记录了协作进展"),close:tr("结束了协作"),reopen:tr("重新开放了协作")};
        const analysis={report:tr("完成了对你作品的 AI 分析报告"),response:tr("回应了你作品 AI 分析中的一条发现")};
        const action=item.type==="collaboration"?collaboration[item.action]:item.type==="analysis"?analysis[item.action]:{comment:tr("参与了讨论"),evidence:tr("提交了分析"),revision:tr("更新了版本")}[item.type];
        box.append(node("p",tr("{from} {action}",{from:item.from,action})+(item.read?"":tr(" · 新")),"muted"));
        box.append(button(item.title,async()=>{await openWork(item.exploration_id,item.revision_id,true,item.type==="analysis"?"analysis":null);await api("/api/community/read",{record_ids:[item.id]});await personal();},"link"));
        box.append(node("p",item.excerpt));target.append(box);
      }
      return true;
    }catch(error){if(stamp===personalEpoch)$c("notice-count").textContent=error.message;return false;}
  }
  async function extras() {
    try {
      const result=await api("/api/community/topics");
      const chooser=$c("topic-filter"), prior=chooser.value;
      chooser.replaceChildren(node("option",tr("全部主题")));chooser.firstElementChild.value="";
      for(const topic of result.items){const option=node("option",`${topic.name} (${topic.explorations})`);option.value=topic.name;chooser.append(option);}
      chooser.value=[...chooser.options].some(x=>x.value===prior)?prior:"";
      const pills=$c("topic-pills");
      pills.replaceChildren();
      if(result.items.length){
        pills.append(button(tr("全部主题"),async()=>{chooser.value="";offset=0;await renderCommunityFeed();},"chip"));
        for(const topic of result.items)pills.append(button(`# ${topic.name} (${topic.explorations})`,async()=>{chooser.value=topic.name;offset=0;await renderCommunityFeed();},"chip"));
      }
    }catch(error){tell(tr("主题列表暂不可用：{error}",{error:error.message}));}
    await personal();
  }
  async function detail(context) {
    const stamp=++detailEpoch;
    const wid=context.revision.payload.exploration_id, identity=`${wid}:${context.actor_id}`;
    try{
      const meta=await api(`/api/community/details?exploration_id=${encodeURIComponent(wid)}`);
      if(stamp!==detailEpoch)return;
      const tags=$c("detail-topics");tags.replaceChildren(...meta.topics.map(t=>node("span",`# ${t}`,"badge")));
      $c("detail-help").textContent=meta.help?tr("希望一起完成：{help}",{help:meta.help}):"";
      $c("follow-work").textContent=meta.following?tr("取消关注"):tr("关注后续进展");
      $c("follow-work").dataset.enabled=String(meta.following);
      $c("bookmark-work").textContent=meta.bookmarked?tr("取消收藏"):tr("收藏到我的清单");
      $c("bookmark-work").dataset.enabled=String(meta.bookmarked);
      if(!records.some(x=>x.id===wid)&&typeof ensureRecords==="function")await ensureRecords();
      if(stamp!==detailEpoch)return;
      const work=records.find(x=>x.id===wid), owns=Boolean(work&&work.actor_id===context.actor_id);
      $c("community-edit").hidden=!owns;
      const form=$c("community-details");
      if(metadataIdentity!==identity){$c("community-use-current").hidden=true;$c("community-save-state").textContent="";}
      if(metadataIdentity!==identity || form.dataset.dirty!=="true"){
        form.elements.topics.value=meta.topics.join(", ");form.elements.help.value=meta.help;
        metadataVersion=meta.version;metadataIdentity=identity;form.dataset.dirty="false";
      }
    }catch(error){if(stamp===detailEpoch)$c("detail-help").textContent=tr("协作介绍暂不可用：{error}",{error:error.message});}
  }
  window.EksmuCommunity={renderFeed:renderCommunityFeed,openWork};
  document.addEventListener("eksmu:state",()=>{extras();const link=route();if(!hashRouting&&!selected&&link.id)void openWork(link.id,link.version,false,link.section);});
  document.addEventListener("eksmu:revision",event=>detail(event.detail));
  window.addEventListener("hashchange",async()=>{
    const stamp=++routeEpoch, link=route(); if(!link.id)return;
    try {
      // Another author may have published since this tab loaded. A version URL
      // must resolve against current records, including same-document links.
      if(typeof invalidateRecords==="function")invalidateRecords();
      hashRouting=true;
      try { await refresh(); } finally { hashRouting=false; }
      if(stamp===routeEpoch)await openWork(link.id,link.version,false,link.section);
    } catch(error) {
      if(stamp===routeEpoch){
        if(typeof showWorkspaceError==="function")showWorkspaceError(error,()=>openWork(link.id,link.version,false,link.section));
        tell(tr("作品记录加载失败：{error}。请检查连接后重试。",{error:error.message}));
      }
    }
  });
  $c("search-form").addEventListener("submit",event=>{event.preventDefault();offset=0;renderCommunityFeed();});
  const filterToggle=$c("filter-toggle");
  if(filterToggle)filterToggle.addEventListener("click",()=>{
    const box=$c("filter-box");
    if(!box)return;
    box.hidden=!box.hidden;
    if(filterToggle.setAttribute)filterToggle.setAttribute("aria-expanded",String(!box.hidden));
    filterToggle.dataset.active=String(!box.hidden);
  });
  for(const id of ["topic-filter","view","feed-order"])$c(id).addEventListener("change",()=>{offset=0;renderCommunityFeed();});
  $c("page-prev").addEventListener("click",()=>{offset=Math.max(0,offset-12);renderCommunityFeed();});
  $c("page-next").addEventListener("click",()=>{if(next!==null){offset=next;renderCommunityFeed();}});
  $c("refresh-community").addEventListener("click",()=>refresh().catch(e=>tell(e.message)));
  for(const [id,kind] of [["follow-work","follow"],["bookmark-work","bookmark"]]){
    $c(id).addEventListener("click",async()=>{
      const b=$c(id);b.disabled=true;
      try{requireIdentity();
        try{await api("/api/community/mark",{exploration_id:selected,kind,enabled:b.dataset.enabled!=="true"});}
        catch(error){
          if(uncertainWrite(error)){
            const workId=selected,actorId=current?.id;
            let refreshed=false;
            try{
              const latest=await api(`/api/community/details?exploration_id=${encodeURIComponent(workId)}`);
              if(workId===selected&&actorId===current?.id){
                const enabled=kind==="follow"?latest.following:latest.bookmarked;
                b.dataset.enabled=String(enabled);
                b.textContent=kind==="follow"?(enabled?tr("取消关注"):tr("关注后续进展")):(enabled?tr("取消收藏"):tr("收藏到我的清单"));
                refreshed=true;
              }
            }catch{}
            tell(refreshed?tr("操作结果未确认，已刷新当前清单；请核对关注或收藏状态后再决定是否重试。"):tr("操作结果未确认，清单暂时无法刷新；请恢复连接后先核对状态。"));
          }else tell(error.message);
          return;
        }
        try{await refresh();tell(kind==="follow"?tr("关注已更新；关注后的新进展会出现在我的回应中。"):tr("收藏已更新，只属于你的个人清单。"));}
        catch{tell(tr("关注或收藏写入已确认，但页面暂时无法刷新；请稍后查看。"));}
      }catch(error){tell(error.message);}finally{b.disabled=false;}
    });
  }
  async function showCurrentIntroduction(){
    const identity=`${selected}:${current?.id||"guest"}`, workId=selected;
    $c("community-use-current").hidden=true;
    try{
      const latest=await api(`/api/community/details?exploration_id=${encodeURIComponent(workId)}`);
      if(identity!==`${selected}:${current?.id||"guest"}`)return false;
      const ack=$c("community-use-current");ack.dataset.version=String(latest.version);ack.dataset.identity=identity;ack.hidden=false;
      $c("community-save-state").textContent=tr("服务器当前介绍（版本 {version}）：主题 {topics}；协作说明 {help}。当前草稿仍在上方，请核对后再选择是否继续保存。",{version:latest.version,topics:latest.topics.join(tr("、"))||tr("无"),help:latest.help||tr("无")});
      return true;
    }catch{return false;}
  }
  $c("community-use-current").addEventListener("click",()=>{
    const ack=$c("community-use-current");
    if(ack.dataset.identity!==`${selected}:${current?.id||"guest"}`)return;
    metadataVersion=Number(ack.dataset.version);ack.hidden=true;
    $c("community-save-state").textContent=tr("已选择服务器版本 {version} 作为保存基础；当前草稿未改变。",{version:metadataVersion});
  });
  $c("community-details").addEventListener("input",()=>{$c("community-details").dataset.dirty="true";});
  $c("community-details").addEventListener("submit",async event=>{
    event.preventDefault();const form=event.currentTarget,b=form.querySelector("button");b.disabled=true;
    try{requireIdentity();const data=Object.fromEntries(new FormData(form));
      try{await api("/api/community/details",{exploration_id:selected,topics:data.topics.split(/[,，]/).map(x=>x.trim()).filter(Boolean),help:data.help,expected_version:metadataVersion});}
      catch(error){
        if(error.status===409){const shown=await showCurrentIntroduction();tell(shown?tr("介绍已有新版本。当前草稿已保留；请先对照服务器内容，再明确选择是否用当前草稿继续。"):tr("介绍已有新版本，但暂时无法读取。当前草稿已保留，请稍后刷新核对。"));}
        else if(uncertainWrite(error)){const shown=await showCurrentIntroduction();tell(shown?tr("保存结果未确认，已显示服务器当前介绍。请先核对是否已保存；当前草稿仍在。"):tr("保存结果未确认，服务器介绍暂时无法读取。请恢复连接后先核对；当前草稿仍在。"));}
        else tell(error.message);
        return;
      }
      form.dataset.dirty="false";$c("community-use-current").hidden=true;$c("community-save-state").textContent="";
      try{await refresh();tell(tr("主题与协作介绍已保存，其他人可以按方向找到这项作品。"));}
      catch{tell(tr("主题与协作介绍已写入，但页面暂时无法刷新；请稍后查看。"));}
    }catch(error){tell(error.message);}finally{b.disabled=false;}
  });
  $c("read-notices").addEventListener("click",async()=>{
    try{
      try{await api("/api/community/read",{record_ids:noticeIds});}
      catch(error){
        if(uncertainWrite(error)){const refreshed=await personal();tell(refreshed?tr("已读操作结果未确认，动态列表已刷新；请核对未读状态。"):tr("已读操作结果未确认，动态暂时无法刷新；请稍后核对。"));}
        else tell(error.message);
        return;
      }
      const refreshed=await personal();tell(refreshed?tr("显示的动态已标记为已读。"):tr("已读写入已确认，但动态列表暂时无法刷新；请稍后查看。"));
    }catch(error){tell(error.message);}
  });
  $c("copy-work-link").addEventListener("click",async()=>{
    if(!selected||!revision)return;
    const url=new URL(`/w/${encodeURIComponent(selected)}`,location.href);url.search=new URLSearchParams({revision});
    $c("work-link").value=url.href;$c("work-link").hidden=false;
    try{await navigator.clipboard.writeText(url.href);tell(tr("当前版本链接已复制；本地地址供同一实例使用。"));}catch{$c("work-link").select();tell(tr("链接已显示，可以复制。"));}
  });
  $c("export-personal").addEventListener("click",async()=>{try{requireIdentity();download(await api("/api/community/export"),"eksmu-personal.json");tell(tr("个人清单和自己作品的主题介绍已导出。"));}catch(e){tell(e.message);}});
  $c("import-personal").addEventListener("change",async event=>{
    const file=event.target.files[0];if(!file)return;
    try{requireIdentity();if(file.size>1900000)throw new Error(tr("个人备份超过本机导入限制"));
      if(!confirm(tr("将用备份替换当前身份的关注、收藏和已读记录，保留文章及他人数据。继续吗？")))return;
      try{await api("/api/community/import",{bundle:JSON.parse(await file.text())});}
      catch(error){
        if(uncertainWrite(error)){let refreshed=false;try{await refresh();refreshed=true;}catch{}
          tell(refreshed?tr("个人清单恢复结果未确认，已刷新当前清单。请核对关注、收藏和已读状态；若需重试，请重新选择本机备份。"):tr("个人清单恢复结果未确认，暂时无法刷新。请恢复连接后先核对；备份文件仍在本机。"));}
        else tell(error.message);
        return;
      }
      try{await refresh();tell(tr("个人清单已恢复。"));}
      catch{tell(tr("个人清单已写入，但页面暂时无法刷新；请稍后查看。"));}
    }catch(error){tell(error.message);}finally{event.target.value="";}
  });
  const starters={question:tr("我想弄明白的问题是：\n\n我已经观察或尝试过：\n\n我希望一起讨论："),finding:tr("我观察到了：\n\n我是这样得到这个结果的：\n\n接下来想检查："),failed_attempt:tr("我原来想尝试：\n\n实际发生了：\n\n这次尝试留下的问题：")};
  for(const b of document.querySelectorAll("[data-starter]"))b.addEventListener("click",()=>{
    const form=$c("publish");if(!form.elements.body.value.trim()){form.elements.kind.value=b.dataset.starter;form.elements.body.value=starters[b.dataset.starter];}
    if(typeof showView==="function")showView("publish");
    $c("compose-panel").scrollIntoView({behavior:"smooth"});form.elements.body.focus();
  });
})();
