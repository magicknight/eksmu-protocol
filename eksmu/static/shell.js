/* Eksmu Night Signal HUD — Shell (Command Palette, FX/LITE, Shortcuts, Reading Paper Mode) */
"use strict";
(()=>{
  const isMac = /Mac|iPhone|iPad/i.test(navigator.platform || navigator.userAgent || "");
  const modKey = isMac ? "⌘" : "Ctrl";

  const getLang = () => {
    if (window.EKSMU_LANG) return window.EKSMU_LANG;
    const path = location.pathname;
    if (path.startsWith("/en/") || path === "/en") return "en";
    const docLang = document.documentElement.lang;
    return docLang && docLang.startsWith("en") ? "en" : "zh";
  };

  const T = (zh, en) => getLang() === "en" ? en : zh;

  /* ---- FX / LITE Mode ---- */
  const initFx = () => {
    const prefersReduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const isSaveData = Boolean(navigator.connection && navigator.connection.saveData);
    let stored = null;
    try { stored = localStorage.getItem("eksmu_fx"); } catch (_) {}

    const isLite = prefersReduced || stored === "lite" || (!stored && isSaveData);
    if (isLite) {
      document.documentElement.classList.add("fx-lite");
    } else {
      document.documentElement.classList.remove("fx-lite");
    }
    syncFxButtons();
  };

  const setFx = (lite) => {
    const prefersReduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const actualLite = prefersReduced ? true : lite;
    if (actualLite) {
      document.documentElement.classList.add("fx-lite");
    } else {
      document.documentElement.classList.remove("fx-lite");
    }
    try { localStorage.setItem("eksmu_fx", actualLite ? "lite" : "fx"); } catch (_) {}
    syncFxButtons();
  };

  const toggleFx = () => {
    const currentLite = document.documentElement.classList.contains("fx-lite");
    setFx(!currentLite);
  };

  const syncFxButtons = () => {
    const isLite = document.documentElement.classList.contains("fx-lite");
    for (const btn of document.querySelectorAll(".tool.fx, #fx-toggle")) {
      btn.setAttribute("aria-pressed", isLite ? "false" : "true");
      btn.classList.toggle("lite", isLite);
      const dot = btn.querySelector(".dot");
      if (dot) {
        dot.className = "dot " + (isLite ? "off" : "hot");
      }
      const txt = btn.querySelector(".fx-txt");
      if (txt) {
        txt.textContent = isLite ? "LITE" : "FX";
      }
    }
  };

  /* ---- Reading Paper Mode ---- */
  const initPaperMode = () => {
    let paper = false;
    try { paper = localStorage.getItem("eksmu_reading_paper") === "1"; } catch (_) {}
    applyPaperMode(paper);
  };

  const applyPaperMode = (paper) => {
    for (const el of document.querySelectorAll(".work-article, .detail-panel, #detail")) {
      el.classList.toggle("paper-mode", paper);
    }
    for (const btn of document.querySelectorAll(".paper-toggle, #paper-toggle")) {
      btn.setAttribute("aria-pressed", paper ? "true" : "false");
      btn.classList.toggle("on", paper);
    }
    try { localStorage.setItem("eksmu_reading_paper", paper ? "1" : "0"); } catch (_) {}
  };

  const togglePaperMode = () => {
    const el = document.querySelector(".work-article, .detail-panel, #detail");
    const isPaper = el ? el.classList.contains("paper-mode") : false;
    applyPaperMode(!isPaper);
  };

  /* ---- Build Command Palette & Shortcuts Sheet DOM ---- */
  let cmdOpen = false;
  let keysOpen = false;
  let selIndex = 0;
  let items = [];
  let searchTimer = null;

  const getPages = () => {
    const lang = getLang();
    const p = (path) => `/${lang}${path === "/" ? "/" : path}`;
    return [
      { id: "p-discover", kind: "cmd", label: T("发现", "Discover"), hint: p("/"), icon: "01", run: () => location.href = p("/") },
      { id: "p-publish", kind: "cmd", label: T("发表", "Publish"), hint: p("/publish"), icon: "02", run: () => location.href = p("/publish") },
      { id: "p-activity", kind: "cmd", label: T("动态", "Activity"), hint: p("/activity"), icon: "03", run: () => location.href = p("/activity") },
      { id: "p-part", kind: "cmd", label: T("参与说明", "Participation Guide"), hint: p("/participation.html"), icon: "ℹ", run: () => location.href = p("/participation.html") },
      { id: "p-found", kind: "cmd", label: T("创立期活动", "Founding Campaign"), hint: p("/founding.html"), icon: "★", run: () => location.href = p("/founding.html") },
      { id: "p-open", kind: "cmd", label: T("开放核验", "Open Verification"), hint: p("/openness.html"), icon: "✓", run: () => location.href = p("/openness.html") },
      { id: "p-acc", kind: "cmd", label: T("账户设置", "Account Settings"), hint: p("/account"), icon: "⚙", run: () => location.href = p("/account") },
      { id: "c-fx", kind: "cmd", label: T("切换视觉效果 (FX / LITE)", "Toggle Visual Effects (FX / LITE)"), hint: "FX / LITE", icon: "✦", run: () => toggleFx() },
      { id: "c-paper", kind: "cmd", label: T("切换阅读区纸面模式", "Toggle Reading Paper Canvas"), hint: "Paper", icon: "📄", run: () => togglePaperMode() },
      {
        id: "c-lang",
        kind: "cmd",
        label: lang === "zh" ? "Switch language to English" : "切换界面语言为中文",
        hint: lang === "zh" ? "en" : "zh",
        icon: "◎",
        run: () => {
          const targetLang = lang === "zh" ? "en" : "zh";
          const switchLink = document.querySelector("a.lang-switch");
          if (switchLink) {
            switchLink.click();
          } else {
            const currentPath = location.pathname.replace(/^\/(zh|en)/, "");
            location.href = `/${targetLang}${currentPath || "/"}${location.search}${location.hash}`;
          }
        }
      },
      { id: "c-keys", kind: "cmd", label: T("查看快捷键说明", "Show Keyboard Shortcuts"), hint: "?", icon: "⌨", run: () => openKeys() }
    ];
  };

  const ensureDOM = () => {
    if (document.getElementById("cmd-dialog")) return;

    const cmdDialog = document.createElement("div");
    cmdDialog.id = "cmd-dialog";
    cmdDialog.className = "cp-scrim";
    cmdDialog.hidden = true;
    cmdDialog.innerHTML = `
      <div class="cp" role="dialog" aria-modal="true" aria-label="${T("命令与搜索", "Command & Search")}">
        <div class="cp-field">
          <span class="gt mono" aria-hidden="true">&gt;</span>
          <input id="cp-input" type="text" class="cp-in" placeholder="${T("搜索作品、页面或命令…", "Search works, pages or commands…")}" autocomplete="off" spellcheck="false" role="combobox" aria-expanded="true" aria-controls="cp-list">
          <span id="cp-spin" class="mx-spin" style="display:none" aria-hidden="true"></span>
          <button type="button" class="kbd esc" id="cp-close" aria-label="${T("关闭", "Close")}">Esc</button>
        </div>
        <ul id="cp-list" class="cp-list" role="listbox"></ul>
        <p class="cp-foot mono">${T("↑↓ 选择 · Enter 打开 · Esc 关闭", "↑↓ navigate · Enter open · Esc close")}</p>
      </div>
    `;
    document.body.appendChild(cmdDialog);

    const keysDialog = document.createElement("div");
    keysDialog.id = "keys-dialog";
    keysDialog.className = "ks-scrim";
    keysDialog.hidden = true;
    keysDialog.innerHTML = `
      <div class="ks" role="dialog" aria-modal="true" aria-label="${T("快捷键说明", "Keyboard Shortcuts")}">
        <header class="ks-head">
          <span class="ks-code mono">KEYS</span>
          <h2 class="ks-t">${T("快捷键说明", "Keyboard Shortcuts")}</h2>
          <button type="button" class="icon-btn" id="keys-close" aria-label="${T("关闭", "Close")}">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round"/></svg>
          </button>
        </header>
        <dl class="ks-list">
          <div class="ks-row"><dt><span class="kbd">${modKey}</span><span class="kbd">K</span><span class="or">/</span><span class="kbd">/</span></dt><dd>${T("打开命令面板", "Open Command Palette")}</dd></div>
          <div class="ks-row"><dt><span class="kbd">?</span></dt><dd>${T("快捷键说明", "Show shortcuts sheet")}</dd></div>
          <div class="ks-row"><dt><span class="kbd">Esc</span></dt><dd>${T("关闭当前弹窗", "Close dialog")}</dd></div>
        </dl>
      </div>
    `;
    document.body.appendChild(keysDialog);

    // Dialog event listeners
    cmdDialog.addEventListener("click", (e) => {
      if (e.target === cmdDialog) closeCmd();
    });
    keysDialog.addEventListener("click", (e) => {
      if (e.target === keysDialog) closeKeys();
    });

    document.getElementById("cp-close").addEventListener("click", closeCmd);
    document.getElementById("keys-close").addEventListener("click", closeKeys);

    const cpIn = document.getElementById("cp-input");
    cpIn.addEventListener("input", () => onInput(cpIn.value));
    cpIn.addEventListener("keydown", onInputKey);
  };

  const openCmd = (query = "") => {
    ensureDOM();
    closeKeys();
    const dialog = document.getElementById("cmd-dialog");
    const input = document.getElementById("cp-input");
    dialog.hidden = false;
    cmdOpen = true;
    input.value = query;
    onInput(query);
    setTimeout(() => { input.focus(); input.select(); }, 20);
  };

  const closeCmd = () => {
    const dialog = document.getElementById("cmd-dialog");
    if (dialog) dialog.hidden = true;
    cmdOpen = false;
    clearTimeout(searchTimer);
  };

  const openKeys = () => {
    ensureDOM();
    closeCmd();
    const dialog = document.getElementById("keys-dialog");
    dialog.hidden = false;
    keysOpen = true;
  };

  const closeKeys = () => {
    const dialog = document.getElementById("keys-dialog");
    if (dialog) dialog.hidden = true;
    keysOpen = false;
  };

  const onInput = (q) => {
    const val = q.trim().toLowerCase();
    const pages = getPages();
    let localMatches = [];
    if (!val) {
      localMatches = pages;
    } else {
      localMatches = pages.filter(p => p.label.toLowerCase().includes(val) || (p.hint && p.hint.toLowerCase().includes(val)));
    }

    items = [...localMatches];
    selIndex = 0;
    renderList(items, false);

    clearTimeout(searchTimer);
    const spin = document.getElementById("cp-spin");
    if (!val || val.length < 2) {
      if (spin) spin.style.display = "none";
      return;
    }

    if (spin) spin.style.display = "inline-block";
    searchTimer = setTimeout(async () => {
      try {
        const lang = getLang();
        const res = await fetch(`/api/community/feed?q=${encodeURIComponent(val)}`);
        if (res.ok) {
          const data = await res.json();
          const works = (data.items || []).slice(0, 8).map(w => ({
            id: `work-${w.id}`,
            kind: "work",
            label: w.title || T("未命名作品", "Untitled work"),
            hint: (w.topics && w.topics.length ? "#" + w.topics.join(" #") : "") || w.author || "",
            icon: "📄",
            run: () => {
              location.href = `/${lang}/w/${w.id}`;
            }
          }));
          items = [...localMatches, ...works];
          renderList(items, false);
        }
      } catch (_) {
        // quiet failure
      } finally {
        if (spin) spin.style.display = "none";
      }
    }, 180);
  };

  const renderList = (list, keepSelection) => {
    const ul = document.getElementById("cp-list");
    if (!ul) return;
    ul.innerHTML = "";
    if (!list.length) {
      const empty = document.createElement("li");
      empty.className = "none";
      empty.textContent = T("无匹配命令或作品", "No matching commands or works");
      ul.appendChild(empty);
      return;
    }

    let lastKind = null;
    list.forEach((item, idx) => {
      if (item.kind !== lastKind) {
        lastKind = item.kind;
        const grp = document.createElement("li");
        grp.className = "grp";
        grp.setAttribute("role", "presentation");
        grp.textContent = item.kind === "work" ? T("作品", "Works") : T("命令与页面", "Commands & Pages");
        ul.appendChild(grp);
      }

      const li = document.createElement("li");
      li.id = "cp-item-" + idx;
      li.className = "it" + (idx === selIndex ? " sel" : "");
      li.setAttribute("role", "option");
      li.setAttribute("aria-selected", idx === selIndex ? "true" : "false");
      li.innerHTML = `
        <span class="ico mono" aria-hidden="true">${item.icon || "→"}</span>
        <span class="lb">${escapeHTML(item.label)}</span>
        ${item.hint ? `<span class="hint mono">${escapeHTML(item.hint)}</span>` : ""}
      `;
      li.addEventListener("click", () => {
        closeCmd();
        item.run();
      });
      li.addEventListener("mousemove", () => {
        if (selIndex !== idx) {
          selIndex = idx;
          updateSelection();
        }
      });
      ul.appendChild(li);
    });

    if (keepSelection) {
      scrollSel();
    }
  };

  const updateSelection = () => {
    const ul = document.getElementById("cp-list");
    if (!ul) return;
    const options = ul.querySelectorAll("li.it");
    options.forEach((opt, idx) => {
      const isSel = idx === selIndex;
      opt.classList.toggle("sel", isSel);
      opt.setAttribute("aria-selected", isSel ? "true" : "false");
    });
    scrollSel();
  };

  const scrollSel = () => {
    const ul = document.getElementById("cp-list");
    const selEl = ul ? ul.querySelector("li.it.sel") : null;
    if (selEl) {
      selEl.scrollIntoView({ block: "nearest" });
    }
  };

  const onInputKey = (e) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (items.length) {
        selIndex = (selIndex + 1) % items.length;
        updateSelection();
      }
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      if (items.length) {
        selIndex = (selIndex - 1 + items.length) % items.length;
        updateSelection();
      }
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (items[selIndex]) {
        closeCmd();
        items[selIndex].run();
      }
    } else if (e.key === "Escape") {
      e.preventDefault();
      closeCmd();
    }
  };

  const escapeHTML = (str) => {
    return String(str || "").replace(/[&<>"']/g, (m) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[m]));
  };

  /* ---- Global Keyboard listener ---- */
  window.addEventListener("keydown", (e) => {
    const target = e.target;
    const isField = target && (
      target.tagName === "INPUT" ||
      target.tagName === "TEXTAREA" ||
      target.tagName === "SELECT" ||
      target.isContentEditable
    );

    if (cmdOpen || keysOpen) {
      if (e.key === "Escape") {
        closeCmd();
        closeKeys();
        return;
      }
    }

    if (isField) return;

    if ((e.metaKey || e.ctrlKey) && (e.key === "k" || e.key === "K")) {
      e.preventDefault();
      openCmd();
    } else if (e.key === "/") {
      e.preventDefault();
      openCmd();
    } else if (e.key === "?") {
      e.preventDefault();
      openKeys();
    }
  });

  /* ---- Wire buttons on page load ---- */
  const setupHooks = () => {
    initFx();
    initPaperMode();

    for (const btn of document.querySelectorAll(".cmd, #cmd-trigger")) {
      btn.addEventListener("click", () => openCmd());
    }

    for (const btn of document.querySelectorAll(".tool.fx, #fx-toggle")) {
      btn.addEventListener("click", () => toggleFx());
    }

    for (const btn of document.querySelectorAll("#rail-keys-btn, .keys-btn")) {
      btn.addEventListener("click", () => openKeys());
    }

    for (const btn of document.querySelectorAll(".paper-toggle, #paper-toggle")) {
      btn.addEventListener("click", () => togglePaperMode());
    }
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", setupHooks);
  } else {
    setupHooks();
  }

  // Expose API on window for scripts/console
  window.EksmuShell = {
    openCmd,
    closeCmd,
    openKeys,
    closeKeys,
    setFx,
    toggleFx,
    togglePaperMode
  };
})();
