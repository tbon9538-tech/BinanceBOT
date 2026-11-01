/* 管理界面的交互（D-080）。同源加载，不依赖任何外部库；不开脚本时页面仍然可用。
 * 图表数据来自页面内的 <script type="application/json" id="chart-data">；标签一律用 textContent 写入。 */
(() => {
  "use strict";
  const root = document.documentElement;
  root.classList.add("js");

  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* 无痕模式等：忽略 */ } },
  };

  // ---------- 主题：自动 → 浅色 → 深色 ----------
  const THEMES = ["auto", "light", "dark"];
  const THEME_LABEL = { auto: "跟随系统", light: "浅色", dark: "深色" };
  function applyTheme(t) {
    if (t === "light" || t === "dark") root.dataset.theme = t;
    else delete root.dataset.theme;
  }
  applyTheme(store.get("dash-theme"));

  const NS = "http://www.w3.org/2000/svg";
  function el(tag, attrs, parent) {
    const n = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, v);
    if (parent) parent.appendChild(n);
    return n;
  }
  function h(tag, cls, text, parent) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    if (parent) parent.appendChild(n);
    return n;
  }

  // ---------- 图表 ----------
  const fmt = {
    navu: (v) => v.toFixed(4),
    pct: (v) => (v * 100).toFixed(1) + "%",
    usd: (v) => Math.round(v).toLocaleString("en-US"),
  };

  function niceTicks(lo, hi, n) {
    if (!(hi > lo)) { const pad = Math.abs(lo) * 0.002 || 0.001; lo -= pad; hi += pad; }
    const raw = (hi - lo) / n;
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const norm = raw / mag;
    const step = (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 3.5 ? 2.5 : norm < 7.5 ? 5 : 10) * mag;
    const a = Math.floor(lo / step) * step, b = Math.ceil(hi / step) * step;
    const ticks = [];
    for (let v = a; v <= b + step / 2; v += step) ticks.push(+v.toFixed(10));
    return { lo: a, hi: b, ticks, step };
  }

  const SPECS = {
    nav: {
      key: "nav", height: 190, label: "单位净值",
      series: [{ idx: 1, cls: "s1", dot: "s1f", key: "key-line", name: "单位净值", fmt: fmt.navu }],
      tick: (v, step) => v.toFixed(Math.max(2, Math.min(6, Math.ceil(-Math.log10(step))))),
    },
    dd: {
      key: "nav", height: 96, label: "回撤", zeroTop: true, ticksN: 2,
      series: [{ idx: 2, cls: "dd", dot: "ddf", key: "key-dd", name: "回撤", fmt: fmt.pct, area: true }],
      tick: (v, step) => (v * 100).toFixed(step * 100 < 1 ? 1 : 0) + "%",
    },
    regime: {
      key: "regime", height: 318, label: "BTC 收盘价与均线", band: 3,
      series: [
        { idx: 1, cls: "s1", dot: "s1f", key: "key-line", name: "收盘价", fmt: fmt.usd },
        { idx: 2, cls: "s2", dot: "s2f", key: "key-line s2", name: "均线", fmt: fmt.usd },
      ],
      tick: (v) => (v >= 1000 ? Math.round(v / 1000) + "K" : String(v)),
      extra: (r) => ["状态", r[3] ? "Risk-On" : "Risk-Off"],
    },
  };

  // 用不含账号信息的站点地址发请求（地址栏里带 user:pass@ 时，相对地址的 fetch 会被浏览器拒绝）
  const api = (path) => new URL(path, location.origin).toString();

  function dateLabel(iso, longSpan) { return longSpan ? iso.slice(2, 7) : iso.slice(5); }

  function render(fig, spec, rows) {
    const plot = fig.querySelector(".plot");
    const w = Math.max(240, Math.round(plot.clientWidth));
    const H = spec.height;
    const m = { l: 46, r: 10, t: 10, b: 24 };
    plot.textContent = "";
    if (!rows.length) { h("p", "empty", "这个时间范围内没有数据。", plot); return; }

    const vals = [];
    for (const r of rows) for (const s of spec.series) if (r[s.idx] != null) vals.push(r[s.idx]);
    let lo = Math.min(...vals), hi = Math.max(...vals);
    if (spec.zeroTop) { hi = 0; lo = Math.min(lo, -0.01); }
    const t = niceTicks(lo, hi, spec.ticksN || 4);
    const n = rows.length;
    const span = w - m.l - m.r;
    const x = (i) => m.l + (n === 1 ? span / 2 : (i * span) / (n - 1));
    const y = (v) => m.t + ((t.hi - v) / (t.hi - t.lo)) * (H - m.t - m.b);
    const svg = el("svg", { viewBox: `0 0 ${w} ${H}`, width: w, height: H, role: "img", "aria-label": spec.label }, plot);

    if (spec.band != null) {  // Risk-On 区间：相邻的日子合并成一个矩形
      const half = n > 1 ? span / (n - 1) / 2 : span / 2;
      let start = -1;
      for (let i = 0; i <= n; i++) {
        const on = i < n && rows[i][spec.band];
        if (on && start < 0) start = i;
        if (!on && start >= 0) {
          const x0 = Math.max(m.l, x(start) - half), x1 = Math.min(w - m.r, x(i - 1) + half);
          el("rect", { class: "band-on", x: x0, y: m.t, width: Math.max(1, x1 - x0), height: H - m.t - m.b }, svg);
          start = -1;
        }
      }
    }
    for (const v of t.ticks) {
      el("line", { class: "gridline", x1: m.l, x2: w - m.r, y1: y(v), y2: y(v) }, svg);
      const lab = el("text", { class: "tick", x: m.l - 8, y: y(v) + 4, "text-anchor": "end" }, svg);
      lab.textContent = spec.tick(v, t.step);
    }
    el("line", { class: "baseline", x1: m.l, x2: w - m.r, y1: H - m.b + 0.5, y2: H - m.b + 0.5 }, svg);
    const longSpan = n > 200;
    const idxs = [...new Set([0, Math.round((n - 1) / 3), Math.round((2 * (n - 1)) / 3), n - 1])];
    idxs.forEach((i, k) => {
      const anchor = k === 0 ? "start" : k === idxs.length - 1 ? "end" : "middle";
      const lab = el("text", { class: "tick", x: n === 1 ? x(i) : x(i), y: H - 6, "text-anchor": n === 1 ? "middle" : anchor }, svg);
      lab.textContent = dateLabel(rows[i][0], longSpan);
    });

    for (const s of spec.series) {
      let d = "", pen = false;
      rows.forEach((r, i) => {
        const v = r[s.idx];
        if (v == null) { pen = false; return; }
        d += (pen ? "L" : "M") + x(i).toFixed(1) + "," + y(v).toFixed(1);
        pen = true;
      });
      if (n === 1 && rows[0][s.idx] != null) d = `M${m.l},${y(rows[0][s.idx]).toFixed(1)}L${w - m.r},${y(rows[0][s.idx]).toFixed(1)}`;
      if (s.area && n > 1) {
        el("path", { class: "dd-area", d: `${d}L${x(n - 1).toFixed(1)},${y(0).toFixed(1)}L${x(0).toFixed(1)},${y(0).toFixed(1)}Z` }, svg);
      }
      el("path", { class: s.cls, d }, svg);
    }

    // 十字准线、各系列的点、提示框
    const cross = el("line", { class: "cross", y1: m.t, y2: H - m.b, visibility: "hidden" }, svg);
    const dots = spec.series.map((s) => el("circle", { class: "pt " + s.dot, r: 4, visibility: "hidden" }, svg));
    const hit = el("rect", { x: m.l, y: 0, width: span, height: H, fill: "transparent" }, svg);
    const tip = h("div", "tip", null, plot);
    tip.hidden = true;

    let cur = -1;
    function show(i) {
      i = Math.max(0, Math.min(n - 1, i));
      cur = i;
      const r = rows[i], cx = x(i);
      cross.setAttribute("x1", cx); cross.setAttribute("x2", cx); cross.setAttribute("visibility", "visible");
      spec.series.forEach((s, k) => {
        const v = r[s.idx];
        if (v == null) { dots[k].setAttribute("visibility", "hidden"); return; }
        dots[k].setAttribute("cx", cx); dots[k].setAttribute("cy", y(v)); dots[k].setAttribute("visibility", "visible");
      });
      tip.textContent = "";
      h("div", "d", r[0], tip);
      for (const s of spec.series) {
        const row = h("div", "r", null, tip);
        h("i", s.key, null, row);
        h("b", null, r[s.idx] == null ? "—" : s.fmt(r[s.idx]), row);
        h("span", null, s.name, row);
      }
      if (spec.extra) {
        const [k, v] = spec.extra(r);
        const row = h("div", "r", null, tip);
        h("b", null, v, row); h("span", null, k, row);
      }
      tip.hidden = false;
      const tw = tip.offsetWidth;
      const left = cx + 14 + tw > w ? cx - 14 - tw : cx + 14;
      tip.style.left = Math.max(0, left) + "px";
      tip.style.top = m.t + "px";
    }
    function hide() {
      cur = -1;
      cross.setAttribute("visibility", "hidden");
      dots.forEach((d) => d.setAttribute("visibility", "hidden"));
      tip.hidden = true;
    }
    const indexAt = (clientX) => {
      const px = clientX - svg.getBoundingClientRect().left;
      return n === 1 ? 0 : Math.round(((px - m.l) / span) * (n - 1));
    };
    hit.addEventListener("pointermove", (e) => show(indexAt(e.clientX)));
    hit.addEventListener("pointerdown", (e) => show(indexAt(e.clientX)));
    hit.addEventListener("pointerleave", (e) => { if (e.pointerType === "mouse") hide(); });
    fig.onkeydown = (e) => {
      if (e.key === "ArrowLeft") { show(cur < 0 ? n - 1 : cur - 1); e.preventDefault(); }
      else if (e.key === "ArrowRight") { show(cur < 0 ? n - 1 : cur + 1); e.preventDefault(); }
      else if (e.key === "Home") { show(0); e.preventDefault(); }
      else if (e.key === "End") { show(n - 1); e.preventDefault(); }
      else if (e.key === "Escape") hide();
    };
    fig.onfocus = () => { if (cur < 0) show(n - 1); };
    fig.onblur = () => hide();
  }

  function initCharts() {
    const node = document.getElementById("chart-data");
    if (!node) return;
    let data;
    try { data = JSON.parse(node.textContent); } catch { return; }
    const figs = [...document.querySelectorAll("figure[data-chart]")].filter((f) => SPECS[f.dataset.chart]);
    const buttons = [...document.querySelectorAll(".seg [data-range]")];
    let range = store.get("dash-range") || "365";
    if (!buttons.some((b) => b.dataset.range === range)) range = "365";

    function slice(rows) {
      if (!rows.length) return rows;
      const last = new Date(rows[rows.length - 1][0] + "T00:00:00Z");
      last.setUTCDate(last.getUTCDate() - Number(range));
      const cut = last.toISOString().slice(0, 10);
      return rows.filter((r) => r[0] > cut);
    }
    function drawAll() {
      buttons.forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.range === range)));
      for (const f of figs) {
        const spec = SPECS[f.dataset.chart];
        render(f, spec, slice(data[spec.key] || []));
      }
    }
    buttons.forEach((b) => b.addEventListener("click", () => { range = b.dataset.range; store.set("dash-range", range); drawAll(); }));
    drawAll();
    let raf = 0, lastW = 0;
    new ResizeObserver(() => {
      const wNow = figs[0] ? figs[0].clientWidth : 0;
      if (Math.abs(wNow - lastW) < 2) return;
      lastW = wNow;
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(drawAll);
    }).observe(document.body);
  }

  // ---------- 实时持仓（异步加载） ----------
  function initLive() {
    const slot = document.getElementById("live-slot");
    if (!slot) return;
    const btn = document.querySelector("[data-live-refresh]");
    async function load(fresh) {
      slot.classList.add("refreshing");
      try {
        const r = await fetch(api(slot.dataset.src + (fresh ? "?fresh=1" : "")), { credentials: "same-origin" });
        if (!r.ok) throw new Error("HTTP " + r.status);
        slot.innerHTML = await r.text();  // 同源，由服务端模板生成并转义
      } catch (e) {
        slot.textContent = "";
        const p = h("p", "loading", "读取实时持仓失败（" + e.message + "）。", slot);
        const retry = h("button", "btn", "重试", p);
        retry.type = "button";
        retry.style.marginLeft = "8px";
        retry.addEventListener("click", () => load(false));
      } finally {
        slot.classList.remove("refreshing");
      }
    }
    if (btn) { btn.hidden = false; btn.addEventListener("click", () => load(true)); }
    if (!slot.dataset.loaded) load(false);
  }

  // ---------- 分区导航高亮 ----------
  function initNav() {
    const links = [...document.querySelectorAll(".subnav a")];
    const map = new Map(links.map((a) => [a.getAttribute("href").slice(1), a]));
    const seen = new Map();
    const io = new IntersectionObserver((entries) => {
      for (const e of entries) seen.set(e.target.id, e.isIntersecting ? e.intersectionRatio : 0);
      let best = null, ratio = 0;
      for (const [id, r] of seen) if (r > ratio) { ratio = r; best = id; }
      links.forEach((a) => a.setAttribute("aria-current", String(map.get(best) === a)));
    }, { rootMargin: "-30% 0px -55% 0px", threshold: [0, 0.01, 0.5, 1] });
    for (const id of map.keys()) { const s = document.getElementById(id); if (s) io.observe(s); }
  }

  // ---------- 记录页签与事件筛选 ----------
  function initTabs() {
    const tabs = [...document.querySelectorAll('[role="tab"]')];
    function select(tab) {
      tabs.forEach((t) => {
        const on = t === tab;
        t.setAttribute("aria-selected", String(on));
        t.tabIndex = on ? 0 : -1;
        document.getElementById(t.getAttribute("aria-controls")).hidden = !on;
      });
    }
    tabs.forEach((t, i) => {
      t.addEventListener("click", () => select(t));
      t.addEventListener("keydown", (e) => {
        const d = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
        if (d) { const nt = tabs[(i + d + tabs.length) % tabs.length]; select(nt); nt.focus(); }
      });
    });
    const chips = [...document.querySelectorAll(".chips [data-level]")];
    chips.forEach((c) => c.addEventListener("click", () => {
      chips.forEach((x) => x.setAttribute("aria-pressed", String(x === c)));
      const lv = c.dataset.level;
      document.querySelectorAll("#tab-events tr[data-level]").forEach((tr) => {
        tr.hidden = Boolean(lv) && tr.dataset.level !== lv;
      });
    }));
  }

  // ---------- 操作：确认框、KILL 输入、后台操作轮询、结果提示 ----------
  function initOps() {
    const supportsPopover = typeof HTMLElement !== "undefined" && "popover" in HTMLElement.prototype;
    if (!supportsPopover) {  // 旧浏览器：对话框改成点击展开的普通表单
      document.querySelectorAll("[popover]").forEach((d) => { d.hidden = true; });
      document.querySelectorAll("[popovertarget]").forEach((b) => b.addEventListener("click", () => {
        const d = document.getElementById(b.getAttribute("popovertarget"));
        if (d) d.hidden = b.getAttribute("popovertargetaction") === "hide" ? true : !d.hidden;
      }));
    }
    const word = document.querySelector("[data-kill-word]");
    const submit = document.querySelector("[data-kill-submit]");
    if (word && submit) {
      const sync = () => { submit.disabled = word.value.trim() !== word.dataset.killWord; };
      word.addEventListener("input", sync);
      sync();
    }
    document.querySelectorAll("form[action^='/ops/']").forEach((f) => f.addEventListener("submit", () => {
      f.querySelectorAll("button[type=submit]").forEach((b) => { b.disabled = true; b.textContent = "提交中…"; });
    }));

    const toast = document.getElementById("toast");
    if (toast) {
      history.replaceState(null, "", location.pathname + location.hash);
      const close = () => toast.classList.add("hide");
      toast.querySelector("[data-dismiss]")?.addEventListener("click", (e) => { e.preventDefault(); close(); });
      if (!toast.querySelector(".tag.进行中")) setTimeout(close, 7000);
    }
    if (document.body.dataset.busy === "1") {
      const timer = setInterval(async () => {
        try {
          const r = await fetch(api("/api/ops"), { credentials: "same-origin" });
          const s = await r.json();
          if (!s.busy) { clearInterval(timer); location.replace("/?op=1#ops"); }
        } catch { /* 网络抖动：下次再试 */ }
      }, 3000);
    }
  }

  // ---------- 顶栏：更新时间、主题按钮、定时刷新 ----------
  function initTop() {
    const upd = document.querySelector(".updated[data-ts]");
    const tick = () => {
      if (!upd) return;
      const s = (Date.now() - Date.parse(upd.dataset.ts)) / 1000;
      upd.textContent = s < 60 ? "刚刚更新" : s < 3600 ? Math.floor(s / 60) + " 分钟前更新" : Math.floor(s / 3600) + " 小时前更新";
    };
    tick();
    setInterval(tick, 30000);

    const btn = document.getElementById("theme-btn");
    if (btn) {
      btn.hidden = false;
      const label = () => { const t = store.get("dash-theme") || "auto"; btn.title = "主题：" + THEME_LABEL[t] + "（点击切换）"; };
      label();
      btn.addEventListener("click", () => {
        const cur = store.get("dash-theme") || "auto";
        const next = THEMES[(THEMES.indexOf(cur) + 1) % THEMES.length];
        store.set("dash-theme", next);
        applyTheme(next);  // 图表颜色来自 CSS 变量，不需要重画
        label();
      });
    }
    // 每 5 分钟刷新一次页面：只在页面可见、没有打开确认框、没有正在输入时
    setInterval(() => {
      const typing = document.activeElement && /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
      let open = false;
      try { open = Boolean(document.querySelector(":popover-open")); } catch { open = false; }
      if (document.visibilityState === "visible" && !typing && !open) location.reload();
    }, 300000);
  }

  function boot() {
    initTop();
    initCharts();
    initLive();
    initNav();
    initTabs();
    initOps();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
