"use strict";
// AI public analysis (RFC 0031): per-finding, version-bound, rebuttable. Never a score, never a gate.
(() => {
  let context = null, epoch = 0, service = null;
  const vendor = model => /^claude-/.test(model) ? "Anthropic" : /^gpt-/.test(model) ? "OpenAI" : /^deepseek-/.test(model) ? "DeepSeek" : "";
  const severity = {blocking:tr("若成立则主要结论不成立"),major:tr("重要"),minor:tr("次要"),note:tr("建议")};
  const category = {derivation_gap:tr("推导缺口"),unsupported_claim:tr("缺少支持的论断"),internal_inconsistency:tr("前后不一致"),
    missing_definition:tr("缺少定义"),known_result:tr("已有结果"),citation_issue:tr("引用问题"),reproduction:tr("复现"),
    formal_check:tr("形式化检查"),clarity:tr("表述不清"),other:tr("其他")};
  const confidence = {low:tr("低"),medium:tr("中"),high:tr("高")};
  const stances = {accept:tr("接受"),dispute:tr("反驳"),revised:tr("已在新版本修改"),unclear:tr("不清楚")};
  const states = {queued:tr("已排队，通常 1–2 分钟内完成，本区会自动刷新"),running:tr("分析中，通常 1–2 分钟，本区会自动刷新"),done:tr("已完成"),failed:tr("未能完成"),declined:tr("模型拒绝分析")};
  let poll = null;

  function responseForm(report, finding) {
    const form = node("form"), stance = node("select"), body = node("textarea"), send = node("button", tr("提交回应"));
    for (const [value, name] of Object.entries(stances)) { const o = node("option", name); o.value = value; stance.append(o); }
    form.method = "post"; body.required = true; body.maxLength = 4000; body.rows = 3; body.placeholder = tr("说明理由；反驳 AI 和接受 AI 同样欢迎。");
    form.append(stance, body, send);
    form.addEventListener("submit", async e => {
      e.preventDefault(); send.disabled = true;
      try {
        requireIdentity();
        await api("/api/analysis/respond", {report_id: report.id, finding_id: finding.id, stance: stance.value, body: body.value, revision_ref: null});
        tell(tr("回应已公开记录。")); load();
      } catch (error) { tell(error.message); send.disabled = false; }
    });
    return form;
  }

  const assessment = {supported:tr("论证充分"),partially_supported:tr("部分成立，仍有缺口"),not_established:tr("尚未证成"),not_assessed:tr("未评估")};

  function renderFinding(report, f, responses, checks) {
    const item = node("section", undefined, `card finding-item finding-sev-${f.severity}`);
    const head = node("div", undefined, "finding-header");
    head.append(node("span", `${f.id} · ${severity[f.severity]} · ${category[f.category]}`, `badge sev-badge sev-${f.severity}`));
    item.append(head);
    if (f.location.section) item.append(node("p", tr("位置：{section}", {section: f.location.section}), "muted"));
    if (f.location.quote) {
      item.append(node("blockquote", f.location.quote));
      const found = checks[report.id]?.[f.id] ?? f.quote_verified;
      item.append(node("span", found ? tr("引文已核对") : tr("正文中找不到这段引文，请谨慎对待这条发现。"), `badge quote-status ${found ? "ok" : "danger"}`));
    }
    item.append(node("p", f.statement, "finding-statement"));
    for (const r of responses.filter(x => x.report_id === report.id && x.finding_id === f.id)) {
      item.append(node("p", `${label(r.actor_id)}${r.is_author ? tr("（作者）") : ""} · ${stances[r.stance]} · ${r.created_at}`, "muted"), node("pre", r.body));
    }
    const details = node("details", undefined, "finding-reply"), summary = node("summary", tr("回应这条发现"));
    details.append(summary, responseForm(report, f)); item.append(details);
    return item;
  }

  function renderReport(report, responses, box, checks) {
    const card = node("article", undefined, "hp card analysis-report"), v2 = report.format === "eksmu.analysis-report.v2";
    const heading = text => node("h4", text), list = items => { const ul = node("ul"); for (const x of items) ul.append(node("li", x)); return ul; };
    card.append(node("h4", tr("{model} · {time}", {model: report.analyzer.name, time: report.created_at})));
    card.append(node("p", report.analyzer.cost_borne_by === "Eksmu operator" ? tr("平台提供的基础分析") : tr("作者自费的深入分析（同一评判标准）"), "badge"));
    if (report.suspicious_instructions) card.append(node("p", tr("注意：正文中含有试图指挥 AI 的文字，模型已忽略并标记。"), "notice"));
    if (v2 && report.summary) card.append(heading(tr("概要")), node("p", report.summary));
    if (v2 && report.claims.length) {
      card.append(heading(tr("主要论断评估")), node("p", tr("这是模型对“正文是否已经证成”的意见，不是平台结论。"), "muted"));
      for (const c of report.claims) {
        const item = node("section", undefined, "card");
        item.append(node("h5", `${c.id} · ${assessment[c.assessment]}${c.section ? " · " + c.section : ""}`), node("p", c.claim), node("p", c.reasoning, "muted"));
        card.append(item);
      }
    }
    if (v2 && report.strengths.length) card.append(heading(tr("优点")), list(report.strengths));
    const major = report.findings.filter(f => ["blocking", "major"].includes(f.severity)), minor = report.findings.filter(f => !["blocking", "major"].includes(f.severity));
    if (major.length) { card.append(heading(tr("主要问题"))); for (const f of major) card.append(renderFinding(report, f, responses, checks)); }
    if (minor.length) { card.append(heading(tr("次要问题"))); for (const f of minor) card.append(renderFinding(report, f, responses, checks)); }
    if (!report.findings.length) card.append(node("p", tr("模型没有列出具体问题。这不等于正文已被证明正确。"), "muted"));
    if (v2 && report.questions.length) card.append(heading(tr("给作者的问题")), list(report.questions));
    if (v2 && report.suggestions.length) card.append(heading(tr("改进建议")), list(report.suggestions));
    if (v2 && report.overall) card.append(heading(tr("总体意见")), node("p", report.overall));
    const note = x => ({"attachments, data files and code were not read": tr("未读取附件、数据文件和代码"), "data files and code were not executed": tr("未运行数据文件和代码"), "PDF attachments were not read (no text extraction)": tr("PDF 附件未读取（暂不提取 PDF 文字）"), "title and full body of this revision; no attachments": tr("本版标题与全文，不含附件")})[x] || x;
    const input = i => {
      const cut = i.truncated ? tr("，已截断") : "", n = i.chars.toLocaleString();
      if (i.kind === "body") return tr("本版标题、摘要与正文") + cut;
      if (i.read && i.kind === "source") return tr("LaTeX 源码包 {name}（{n} 字符{cut}）", {name: i.filename, n, cut});
      if (i.read && i.kind === "html") return tr("本站编译的 HTML 版 {name}（{n} 字符{cut}）", {name: i.filename, n, cut});
      return tr("{name} 未读取", {name: i.filename});
    };
    const read = report.scope.inputs ? report.scope.inputs.map(input).join("；") : note(report.scope.read);
    card.append(node("p", tr("读取范围：{read}。未检查：{missing}", {read, missing: report.scope.not_checked.map(note).join("；")}), "muted"));
    card.append(node("p", tr("绑定版本摘要 {digest}… · 费用由 {payer} 承担 · 报告摘要 {sha}…", {digest: report.revision_sha256.slice(0, 12), payer: report.analyzer.cost_borne_by, sha: report.sha256.slice(0, 12)}), "muted"));
    if (report.limitations) card.append(node("p", tr("局限：{text}", {text: report.limitations}), "muted"));
    box.append(card);
  }

  async function load() {
    if (!context) return;
    const stamp = ++epoch, rev = context.revision, box = $("ai-analysis-reports"), state = $("ai-analysis-state"), ask = $("ai-analysis-request");
    box.replaceChildren(); ask.hidden = true; state.textContent = tr("正在读取 AI 分析…");
    try {
      const view = await api(`/api/analysis?revision_id=${encodeURIComponent(rev.id)}`);
      if (stamp !== epoch) return;
      const req = view.request; service = view.service;
      state.textContent = req ? tr("最近一次请求：{state}{detail}", {state: states[req.state], detail: req.detail ? ` · ${req.detail}` : ""}) : tr("此版本尚未请求 AI 分析。没有分析不影响发表和讨论。");
      if (!view.service.active) state.textContent += " " + tr("分析服务目前未运行；请求会保留，服务启用后按顺序处理。");
      const author = Boolean(context.actor_id && context.actor_id === rev.actor_id), busy = req && ["queued", "running"].includes(req.state);
      const platformDone = view.reports.some(r => r.analyzer.cost_borne_by === "Eksmu operator");
      ask.hidden = !(author && !busy && !platformDone);
      const own = $("ai-analysis-own"), select = $("ai-analysis-own-form").elements.model;
      own.hidden = !(author && !busy && view.service.author_keys);
      if (!own.hidden && select.options.length !== view.service.author_models.length) {
        select.replaceChildren();
        for (const m of view.service.author_models) { const o = node("option", `${vendor(m)} · ${m}`); o.value = m; select.append(o); }
      }
      if (req?.payer === "author") state.textContent += " " + tr("（作者自带密钥，模型 {model}）", {model: req.model});
      const reports = view.reports.slice().reverse();   // newest first
      if (reports.length) renderReport(reports[0], view.responses, box, view.quote_checks || {});
      if (reports.length > 1) {
        const older = node("details", undefined, "older-reports"), inner = node("div");
        older.append(node("summary", tr("早先的分析（{n} 份）", {n: reports.length - 1})), inner);
        for (const report of reports.slice(1)) renderReport(report, view.responses, inner, view.quote_checks || {});
        box.append(older);
      }
      clearInterval(poll); poll = null;
      if (busy) poll = setInterval(() => { if (stamp === epoch) load(); else clearInterval(poll); }, 15000);
    } catch (error) { if (stamp === epoch) state.textContent = error.message; }
  }

  document.addEventListener("eksmu:revision", e => { context = e.detail; load(); });
  document.addEventListener("DOMContentLoaded", () => {
    $("ai-analysis-own-form").addEventListener("submit", async e => {
      e.preventDefault();
      const form = e.target, button = form.querySelector("button");
      if (!context) return;
      button.disabled = true;
      try {
        requireIdentity();
        const model = form.elements.model.value;
        if (!confirm(tr("这会用你的 API 密钥调用 {vendor} 的模型 {model}，把本版标题、摘要、正文，以及附件里的论文全文（LaTeX 源码包或本站编译的 HTML 版）发送过去分析，费用记在你的账户上；报告将公开显示在作品旁。继续吗？", {vendor: vendor(model), model}))) return;
        await api("/api/analysis/request", {revision_id: context.revision.id, model, api_key: form.elements.api_key.value});
        tell(tr("已加入分析队列。完成后报告会显示在这里。")); load();
      } catch (error) { tell(error.message); }
      finally { form.elements.api_key.value = ""; button.disabled = false; }
    });
    $("ai-analysis-request").addEventListener("click", async () => {
      if (!context) return;
      try {
        requireIdentity();
        const model = service?.model || "";
        const question = model ? tr("这会把本版标题、摘要、正文，以及附件里的论文全文（LaTeX 源码包或本站编译的 HTML 版）发送给 {vendor} 的模型 {model} 进行分析，报告将公开显示在作品旁。继续吗？", {vendor: vendor(model) || tr("外部服务"), model})
          : tr("这会把本版标题、摘要、正文，以及附件里的论文全文（LaTeX 源码包或本站编译的 HTML 版）发送给外部 AI 模型服务进行分析，报告将公开显示在作品旁。继续吗？");
        if (!confirm(question)) return;
        await api("/api/analysis/request", {revision_id: context.revision.id});
        tell(tr("已加入分析队列。完成后报告会显示在这里。")); load();
      } catch (error) { tell(error.message); }
    });
  });
})();
