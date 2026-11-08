"use strict";
// Public work page: show the author's own actions, request the free AI analysis
// in place, and refresh while an analysis is queued or running.
(() => {
  const $ = id => document.getElementById(id);
  const lang = () => window.EKSMU_LANG || "zh";

  async function call(path, data) {
    const headers = {"X-Eksmu-Lang": lang()}, opts = {headers, credentials: "same-origin"};
    if (data !== undefined) {
      opts.method = "POST"; headers["Content-Type"] = "application/json"; headers["X-Eksmu-Client"] = "local-prototype";
      opts.body = JSON.stringify(data);
    }
    const response = await fetch(path, opts), result = await response.json();
    if (!response.ok) throw new Error(window.tr ? tr(result.error || `HTTP ${response.status}`) : result.error);
    return result;
  }

  function watchAnalysis() {
    const section = $("work-analysis");
    if (!section || !["queued", "running"].includes(section.dataset.analysisState)) return;
    const revision = $("author-actions")?.dataset.revision;
    if (!revision) return;
    const timer = setInterval(async () => {
      try {
        const view = await call(`/api/analysis?revision_id=${encodeURIComponent(revision)}`);
        if (!view.request || !["queued", "running"].includes(view.request.state)) { clearInterval(timer); location.reload(); }
      } catch (error) { /* keep the page usable; try again on the next tick */ }
    }, 15000);
  }

  function commentForm(signedIn, revision) {
    const form = $("work-comment"), guest = $("work-comment-guest"), state = $("work-comment-state");
    if (!form) return;
    form.hidden = !signedIn; guest.hidden = signedIn;
    const link = guest.querySelector("a"); if (link) link.href = `/${lang()}/account`;
    const replying = $("work-comment-replying"), target = $("work-comment-reply-target"), cancel = $("work-comment-cancel-reply");
    let replyTo = null;
    const clearReply = () => {
      replyTo = null;
      form.dataset.replyTo = "";
      if (target) target.textContent = "";
      if (replying) replying.hidden = true;
      if (guest && guest.before) guest.before(form, state);
    };
    if (cancel) cancel.addEventListener("click", clearReply);
    for (const btn of document.querySelectorAll(".comment-reply-btn")) {
      btn.hidden = !signedIn;
      btn.addEventListener("click", () => {
        replyTo = btn.dataset.commentId || null;
        form.dataset.replyTo = replyTo || "";
        if (target) target.textContent = tr("正在回复 {who}", {who: btn.dataset.commentAuthor || ""});
        if (replying) replying.hidden = !replyTo;
        const card = $(`c-${replyTo}`) || btn.closest?.("article");
        if (card && card.after) card.after(form, state);
        form.elements.body?.focus?.();
      });
    }
    form.addEventListener("submit", async event => {
      event.preventDefault();
      const button = form.querySelector('button[type="submit"]') || form.querySelector("button");
      if (button) button.disabled = true;
      try {
        await call("/api/comments", {revision_id: revision, body: form.elements.body.value, reply_to: replyTo});
        location.reload();
      } catch (error) { state.textContent = error.message; if (button) button.disabled = false; }
    });
  }

  function findingForms(signedIn) {
    const guest = $("work-analysis-guest");
    if (guest) guest.hidden = signedIn;
    for (const box of document.querySelectorAll(".finding-reply")) {
      box.hidden = !signedIn;
      const form = box.querySelector(".finding-response-form"), state = box.querySelector(".finding-reply-state");
      if (!form) continue;
      form.addEventListener("submit", async event => {
        event.preventDefault();
        const button = form.querySelector("button"); button.disabled = true;
        try {
          await call("/api/analysis/respond", {
            report_id: form.dataset.report, finding_id: form.dataset.finding,
            stance: form.elements.stance.value, body: form.elements.body.value,
            revision_ref: form.elements.revision_ref?.value || null
          });
          location.reload();
        } catch (error) { state.textContent = error.message; button.disabled = false; }
      });
    }
  }

  function bindAppealForm(form) {
    if (!form || form.dataset.bound) return;
    form.dataset.bound = "1";
    const state = $("work-label-appeal-state") || form.querySelector("#work-label-appeal-state");
    form.addEventListener("submit", async event => {
      event.preventDefault();
      const button = form.querySelector("button");
      if (button) button.disabled = true;
      try {
        await call("/api/community/label-appeal", {
          label_id: form.dataset.labelId || undefined,
          exploration_id: form.dataset.exploration || undefined,
          body: form.elements.body.value
        });
        location.reload();
      } catch (error) {
        if (state) state.textContent = error.message;
        if (button) button.disabled = false;
      }
    });
  }

  async function authorLabelAppeal(workId) {
    const appealBox = $("work-label-appeal");
    if (!appealBox || !workId) return;
    const existingForm = $("work-label-appeal-form");
    if (existingForm) { bindAppealForm(existingForm); appealBox.hidden = false; return; }
    let details = null;
    try { details = await call(`/api/community/details?exploration_id=${encodeURIComponent(workId)}`); } catch (error) { return; }
    const status = details?.label_appeal;
    if (!status || (!status.can_appeal && !status.appeal)) return;
    appealBox.textContent = "";
    const h3 = document.createElement("h3");
    h3.textContent = tr("作者申诉");
    appealBox.appendChild(h3);
    if (status.can_appeal && status.active_label) {
      const p = document.createElement("p");
      p.className = "muted";
      p.textContent = tr("如果你认为此标记有误，可以提交一次申诉（不超过 2000 字）。申诉内容私密，仅作者本人与离线维护者可见。");
      appealBox.appendChild(p);
      const form = document.createElement("form");
      form.method = "post";
      form.id = "work-label-appeal-form";
      form.dataset.labelId = status.active_label.id;
      form.dataset.exploration = workId;
      const label = document.createElement("label");
      label.textContent = tr("申诉说明（最多 2000 字）");
      const textarea = document.createElement("textarea");
      textarea.name = "body";
      textarea.rows = 4;
      textarea.maxLength = 2000;
      textarea.required = true;
      textarea.placeholder = tr("请说明这篇作品并非商业推广或需要复核的具体理由。");
      label.appendChild(textarea);
      form.appendChild(label);
      const btn = document.createElement("button");
      btn.type = "submit";
      btn.textContent = tr("提交申诉");
      form.appendChild(btn);
      const st = document.createElement("p");
      st.id = "work-label-appeal-state";
      st.className = "muted";
      st.setAttribute("role", "status");
      form.appendChild(st);
      appealBox.appendChild(form);
      bindAppealForm(form);
      appealBox.hidden = false;
    } else if (status.appeal) {
      const ap = status.appeal;
      const pStatus = document.createElement("p");
      const strong = document.createElement("strong");
      if (ap.state === "pending") {
        strong.textContent = tr("申诉状态：已提交，待维护者离线复核。");
      } else if (ap.decision === "retract") {
        strong.textContent = tr("申诉处理结果：已撤回标记，作品已恢复正常分发。");
      } else {
        strong.textContent = tr("申诉处理结果：维持原标记。");
      }
      pStatus.appendChild(strong);
      appealBox.appendChild(pStatus);
      if (ap.decision_reason) {
        const pReason = document.createElement("p");
        pReason.textContent = tr("复核说明：{reason}", {reason: ap.decision_reason});
        appealBox.appendChild(pReason);
      }
      const pBody = document.createElement("p");
      pBody.className = "muted";
      pBody.textContent = tr("你的申诉内容：{body}", {body: ap.body});
      appealBox.appendChild(pBody);
      appealBox.hidden = false;
    }
  }

  async function authorActions() {
    const box = $("author-actions");
    if (!box) return;
    let status = null;
    try { status = await call("/api/auth/status"); } catch (error) { /* treat as signed out */ }
    const signedIn = Boolean(status?.actor);
    commentForm(signedIn, box.dataset.revision);
    findingForms(signedIn);
    const justPublished = location.hash === "#published";
    if (justPublished) history.replaceState(null, "", location.pathname + location.search);
    if (!signedIn || status.actor.id !== box.dataset.author) return;
    box.hidden = false;
    $("published-banner").hidden = !justPublished;
    if (justPublished) box.scrollIntoView({block: "center"});
    const workId = box.dataset.work || $("work-label-appeal")?.dataset.exploration;
    await authorLabelAppeal(workId);
    const button = $("author-request-analysis"), state = $("author-action-state");
    let view = null;
    try { view = await call(`/api/analysis?revision_id=${encodeURIComponent(box.dataset.revision)}`); } catch (error) { /* optional */ }
    const busy = view?.request && ["queued", "running"].includes(view.request.state);
    const platformDone = view?.reports?.some(r => r.analyzer.cost_borne_by === "Eksmu operator");
    button.hidden = !view || busy || platformDone;
    if (view && !view.service.active) state.textContent = tr("分析服务目前未运行；请求会保留，服务启用后按顺序处理。");
    button.addEventListener("click", async () => {
      const model = view?.service?.model || "", vendor = /^claude-/.test(model) ? "Anthropic" : /^gpt-/.test(model) ? "OpenAI" : /^deepseek-/.test(model) ? "DeepSeek" : "";
      const question = model && vendor ? tr("这会把本版标题、摘要、正文，以及附件里的论文全文（LaTeX 源码包或本站编译的 HTML 版）发送给 {vendor} 的模型 {model} 进行分析，报告将公开显示在作品旁。继续吗？", {vendor, model})
        : tr("这会把本版标题、摘要、正文，以及附件里的论文全文（LaTeX 源码包或本站编译的 HTML 版）发送给外部 AI 模型服务进行分析，报告将公开显示在作品旁。继续吗？");
      if (!confirm(question)) return;
      button.disabled = true;
      try {
        await call("/api/analysis/request", {revision_id: box.dataset.revision});
        location.reload();
      } catch (error) { state.textContent = error.message; button.disabled = false; }
    });
  }

  function selectText(el) {
    if (!el) return;
    if (typeof el.select === "function") { el.select(); return; }
    const sel = window.getSelection?.();
    if (sel && document.createRange) {
      const range = document.createRange();
      range.selectNodeContents(el);
      sel.removeAllRanges();
      sel.addRange(range);
    }
  }

  function creditShare() {
    const button = $("copy-credit-share"), state = $("credit-share-state");
    if (button && state) {
      button.addEventListener("click", async () => {
        const text = button.dataset.share || "";
        state.hidden = false;
        try {
          await navigator.clipboard.writeText(text);
          state.textContent = tr("已复制时间存证分享文案（证明此版本不晚于某时存在），可直接粘贴到社交平台或学术群。");
        } catch (error) {
          state.textContent = text;
        }
      });
    }
    const badgeBtn = $("copy-credit-badge"), badgeState = $("credit-badge-state");
    if (badgeBtn && badgeState) {
      badgeBtn.addEventListener("click", async () => {
        const text = badgeBtn.dataset.badge || "";
        badgeState.hidden = false;
        try {
          await navigator.clipboard.writeText(text);
          badgeState.textContent = tr("已复制 Markdown 存证徽章代码，可直接粘贴到 GitHub README 或个人主页。");
        } catch (error) {
          badgeState.textContent = text;
        }
      });
    }
    const citeState = $("cite-copy-state");
    const bindCiteCopy = (btnId, preId, okMsg, failMsg) => {
      const btn = $(btnId), pre = $(preId);
      if (!btn || !pre || !citeState) return;
      btn.addEventListener("click", async () => {
        const text = pre.textContent || "";
        citeState.hidden = false;
        try {
          await navigator.clipboard.writeText(text);
          citeState.textContent = okMsg();
        } catch (error) {
          selectText(pre);
          citeState.textContent = failMsg();
        }
      });
    };
    bindCiteCopy(
      "copy-citation-text",
      "citation-text",
      () => tr("已复制纯文本引用。"),
      () => tr("未能自动访问剪贴板；已选中纯文本引用，请按 Ctrl+C 或 Cmd+C 复制。")
    );
    bindCiteCopy(
      "copy-citation-bibtex",
      "citation-bibtex",
      () => tr("已复制 BibTeX 引用。"),
      () => tr("未能自动访问剪贴板；已选中 BibTeX 引用，请按 Ctrl+C 或 Cmd+C 复制。")
    );
  }

  // Highlight the in-page nav link of the section currently at the top of the viewport.
  function sectionNav() {
    const nav = $("work-toc");
    if (!nav || !("IntersectionObserver" in window)) return;
    const links = [...nav.querySelectorAll("a[data-target]")];
    const targets = links.map(a => $(a.dataset.target)).filter(Boolean);
    const visible = new Set();
    const mark = () => {
      // A short last section may never reach the band; at the very bottom it is the current one.
      const atBottom = window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 2;
      const current = atBottom ? targets[targets.length - 1] : (targets.find(t => visible.has(t)) || null);
      for (const a of links) {
        const on = current !== null && a.dataset.target === current.id;
        a.classList.toggle("on", on);
        if (on) a.setAttribute("aria-current", "true"); else a.removeAttribute("aria-current");
      }
    };
    const observer = new IntersectionObserver(entries => {
      for (const e of entries) { if (e.isIntersecting) visible.add(e.target); else visible.delete(e.target); }
      mark();
    }, {rootMargin: "-120px 0px -55% 0px"});
    targets.forEach(t => observer.observe(t));
    window.addEventListener("scroll", () => { if (window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 2) mark(); }, {passive: true});
  }

  document.addEventListener("DOMContentLoaded", () => { creditShare(); authorActions(); watchAnalysis(); sectionNav(); });
})();
