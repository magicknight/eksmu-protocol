/* Eksmu theme — loaded synchronously in <head> so the page paints in the right
   palette. Preference: "system" (default, follows prefers-color-scheme), "light"
   or "dark"; stored per browser in localStorage("eksmu_theme"). Resolved theme
   goes to html[data-theme], the preference to html[data-theme-pref]. */
"use strict";
(()=>{
  const KEY = "eksmu_theme";
  const ORDER = ["system", "light", "dark"];
  const root = document.documentElement;
  const mq = window.matchMedia ? window.matchMedia("(prefers-color-scheme: light)") : null;
  let pref = "system";
  try { const saved = localStorage.getItem(KEY); if (ORDER.includes(saved)) pref = saved; } catch (_) {}

  const en = () => (root.lang || "").startsWith("en");
  const LABEL = {
    system: ["主题：跟随系统", "Theme: follow system", "AUTO"],
    light: ["主题：浅色", "Theme: light", "LIGHT"],
    dark: ["主题：深色", "Theme: dark", "DARK"],
  };

  const sync = () => {
    for (const btn of document.querySelectorAll("#theme-toggle, .tool.theme")) {
      const [zh, english, short] = LABEL[pref];
      const label = en() ? english : zh;
      btn.setAttribute("aria-label", label);
      btn.title = label + (en() ? " (click to switch)" : "（点击切换）");
      const txt = btn.querySelector(".theme-txt");
      if (txt) txt.textContent = short;
    }
  };

  const apply = () => {
    const theme = pref === "system" ? (mq && mq.matches ? "light" : "dark") : pref;
    root.dataset.theme = theme;
    root.dataset.themePref = pref;
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", theme === "light" ? "#F5F6F9" : "#07080D");
    sync();
  };

  const cycle = () => {
    pref = ORDER[(ORDER.indexOf(pref) + 1) % ORDER.length];
    try { if (pref === "system") localStorage.removeItem(KEY); else localStorage.setItem(KEY, pref); } catch (_) {}
    apply();
  };

  apply();
  if (mq) {
    const onChange = () => { if (pref === "system") apply(); };
    if (mq.addEventListener) mq.addEventListener("change", onChange); else if (mq.addListener) mq.addListener(onChange);
  }
  window.addEventListener("storage", (e) => {
    if (e.key === KEY) { pref = ORDER.includes(e.newValue) ? e.newValue : "system"; apply(); }
  });
  const wire = () => {
    for (const btn of document.querySelectorAll("#theme-toggle, .tool.theme")) btn.addEventListener("click", cycle);
    sync();
  };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", wire); else wire();
  window.EksmuTheme = { cycle, get: () => pref };
})();
