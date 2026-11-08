"use strict";
// Optional paper fields belong to a specific immutable text revision.
(() => {
  const editors = new Map();
  function editor(id) {
    const host = $(id + "-paper"), details = node("details"), toggle = node("input"), fields = node("fieldset");
    toggle.type = "checkbox"; toggle.id = id + "-paper-enabled";
    const caption = node("label", tr("为本版保存论文资料（可选）")); caption.prepend(toggle);
    details.append(node("summary", tr("论文资料：摘要、署名与参考文献")), caption);
    fields.hidden = true; fields.disabled = true;
    const controls = {};
    function field(key, caption, limit, type = "textarea") {
      const label = node("label", caption), input = node(type === "date" ? "input" : type);
      input.id = id + "-paper-" + key; input.maxLength = limit;
      if (type === "date") input.type = "date";
      if (type === "textarea") input.rows = 3;
      label.append(input); fields.append(label); controls[key] = input; return input;
    }
    field("abstract", tr("摘要（可留空）"), 6000);
    const authors = node("div"), add = node("button", tr("添加一位作者"), "secondary"); add.type = "button";
    add.id = id + "-paper-add-author";
    fields.append(node("p", tr("署名与贡献（可留空，最多32位；不会改变账户权限）"), "muted"), authors, add);
    function changed() { $(id).dispatchEvent(new Event("input", {bubbles:true})); }
    function addAuthor(value = {name:"", contribution:""}) {
      if (authors.children.length >= 32) return tell(tr("最多列出32位作者。"));
      const row = node("div", undefined, "card");
      const name = node("input"), contribution = node("textarea"), remove = node("button", tr("移除此行"), "secondary");
      name.maxLength = 160; name.value = value.name; name.dataset.paperField = "name";
      contribution.maxLength = 2000; contribution.rows = 2; contribution.value = value.contribution;
      contribution.dataset.paperField = "contribution";
      const a = node("label", tr("署名（姓名或化名）")), b = node("label", tr("这位作者的贡献说明"));
      a.append(name); b.append(contribution); remove.type = "button";
      remove.addEventListener("click", () => { row.remove(); changed(); });
      row.append(a, b, remove); authors.append(row);
    }
    add.addEventListener("click", () => { addAuthor(); changed(); });
    field("references", tr("参考文献（每行一条，最多100条）"), 48000);
    field("license", tr("许可声明（可留空；填写许可名称、版本、链接或具体条款）"), 4000);
    fields.append(node("p", tr("空白表示未声明许可，不会自动授权他人复用。请填写你有权作出的声明。"), "muted"));
    field("source", tr("既往来源（旧稿地址或出处，可留空）"), 2000);
    field("prior_publication_date", tr("既往发表日期（作者自述，可留空；填写时请同时说明来源）"), 10, "date");
    fields.append(node("p", tr("这里的日期不是外部核验的时间证据。本站另行记录本版保存时间。"), "muted"));
    toggle.addEventListener("change", () => { fields.hidden = !toggle.checked; fields.disabled = !toggle.checked; });
    details.append(fields); host.append(details);
    editors.set(id, {details, toggle, fields, controls, authors, addAuthor});
  }
  function read(id) {
    const e = editors.get(id); if (!e.toggle.checked) return null;
    return {format:"eksmu.paper-metadata.v1", abstract:e.controls.abstract.value,
      authors:Array.from(e.authors.children, row => ({name:row.querySelector('[data-paper-field="name"]').value,
        contribution:row.querySelector('[data-paper-field="contribution"]').value})),
      references:e.controls.references.value.split(/\r?\n/).filter(s=>s.trim()),
      license:e.controls.license.value, provenance:{source:e.controls.source.value,
        prior_publication_date:e.controls.prior_publication_date.value}};
  }
  function fill(id, paper) {
    const e = editors.get(id); e.toggle.checked = !!paper; e.fields.hidden = !paper; e.fields.disabled = !paper;
    if (e.details) e.details.open = !!paper;
    e.authors.replaceChildren();
    for (const author of paper?.authors || []) e.addAuthor(author);
    for (const name of ["abstract", "license"]) e.controls[name].value = paper?.[name] || "";
    e.controls.references.value = (paper?.references || []).join("\n");
    for (const name of ["source", "prior_publication_date"]) e.controls[name].value = paper?.provenance?.[name] || "";
  }
  let currentPaper = null;
  function render(paper, renderedAbstract = null) {
    currentPaper = paper || null;
    const target = $("paper-metadata"); target.replaceChildren(); target.hidden = !paper;
    if (!paper) return;
    target.append(node("h3", tr("论文资料 · 本版作者声明")));
    function section(title, value) { target.append(node("h4", title), node("pre", value)); }
    target.append(node("h4", tr("摘要")));
    if (paper.abstract && renderedAbstract && renderedAbstract.renderer === "eksmu.markdown-mathml.v1" && renderedAbstract.html) {
      const box = node("div", undefined, "work-abstract");
      const tpl = document.createElement("template");
      tpl.innerHTML = renderedAbstract.html;
      box.append(tpl.content.cloneNode(true));
      target.append(box);
    } else {
      target.append(node("pre", paper.abstract || tr("未填写")));
    }
    const authors = node("ol");
    for (const author of paper.authors) authors.append(node("li", tr("{name} — {contribution}", {name: author.name, contribution: author.contribution || tr("未填写贡献说明")})));
    target.append(node("h4", tr("署名与贡献")), paper.authors.length ? authors : node("p", tr("未列出；提交账户见上方。")));
    section(tr("许可声明"), paper.license || tr("未声明许可"));
    const refs = node("ol"); for (const ref of paper.references) refs.append(node("li", ref));
    target.append(node("h4", tr("参考文献")), paper.references.length ? refs : node("p", tr("未列出")));
    section(tr("既往来源"), paper.provenance.source || tr("未提供"));
    if (paper.provenance.prior_publication_date) section(tr("既往发表日期（作者自述）"), paper.provenance.prior_publication_date);
    target.append(node("p", tr("这些资料随所选正文版本保存；修改需要提交新版本。署名、许可与既往日期未经本站独立核验。"), "muted"));
  }
  editor("publish"); editor("revise");
  window.EksmuPapers = {read, fill};
  document.addEventListener("eksmu:revision", event => render(event.detail.revision.payload.paper));
  document.addEventListener("eksmu:research-loaded", event => {
    if (currentPaper && event.detail?.rendered_abstract) render(currentPaper, event.detail.rendered_abstract);
  });
})();
