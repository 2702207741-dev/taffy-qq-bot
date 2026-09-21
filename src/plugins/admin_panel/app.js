/* 塔菲管理面板 — 单页应用逻辑（零依赖，手写 SVG 图表） */
"use strict";

const API = "/api/admin";
/* 面板登录会话有效期：超时后本地令牌作废，必须重新输入 */
const TOKEN_TTL_MS = 12 * 60 * 60 * 1000;
const state = {
  token: localStorage.getItem("taffy_token") || "",
  tab: "dashboard",
  usageOffset: 0,
  usageTotal: 0,
  usageQuery: {},
  learningStatus: "",
  eventSince: null,
  eventCount: 0,
  schedulerVersion: null,
  broadcastGroups: new Map(), // id -> {on}
  schedTimes: new Set(),
  charts: new Map(), // containerId -> {draw()}
};

const TAB_TITLES = {
  dashboard: "仪表盘",
  broadcast: "广播发送",
  scheduler: "定时配置",
  permissions: "权限管理",
  usage: "使用审计",
  monitors: "监控总览",
  sessions: "会话管理",
  learning: "学习记忆",
};

const $ = (id) => document.getElementById(id);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtTime = (ts) => (ts ? new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false }) : "-");
const fmtDur = (sec) => {
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
  return (d ? d + "天" : "") + (h ? h + "时" : "") + m + "分";
};

/* ---------------- 基础设施 ---------------- */

function clearSession() {
  /* Forget the stored credential entirely; showLogin alone only resets UI. */
  state.token = "";
  localStorage.removeItem("taffy_token");
  localStorage.removeItem("taffy_token_ts");
}

function sessionExpired() {
  const savedAt = Number(localStorage.getItem("taffy_token_ts") || 0);
  return Boolean(state.token) && savedAt > 0 && Date.now() - savedAt > TOKEN_TTL_MS;
}

async function api(path, opts = {}) {
  const headers = { Authorization: `Bearer ${state.token}` };
  if (opts.body) headers["Content-Type"] = "application/json";
  const res = await fetch(API + path, { ...opts, headers });
  if (res.status === 401) { clearSession(); showLogin(); throw new Error("auth"); }
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try { const body = await res.json(); if (body && body.detail) detail = body.detail; } catch { /* keep */ }
    // 503 仅在管理 API 本身未启用时才视为登录态问题；业务性 503（如 bot 未连接）按普通错误反馈。
    if (res.status === 503 && detail === "Admin API is disabled") { clearSession(); showLogin(); throw new Error("auth"); }
    throw new Error(detail);
  }
  return res.json();
}

function toast(message, kind = "info", ms = 4200) {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  el.onclick = () => el.remove();
  $("toast-stack").appendChild(el);
  setTimeout(() => el.remove(), ms);
}

function confirmModal(title, text) {
  return new Promise((resolve) => {
    $("modal-title").textContent = title;
    $("modal-text").textContent = text;
    $("modal-mask").classList.remove("hidden");
    const done = (value) => {
      $("modal-mask").classList.add("hidden");
      $("modal-confirm").onclick = null;
      $("modal-cancel").onclick = null;
      resolve(value);
    };
    $("modal-confirm").onclick = () => done(true);
    $("modal-cancel").onclick = () => done(false);
  });
}

function showLogin() {
  $("app").classList.add("hidden");
  $("login").classList.remove("hidden");
  stopEventPolling();
}

function showApp() {
  $("login").classList.add("hidden");
  $("app").classList.remove("hidden");
  switchTab(state.tab);
  startEventPolling();
}

function switchTab(tab) {
  state.tab = tab;
  $$(".side-item[data-tab]").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  Object.keys(TAB_TITLES).forEach((t) => {
    const el = $("tab-" + t);
    if (el) el.classList.toggle("hidden", t !== tab);
  });
  $("page-title").textContent = TAB_TITLES[tab] || tab;
  closeSidebar();
  if (tab === "dashboard") loadDashboard();
  if (tab === "broadcast") loadBroadcast();
  if (tab === "scheduler") loadScheduler();
  if (tab === "permissions") loadPermissions();
  if (tab === "usage") loadUsage(true);
  if (tab === "monitors") loadMonitors();
  if (tab === "sessions") loadSessions();
  if (tab === "learning") loadLearning(true);
}

/* 加载态 / 空态 / 错误态 */
const skeletonBlock = () => `<div class="skeleton block"></div>`;
const skeletonRows = (n, cols) =>
  Array.from({ length: n }, () => `<tr>${`<td><div class="skeleton line" style="width:${60 + Math.random() * 40}%"></div></td>`.repeat(cols)}</tr>`).join("");
const emptyState = (icon, text, hint = "") =>
  `<div class="empty"><div class="empty-icon">${icon}</div><div>${esc(text)}</div>${hint ? `<div class="dim" style="font-size:12px">${esc(hint)}</div>` : ""}</div>`;
function errorState(retryFn) {
  const id = "retry-" + Math.random().toString(36).slice(2, 8);
  setTimeout(() => { const el = $(id); if (el) el.onclick = retryFn; }, 0);
  return `${emptyState("⚠️", "加载失败")}<button class="btn sm retry-btn" id="${id}">重试</button>`;
}

/* ---------------- SVG 图表 ---------------- */

const PALETTE = ["#4f8cff", "#3fb950", "#e3b341", "#f85149", "#a371f7", "#39c5cf", "#d29922", "#7ee787", "#ff9bce", "#8b93a3"];

function niceBounds(values) {
  let min = Math.min(...values), max = Math.max(...values);
  if (!isFinite(min) || !isFinite(max)) { min = 0; max = 1; }
  if (min === max) { min = Math.min(0, min); max = Math.max(1, max); }
  const pad = (max - min) * 0.12;
  return [min - pad, max + pad];
}

function makeTooltip(container) {
  let tip = container.querySelector(".chart-tooltip");
  if (!tip) {
    tip = document.createElement("div");
    tip.className = "chart-tooltip";
    container.appendChild(tip);
  }
  return tip;
}

function drawLineChart(container, { labels, series, yLabel = "", money = false }) {
  container = typeof container === "string" ? $(container) : container;
  const W = Math.max(280, container.clientWidth || 600);
  const H = container.classList.contains("tall") ? 280 : 240;
  const pl = 54, pr = 14, pt = 14, pb = 30;
  const iw = W - pl - pr, ih = H - pt - pb;
  const all = series.flatMap((s) => s.values);
  if (!all.length || labels.length < 2) {
    container.innerHTML = emptyState("📈", "数据采集中", "有数据后自动出图");
    return;
  }
  // 窄屏降采样：只保留 ~40 个点
  const step = Math.max(1, Math.ceil(labels.length / 40));
  const idxs = Array.from({ length: labels.length }, (_, i) => i).filter((i) => i % step === 0 || i === labels.length - 1);
  const [minY, maxY] = niceBounds(all);
  const X = (i) => pl + (idxs.indexOf(i) / (idxs.length - 1)) * iw;
  const Y = (v) => pt + ih - ((v - minY) / (maxY - minY)) * ih;
  const fmtV = (v) => (money ? "¥" + v.toFixed(2) : Math.round(v * 100) / 100);

  let grid = "";
  for (let g = 0; g <= 4; g++) {
    const v = minY + ((maxY - minY) * g) / 4;
    const y = Y(v);
    grid += `<line x1="${pl}" y1="${y}" x2="${W - pr}" y2="${y}" stroke="var(--line)" stroke-width="1"/>`
      + `<text x="${pl - 8}" y="${y + 4}" text-anchor="end" font-size="10" fill="var(--dim)">${fmtV(v)}</text>`;
  }
  const tickEvery = Math.max(1, Math.ceil(idxs.length / 6));
  let xLabels = "";
  idxs.forEach((i, k) => {
    if (k % tickEvery !== 0 && k !== idxs.length - 1) return;
    xLabels += `<text x="${X(i)}" y="${H - 8}" text-anchor="middle" font-size="10" fill="var(--dim)">${esc(labels[i].slice(5))}</text>`;
  });

  const paths = series.map((s, si) => {
    const pts = idxs.map((i) => [X(i), Y(s.values[i])]);
    const d = pts.map((p, k) => (k ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" ");
    const area = pts.length > 1
      ? `${d} L${pts[pts.length - 1][0].toFixed(1)} ${pt + ih} L${pts[0][0].toFixed(1)} ${pt + ih} Z`
      : "";
    const circles = pts.map((p, k) => `<circle cx="${p[0].toFixed(1)}" cy="${p[1].toFixed(1)}" r="3" fill="${s.color}" data-i="${idxs[k]}" class="pt"/>`).join("");
    return `${si === 0 && series.length === 1 ? `<path d="${area}" fill="${s.color}" opacity="0.08"/>` : ""}`
      + `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>${circles}`;
  }).join("");

  container.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="height:${H}px">
    ${grid}${xLabels}${paths}
    <line id="guide" x1="0" x2="0" y1="${pt}" y2="${pt + ih}" stroke="var(--accent)" stroke-dasharray="3 3" opacity="0"/>
  </svg>
  ${series.length > 1 ? `<div class="chart-legend">${series.map((s) => `<span><span class="dot" style="background:${s.color}"></span>${esc(s.name || "")}</span>`).join("")}</div>` : ""}`;

  const svg = container.querySelector("svg");
  const tip = makeTooltip(container);
  const guide = svg.querySelector("#guide");
  const onMove = (ev) => {
    const rect = svg.getBoundingClientRect();
    const x = ((ev.clientX - rect.left) / rect.width) * W;
    let best = idxs[0], bestD = Infinity;
    idxs.forEach((i) => { const d = Math.abs(X(i) - x); if (d < bestD) { bestD = d; best = i; } });
    const lines = [labels[best] + (yLabel ? " · " + yLabel : "")]
      .concat(series.map((s) => `${s.name ? s.name + "：" : ""}${fmtV(s.values[best])}`));
    tip.innerHTML = lines.map(esc).join("<br>");
    tip.style.left = (X(best) / W) * rect.width + "px";
    tip.style.top = (Y(series[0].values[best]) / H) * rect.height + "px";
    tip.style.display = "block";
    guide.setAttribute("x1", X(best)); guide.setAttribute("x2", X(best)); guide.setAttribute("opacity", "0.5");
  };
  const onLeave = () => { tip.style.display = "none"; guide.setAttribute("opacity", "0"); };
  svg.onmousemove = onMove;
  svg.onmouseleave = onLeave;
}

function drawBarChart(container, { labels, values, color = "var(--accent)" }) {
  container = typeof container === "string" ? $(container) : container;
  const W = Math.max(280, container.clientWidth || 600);
  const H = 240, pl = 40, pr = 10, pt = 16, pb = 30;
  const iw = W - pl - pr, ih = H - pt - pb;
  if (!values.length) { container.innerHTML = emptyState("📊", "数据采集中"); return; }
  const max = Math.max(...values) * 1.15 || 1;
  const bw = (iw / values.length) * 0.62;
  let bars = "", labels2 = "";
  values.forEach((v, i) => {
    const x = pl + (i + 0.5) * (iw / values.length) - bw / 2;
    const h = Math.max(2, (v / max) * ih);
    bars += `<rect x="${x.toFixed(1)}" y="${(pt + ih - h).toFixed(1)}" width="${bw.toFixed(1)}" height="${h.toFixed(1)}" rx="4" fill="${color}" opacity="0.85" data-i="${i}"/>`;
    labels2 += `<text x="${(x + bw / 2).toFixed(1)}" y="${H - 8}" text-anchor="middle" font-size="10" fill="var(--dim)">${esc(labels[i])}</text>`;
  });
  let grid = "";
  for (let g = 0; g <= 3; g++) {
    const y = pt + (ih * g) / 3;
    grid += `<line x1="${pl}" y1="${y}" x2="${W - pr}" y2="${y}" stroke="var(--line)"/>`
      + `<text x="${pl - 6}" y="${y + 4}" text-anchor="end" font-size="10" fill="var(--dim)">${Math.round(max - (max * g) / 3)}</text>`;
  }
  container.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="height:${H}px">${grid}${bars}${labels2}</svg>`;
  const svg = container.querySelector("svg");
  const tip = makeTooltip(container);
  svg.onmousemove = (ev) => {
    const rect = svg.getBoundingClientRect();
    const target = ev.target.closest("rect[data-i]");
    if (!target) { tip.style.display = "none"; return; }
    const i = +target.dataset.i;
    tip.textContent = `${labels[i]}：${values[i]}`;
    tip.style.left = ((ev.clientX - rect.left) / rect.width) * W + "px";
    tip.style.top = ((ev.clientY - rect.top) / rect.height) * H + "px";
    tip.style.display = "block";
  };
  svg.onmouseleave = () => { tip.style.display = "none"; };
}

function drawDonutChart(container, { labels, values, colors = PALETTE }) {
  container = typeof container === "string" ? $(container) : container;
  if (!values.length || values.reduce((a, b) => a + b, 0) === 0) {
    container.innerHTML = emptyState("🍩", "数据采集中", "近 7 天暂无使用记录");
    return;
  }
  const total = values.reduce((a, b) => a + b, 0);
  const R = 74, C = 92, gap = 0.02;
  let angle = -Math.PI / 2;
  const arcs = values.map((v, i) => {
    const frac = v / total;
    const a0 = angle + gap / 2, a1 = angle + frac * Math.PI * 2 - gap / 2;
    angle += frac * Math.PI * 2;
    const large = a1 - a0 > Math.PI ? 1 : 0;
    const p = (a) => [C + R * Math.cos(a), C + R * Math.sin(a)];
    const [x0, y0] = p(a0), [x1, y1] = p(a1);
    return `<path d="M ${x0.toFixed(2)} ${y0.toFixed(2)} A ${R} ${R} 0 ${large} 1 ${x1.toFixed(2)} ${y1.toFixed(2)}"
      stroke="${colors[i % colors.length]}" stroke-width="26" fill="none" stroke-linecap="butt" data-i="${i}"/>`;
  }).join("");
  container.innerHTML = `<div style="display:flex;align-items:center;gap:18px;flex-wrap:wrap">
    <svg viewBox="0 0 184 184" style="width:184px;height:184px;flex:0 0 auto">
      ${arcs}
      <text x="92" y="88" text-anchor="middle" font-size="24" font-weight="700" fill="var(--fg)">${total}</text>
      <text x="92" y="106" text-anchor="middle" font-size="11" fill="var(--dim)">总计</text>
    </svg>
    <div class="chart-legend" style="flex-direction:column;align-items:flex-start;gap:7px;margin-top:0">
      ${labels.map((l, i) => `<span><span class="dot" style="background:${colors[i % colors.length]}"></span>${esc(l)} · ${values[i]}（${((values[i] / total) * 100).toFixed(1)}%）</span>`).join("")}
    </div>
  </div>`;
  const svg = container.querySelector("svg");
  const tip = makeTooltip(container);
  svg.onmousemove = (ev) => {
    const target = ev.target.closest("path[data-i]");
    if (!target) { tip.style.display = "none"; return; }
    const i = +target.dataset.i;
    const rect = container.getBoundingClientRect();
    tip.textContent = `${labels[i]}：${values[i]}（${((values[i] / total) * 100).toFixed(1)}%）`;
    tip.style.left = ev.clientX - rect.left + "px";
    tip.style.top = ev.clientY - rect.top + "px";
    tip.style.display = "block";
  };
  svg.onmouseleave = () => { tip.style.display = "none"; };
}

function registerChart(containerId, draw) {
  state.charts.set(containerId, draw);
  draw();
}

let resizeTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => state.charts.forEach((draw) => draw()), 150);
});

/* ---------------- 仪表盘 ---------------- */

async function loadDashboard() {
  const statEl = $("stat-cards");
  statEl.innerHTML = ["", "", "", "", "", ""].map(() => `<div class="stat"><div class="skeleton line" style="width:50%"></div><div class="skeleton line" style="width:80%;height:24px"></div></div>`).join("");
  ["chart-usage-trend", "chart-feature-pie", "chart-market-trend"].forEach((id) => { const el = $(id); if (el) el.innerHTML = skeletonBlock(); });
  $("market-status").innerHTML = skeletonBlock();
  $("feature-stats").innerHTML = skeletonRows(5, 3);
  $("recent-usage").innerHTML = skeletonRows(6, 5);
  $("llm-usage-list").innerHTML = skeletonRows(5, 4);

  try {
    const [summary, trend, pie, market, recent, marketStatus, llmUsage] = await Promise.all([
      api("/stats/summary"),
      api("/stats/usage-trend?days=30"),
      api("/stats/feature-pie?days=7"),
      api("/stats/market-trend?days=30"),
      api("/usage?limit=500"),
      api("/market-status"),
      api("/stats/llm-usage?days=7"),
    ]);
    const cards = [
      { num: summary.today_usage, lbl: "今日命令数", cls: "accent" },
      { num: summary.active_users, lbl: "今日活跃用户", cls: "" },
      { num: summary.alerts_pending, lbl: "预警等待中", cls: summary.alerts_pending > 0 ? "warn" : "" },
      { num: summary.alerts_delivered, lbl: "预警已触发", cls: "ok" },
      { num: summary.watchlist_total, lbl: "自选总数", cls: "" },
      { num: fmtDur(summary.uptime_seconds), lbl: "运行时长", cls: "" },
    ];
    statEl.innerHTML = cards.map((c) => `<div class="stat ${c.cls}"><div class="num">${esc(String(c.num))}</div><div class="lbl">${esc(c.lbl)}</div></div>`).join("");

    registerChart("chart-usage-trend", () => drawLineChart("chart-usage-trend", {
      labels: trend.labels, series: [{ values: trend.values, color: "#4f8cff", name: "命令数" }],
    }));
    registerChart("chart-feature-pie", () => drawDonutChart("chart-feature-pie", {
      labels: pie.labels, values: pie.values,
    }));
    registerChart("chart-market-trend", () => drawLineChart("chart-market-trend", {
      labels: market.labels, series: [{ values: market.values, color: "#3fb950", name: "收盘点位" }], money: true,
    }));

    renderMarketStatus(marketStatus);

    const records = recent.usage || [];
    const byFeature = {};
    const byUser = {};
    records.forEach((r) => { byFeature[r.feature] = (byFeature[r.feature] || 0) + 1; byUser[r.user_id] = (byUser[r.user_id] || 0) + 1; });
    const top = Object.entries(byFeature).sort((a, b) => b[1] - a[1]);
    $("feature-stats").innerHTML = top.length
      ? top.map(([f, n]) => `<tr><td>${esc(f)}</td><td>${n}</td><td>${records.length ? ((n / records.length) * 100).toFixed(1) : 0}%</td></tr>`).join("")
      : `<tr><td colspan="3">${emptyState("📭", "还没有使用记录")}</td></tr>`;
    $("recent-usage").innerHTML = records.length
      ? records.slice(0, 20).map((r) =>
        `<tr><td class="dim">${fmtTime(r.created_at)}</td><td>${r.user_id}</td><td>${esc(r.feature)}</td>` +
        `<td class="mono">${esc(String(r.input_text).slice(0, 60))}</td><td class="mono wrap">${esc(String(r.output_text || "").slice(0, 80))}</td></tr>`).join("")
      : `<tr><td colspan="5">${emptyState("📭", "还没有使用记录", "用户使用任何功能后会自动记录在这里")}</td></tr>`;

    const llmRows = (llmUsage.labels || []).map((day, i) => {
      const req = (llmUsage.requests || [])[i] || 0;
      const inp = (llmUsage.input_tokens || [])[i] || 0;
      const out = (llmUsage.output_tokens || [])[i] || 0;
      return `<tr><td class="mono">${esc(day)}</td><td>${req}</td><td>${inp.toLocaleString()}</td><td>${out.toLocaleString()}</td></tr>`;
    });
    $("llm-usage-list").innerHTML = llmRows.length
      ? llmRows.join("")
      : `<tr><td colspan="4">${emptyState("📭", "暂无 LLM 调用记录")}</td></tr>`;
    const totalIn = (llmUsage.input_tokens || []).reduce((a, b) => a + (b || 0), 0);
    const totalOut = (llmUsage.output_tokens || []).reduce((a, b) => a + (b || 0), 0);
    $("llm-usage-note").textContent = llmRows.length
      ? `近 ${llmRows.length} 天合计：输入 ${totalIn.toLocaleString()} tokens，输出 ${totalOut.toLocaleString()} tokens`
      : "";
  } catch (e) {
    if (e.message !== "auth") {
      statEl.innerHTML = `<div class="card" style="grid-column:1/-1">${errorState(loadDashboard)}</div>`;
      ["chart-usage-trend", "chart-feature-pie", "chart-market-trend"].forEach((id) => { const el = $(id); if (el) el.innerHTML = errorState(loadDashboard); });
      $("market-status").innerHTML = errorState(loadDashboard);
      $("llm-usage-list").innerHTML = errorState(loadDashboard);
      console.error(e);
    }
  }
}

/* 市场功能状态卡片：三项只读状态，后端保证每项要么是真数字要么是诚实降级 */
function renderMarketStatus(data) {
  const el = $("market-status");
  const items = [
    { ico: "🌡️", label: "盘面温度", cls: data.temperature?.available ? "" : "dim", text: data.temperature?.display || "暂不可用" },
    { ico: "🔮", label: "预测倾向", cls: data.prediction?.available ? "" : "dim", text: data.prediction?.display || "积累中" },
    { ico: "🏆", label: "榜单就绪度", cls: data.ranking?.ready ? "ok" : "warn", text: data.ranking?.display || "积累中" },
  ];
  el.innerHTML = `<div class="market-status">` + items.map((it) => `
    <div class="market-status-item ${it.cls}">
      <div class="ms-label">${it.ico} ${esc(it.label)}</div>
      <div class="ms-value">${esc(it.text)}</div>
    </div>`).join("") + `</div>`;
}

/* ---------------- 广播 ---------------- */

async function loadBroadcast() {
  $("broadcast-result-card").classList.add("hidden");
  try {
    const [sched, deliveries] = await Promise.all([
      api("/scheduler"),
      api("/broadcast-deliveries?limit=200"),
    ]);
    const groups = new Set();
    (sched.groups || []).forEach((g) => groups.add(String(g)));
    (deliveries.deliveries || []).forEach((d) => groups.add(String(d.group_id)));
    state.broadcastGroups = new Map(Array.from(groups).sort((a, b) => a - b).map((g) => [g, false]));
    renderBroadcastGroups();
    renderBroadcastDeliveries(deliveries.deliveries || []);
    $("broadcast-source-note").textContent = `群列表来自定时配置与近期投递记录（共 ${groups.size} 个）；发送前请手动勾选目标群，也可手动添加。`;
  } catch (e) {
    if (e.message !== "auth") {
      state.broadcastGroups = new Map();
      renderBroadcastGroups();
      toast("群列表加载失败：" + e.message, "bad");
    }
  }
}

const DELIVERY_STATUS = {
  pending: ["等待中", "warn"], claimed: ["执行中", "dim"],
  delivered: ["已投递", "ok"], failed: ["失败", "bad"],
};

function renderBroadcastDeliveries(deliveries) {
  const el = $("broadcast-deliveries");
  if (!deliveries.length) {
    el.innerHTML = `<tr><td colspan="7">${emptyState("📭", "暂无投递记录", "定时播报或广播发送后这里会出现台账")}</td></tr>`;
    return;
  }
  el.innerHTML = deliveries.map((d) => {
    const [label, cls] = DELIVERY_STATUS[d.status] || [esc(d.status), "dim"];
    const ops = d.status === "failed"
      ? `<button class="btn sm" data-del-retry="${esc(d.schedule_key)}" data-del-group="${d.group_id}">重试</button> ` +
        `<button class="btn sm danger" data-del-discard="${esc(d.schedule_key)}" data-del-group="${d.group_id}">丢弃</button>`
      : "";
    return `<tr><td class="wrap">${esc(d.schedule_key)}</td><td>${d.group_id}</td>` +
      `<td><span class="badge ${cls}">${label}</span></td><td>${d.attempts}</td>` +
      `<td class="wrap dim">${esc(d.last_error || "-")}</td>` +
      `<td class="dim">${fmtTime(d.updated_at)}</td><td>${ops}</td></tr>`;
  }).join("");
  $$("[data-del-retry]").forEach((b) => {
    b.onclick = async () => {
      const okToGo = await confirmModal("重试投递", `重新投递定时任务「${b.dataset.delRetry}」到群 ${b.dataset.delGroup}？`);
      if (!okToGo) return;
      try {
        await api(`/broadcast-deliveries/${encodeURIComponent(b.dataset.delRetry)}/${b.dataset.delGroup}/retry`, { method: "POST" });
        toast("已重新入队", "ok");
        loadBroadcast();
      } catch (e) { if (e.message !== "auth") toast("重试失败：" + e.message, "bad"); }
    };
  });
  $$("[data-del-discard]").forEach((b) => {
    b.onclick = async () => {
      const okToGo = await confirmModal("丢弃投递", `丢弃定时任务「${b.dataset.delDiscard}」到群 ${b.dataset.delGroup} 的失败投递？此操作不可恢复。`);
      if (!okToGo) return;
      try {
        await api(`/broadcast-deliveries/${encodeURIComponent(b.dataset.delDiscard)}/${b.dataset.delGroup}`, { method: "DELETE" });
        toast("投递已丢弃", "ok");
        loadBroadcast();
      } catch (e) { if (e.message !== "auth") toast("丢弃失败：" + e.message, "bad"); }
    };
  });
}

function renderBroadcastGroups() {
  const el = $("broadcast-groups");
  if (!state.broadcastGroups.size) {
    el.innerHTML = emptyState("👥", "暂无已知群号", "可从定时配置页添加推送群，或在上方手动输入");
    return;
  }
  el.innerHTML = Array.from(state.broadcastGroups.entries())
    .map(([g, on]) => `<span class="chip ${on ? "on" : ""}" data-group="${esc(g)}">${esc(g)}</span>`).join("");
  el.querySelectorAll(".chip").forEach((chip) => {
    chip.onclick = () => {
      const g = chip.dataset.group;
      state.broadcastGroups.set(g, !state.broadcastGroups.get(g));
      chip.classList.toggle("on", state.broadcastGroups.get(g));
    };
  });
}

$("broadcast-add-group").onclick = () => {
  const input = $("broadcast-manual-group");
  const v = input.value.trim();
  if (!v || !/^\d+$/.test(v) || +v <= 0) { toast("请输入有效的群号", "warn"); return; }
  if (state.broadcastGroups.size >= 50) { toast("最多 50 个群", "warn"); return; }
  state.broadcastGroups.set(v, true);
  input.value = "";
  renderBroadcastGroups();
};

$("broadcast-text").oninput = (ev) => {
  $("broadcast-counter").textContent = `${ev.target.value.length} / 2000 字`;
};

$("broadcast-send").onclick = async () => {
  const targets = Array.from(state.broadcastGroups.entries()).filter(([, on]) => on).map(([g]) => parseInt(g, 10));
  const text = $("broadcast-text").value.trim();
  if (!targets.length) { toast("请至少选择一个群", "warn"); return; }
  if (!text) { toast("请输入广播正文", "warn"); return; }
  const okToGo = await confirmModal("发送广播", `将向 ${targets.length} 个群发送：\n\n${text.slice(0, 160)}${text.length > 160 ? "…" : ""}`);
  if (!okToGo) return;
  $("broadcast-send").disabled = true;
  try {
    const result = await api("/broadcast", {
      method: "POST",
      body: JSON.stringify({ group_ids: targets, payload_text: text }),
    });
    const failed = result.results.length - result.succeeded;
    const unconfirmed = Number(result.unconfirmed || 0);
    $("broadcast-result-card").classList.remove("hidden");
    $("broadcast-result").innerHTML =
      `<p>共 ${result.total} 群，成功 <b style="color:var(--ok)">${result.succeeded}</b>，失败 <b style="color:var(--bad)">${failed}</b>，未确认 <b>${esc(String(unconfirmed))}</b></p>`
      + (unconfirmed > 0
        ? "<p><small>未确认 = 可能已发出但未记账；这些行仍持有约 10 分钟租约，期内重试会被拒。重投前请先在群里核对是否已收到。</small></p>"
        : "")
      + `<div class="table-wrap"><table><thead><tr><th>群</th><th>结果</th></tr></thead><tbody>`
      + result.results.map((r) => `<tr><td>${r.group_id}</td><td>${r.success ? '<span class="badge ok">已发送</span>' : `<span class="badge bad">失败：${esc(r.error || "")}</span>`}</td></tr>`).join("")
      + `</tbody></table></div>`;
    toast(
      `广播完成：成功 ${result.succeeded}/${result.total}${unconfirmed > 0 ? `（未确认 ${unconfirmed}）` : ""}`,
      failed === 0 && unconfirmed === 0 ? "ok" : "warn",
    );
  } catch (e) {
    if (e.message !== "auth") toast("广播失败：" + e.message, "bad", 6000);
  } finally {
    $("broadcast-send").disabled = false;
  }
};

/* ---------------- 定时配置 ---------------- */

async function loadScheduler() {
  $("sched-error").classList.add("hidden");
  try {
    const config = await api("/scheduler");
    state.schedulerVersion = config.version;
    $("sched-enabled").checked = !!config.enabled;
    state.schedTimes = new Set(config.times || []);
    $("sched-groups").value = (config.groups || []).join(",");
    $("sched-version").textContent = `当前版本 v${config.version}`;
    renderSchedTimes();
  } catch (e) {
    if (e.message !== "auth") toast("定时配置加载失败：" + e.message, "bad");
  }
}

function renderSchedTimes() {
  const el = $("sched-times");
  const times = Array.from(state.schedTimes).sort();
  el.innerHTML = times.length
    ? times.map((t) => `<span class="chip on" data-time="${esc(t)}">${esc(t)}<span class="x" title="移除">✕</span></span>`).join("")
    : emptyState("⏰", "尚未设置播报时间", "在上方选择时间后点击「添加时间」");
  el.querySelectorAll(".chip .x").forEach((x) => {
    x.onclick = (ev) => {
      ev.stopPropagation();
      const chip = x.parentElement;
      const t = chip.dataset.time;
      if (chip.classList.contains("confirming")) {
        // 确认窗口内再次点击 → 真正删除
        clearTimeout(chip._confirmTimer);
        state.schedTimes.delete(t);
        renderSchedTimes();
        return;
      }
      // 第一次点击 → 进入确认态，2 秒内再点 × 才删除，否则自动恢复
      chip.classList.add("confirming");
      toast(`再点一次 ✕ 确认删除 ${t}`, "warn", 2200);
      chip._confirmTimer = setTimeout(() => chip.classList.remove("confirming"), 2000);
    };
  });
}

$("sched-time-add").onclick = () => {
  const v = $("sched-time-input").value.trim();
  if (!/^\d{2}:\d{2}$/.test(v)) { toast("请选择有效时间", "warn"); return; }
  if (state.schedTimes.size >= 24) { toast("最多 24 个时间", "warn"); return; }
  state.schedTimes.add(v);
  $("sched-time-input").value = "";
  renderSchedTimes();
};

$("sched-save").onclick = async () => {
  $("sched-error").classList.add("hidden");
  const groups = $("sched-groups").value.split(/[，,]/).map((s) => s.trim()).filter(Boolean);
  if (!groups.length || groups.some((g) => !/^\d+$/.test(g) || +g <= 0)) {
    $("sched-error").textContent = "推送群必须是非空的正整数群号（逗号分隔）";
    $("sched-error").classList.remove("hidden");
    return;
  }
  if (!state.schedTimes.size) {
    $("sched-error").textContent = "至少保留一个播报时间";
    $("sched-error").classList.remove("hidden");
    return;
  }
  const body = {
    enabled: $("sched-enabled").checked,
    times: Array.from(state.schedTimes),
    groups: groups.map(Number),
    expected_version: state.schedulerVersion,
  };
  try {
    const config = await api("/scheduler", { method: "PUT", body: JSON.stringify(body) });
    state.schedulerVersion = config.version;
    $("sched-version").textContent = `当前版本 v${config.version}`;
    toast("定时配置已保存", "ok");
  } catch (e) {
    if (e.message === "auth") return;
    $("sched-error").textContent = String(e.message).includes("concurrently")
      ? "配置已被其他端更新（版本冲突），请刷新后重试"
      : "保存失败：" + e.message;
    $("sched-error").classList.remove("hidden");
  }
};

/* ---------------- 权限 ---------------- */

async function loadPermissions() {
  $("perm-list").innerHTML = skeletonRows(5, 4);
  try {
    const [features, perms] = await Promise.all([api("/permissions/features"), api("/permissions?limit=500")]);
    $("perm-feature").innerHTML = features.features.map((f) => `<option value="${esc(f.name)}">${esc(f.label)}</option>`).join("");
    $("perm-list").innerHTML = perms.permissions.length
      ? perms.permissions.map((p) =>
        `<tr><td>${p.user_id}</td><td>${esc(p.feature)}</td>` +
        `<td>${p.allowed ? '<span class="badge ok">允许</span>' : '<span class="badge bad">禁用</span>'}</td>` +
        `<td class="dim">${fmtTime(p.updated_at)}</td></tr>`).join("")
      : `<tr><td colspan="4">${emptyState("🔓", "没有覆盖配置", "所有用户默认允许全部功能")}</td></tr>`;
  } catch (e) {
    if (e.message !== "auth") { $("perm-list").innerHTML = `<tr><td colspan="4">${errorState(loadPermissions)}</td></tr>`; console.error(e); }
  }
}

$("perm-set").onclick = async () => {
  const user = $("perm-user").value.trim();
  const feature = $("perm-feature").value;
  const allowed = $("perm-allowed").value === "1";
  if (!user) { toast("请输入 QQ 号", "warn"); return; }
  const btn = $("perm-set");
  btn.classList.add("loading");
  btn.textContent = "设置中...";
  try {
    await api(`/permissions/${user}/${feature}`, { method: "PUT", body: JSON.stringify({ allowed }) });
    toast("权限已更新", "ok");
    loadPermissions();
  } catch (e) {
    if (e.message !== "auth") toast("设置失败：" + e.message, "bad");
  } finally {
    btn.classList.remove("loading");
    btn.textContent = "设置";
  }
};

$("perm-clear").onclick = async () => {
  const user = $("perm-user").value.trim();
  const feature = $("perm-feature").value;
  if (!user) { toast("请输入 QQ 号", "warn"); return; }
  try {
    await api(`/permissions/${user}/${feature}`, { method: "DELETE" });
    toast("已清除覆盖", "ok");
    loadPermissions();
  } catch (e) { if (e.message !== "auth") toast("清除失败：" + e.message, "bad"); }
};

/* ---------------- 审计 ---------------- */

async function loadUsage(reset) {
  if (reset) { state.usageOffset = 0; state.usageTotal = 0; }
  $("usage-list").innerHTML = skeletonRows(6, 6);
  try {
    const params = new URLSearchParams({ limit: "100", offset: String(state.usageOffset) });
    if (state.usageQuery.user) params.set("user", state.usageQuery.user);
    if (state.usageQuery.feature) params.set("feature", state.usageQuery.feature);
    const data = await api("/usage?" + params.toString());
    state.usageTotal = data.total;
    const end = Math.min(state.usageOffset + data.usage.length, data.total);
    $("usage-total").textContent = data.total
      ? `共 ${data.total} 条（第 ${state.usageOffset + 1}-${end} 条）`
      : "共 0 条";
    $("usage-prev").disabled = state.usageOffset <= 0;
    $("usage-next").disabled = state.usageOffset + 100 >= data.total;
    $("usage-list").innerHTML = data.usage.length
      ? data.usage.map((r) =>
        `<tr><td class="dim">${fmtTime(r.created_at)}</td><td>${r.user_id}</td><td class="col-narrow">${r.group_id || "-"}</td>` +
        `<td>${esc(r.feature)}</td><td class="mono wrap">${esc(r.input_text)}</td>` +
        `<td class="mono ellipsis" title="${esc(r.output_text || "")}">${esc(r.output_text || "")}</td></tr>`).join("")
      : `<tr><td colspan="6">${emptyState("📭", "没有匹配的审计记录", "试试放宽过滤条件")}</td></tr>`;
  } catch (e) {
    if (e.message !== "auth") { $("usage-list").innerHTML = `<tr><td colspan="6">${errorState(() => loadUsage(reset))}</td></tr>`; console.error(e); }
  }
}

$("usage-query").onclick = () => {
  state.usageQuery = { user: $("usage-user").value.trim(), feature: $("usage-feature").value };
  loadUsage(true);
};
$("usage-prev").onclick = () => { state.usageOffset = Math.max(0, state.usageOffset - 100); loadUsage(false); };
$("usage-next").onclick = () => { state.usageOffset += 100; loadUsage(false); };
$("usage-export").onclick = () => {
  const params = new URLSearchParams({ format: "csv" });
  if (state.usageQuery.user) params.set("user", state.usageQuery.user);
  if (state.usageQuery.feature) params.set("feature", state.usageQuery.feature);
  downloadCsv("/usage/export?" + params.toString(), "taffy-usage.csv");
};
$("perm-export").onclick = () => downloadCsv("/permissions/export?format=csv", "taffy-permissions.csv");

async function downloadCsv(path, filename) {
  try {
    const res = await fetch(API + path, { headers: { Authorization: `Bearer ${state.token}` } });
    if (res.status === 401) { showLogin(); return; }
    if (!res.ok) {
      const detail = await res.json().catch(() => {});
      toast(detail.detail || `导出失败（${res.status}）`, "bad");
      return;
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    link.click();
    URL.revokeObjectURL(url);
  } catch (e) { console.error(e); toast("导出失败，请稍后再试。", "bad"); }
}

/* ---------------- 监控（含代删） ---------------- */

const ALERT_STATUS = {
  pending: ["等待中", "warn"], claimed: ["执行中", "dim"],
  delivered: ["已触发", "ok"], failed: ["失败", "bad"],
};

async function loadMonitors() {
  $("watchlist-summary").innerHTML = skeletonRows(4, 3);
  $("alert-list").innerHTML = skeletonRows(6, 6);
  $("watchlist-detail").innerHTML = "";
  try {
    const [watch, alerts] = await Promise.all([api("/watchlist"), api("/alerts?limit=50")]);
    const entries = Object.entries(watch.watchlists || {});
    $("watchlist-summary").innerHTML = entries.length
      ? entries.map(([u, v]) =>
        `<tr><td>${esc(u)}</td><td>${v.count}</td><td><button class="btn sm" data-watch-user="${esc(u)}">查看明细</button></td></tr>`).join("")
      : `<tr><td colspan="3">${emptyState("⭐", "还没有用户自选", "用户使用「自选」命令后这里会列出")}</td></tr>`;
    $$("[data-watch-user]").forEach((b) => { b.onclick = () => loadWatchlistDetail(b.dataset.watchUser); });

    $("alert-list").innerHTML = (alerts.alerts || []).length
      ? alerts.alerts.map((a) => {
        const [label, cls] = ALERT_STATUS[a.status] || [esc(a.status), "dim"];
        const retryBtn = a.status === "failed"
          ? `<button class="btn sm" data-alert-retry="${esc(a.id)}" data-name="${esc(a.market_name)}">重试</button> `
          : "";
        return `<tr><td>${a.owner_user_id}</td><td class="wrap">${esc(a.market_name)}</td>` +
          `<td>${a.direction === "above" ? "涨到" : "跌到"}</td><td>¥${(a.target_price_cents / 100).toFixed(2)}</td>` +
          `<td><span class="badge ${cls}">${label}</span></td>` +
          `<td>${retryBtn}<button class="btn sm danger" data-alert-del="${esc(a.id)}" data-name="${esc(a.market_name)}">删除</button></td></tr>`;
      }).join("")
      : `<tr><td colspan="6">${emptyState("🔔", "暂无价格预警", "订阅「预警 涨到/跌到 饰品名 价格」后这里会出现记录")}</td></tr>`;
    $$("[data-alert-retry]").forEach((b) => {
      b.onclick = async () => {
        const okToGo = await confirmModal("重试预警", `确定重新投递预警「${b.dataset.name}」吗？`);
        if (!okToGo) return;
        try {
          await api(`/alerts/${b.dataset.alertRetry}/retry`, { method: "POST" });
          toast("预警已重新入队", "ok");
          loadMonitors();
        } catch (e) {
          if (e.message !== "auth") toast("重试失败：" + e.message, "bad");
        }
      };
    });
    $$("[data-alert-del]").forEach((b) => {
      b.onclick = async () => {
        const okToGo = await confirmModal("删除预警", `确定代用户删除预警「${b.dataset.name}」吗？此操作不可恢复。`);
        if (!okToGo) return;
        try {
          await api(`/alerts/${b.dataset.alertDel}`, { method: "DELETE" });
          toast("预警已删除", "ok");
          loadMonitors();
        } catch (e) { if (e.message !== "auth") toast("删除失败：" + e.message, "bad"); }
      };
    });
  } catch (e) {
    if (e.message !== "auth") {
      $("watchlist-summary").innerHTML = `<tr><td colspan="3">${errorState(loadMonitors)}</td></tr>`;
      $("alert-list").innerHTML = `<tr><td colspan="6">${errorState(loadMonitors)}</td></tr>`;
      console.error(e);
    }
  }
}

async function loadWatchlistDetail(user) {
  const box = $("watchlist-detail");
  box.innerHTML = skeletonRows(3, 3);
  try {
    const data = await api(`/watchlist?user=${encodeURIComponent(user)}`);
    box.innerHTML = `<div class="card" style="margin-top:14px">
      <h2>用户 ${esc(user)} 的自选（${data.count} 件）</h2>
      ${data.items.length
        ? `<div class="table-wrap"><table><thead><tr><th>#</th><th>饰品</th><th>操作</th></tr></thead><tbody>`
          + data.items.map((name, i) =>
            `<tr><td>${i + 1}</td><td class="wrap">${esc(name)}</td>` +
            `<td><button class="btn sm danger" data-watch-del="${i + 1}" data-watch-owner="${esc(user)}" data-name="${esc(name)}">删除</button></td></tr>`).join("")
          + `</tbody></table></div>`
        : emptyState("⭐", "该用户没有自选")}
    </div>`;
    box.querySelectorAll("[data-watch-del]").forEach((b) => {
      b.onclick = async () => {
        const okToGo = await confirmModal("删除自选", `确定从用户 ${b.dataset.watchOwner} 的自选中删除「${b.dataset.name}」吗？`);
        if (!okToGo) return;
        try {
          await api(`/watchlist/${encodeURIComponent(b.dataset.watchOwner)}/${b.dataset.watchDel}`, { method: "DELETE" });
          toast("自选已删除", "ok");
          loadMonitors();
        } catch (e) { if (e.message !== "auth") toast("删除失败：" + e.message, "bad"); }
      };
    });
  } catch (e) {
    if (e.message !== "auth") { box.innerHTML = `<p class="err">明细加载失败：${esc(e.message)}</p>`; }
  }
}

/* ---------------- 学习记忆 ---------------- */

const learningKind = { preference: "偏好", style: "风格", knowledge: "知识", cultural: "群文化" };
const learningStatusText = { pending: "待确认", accepted: "已接受", rejected: "已拒绝", archived: "已归档" };

async function loadLearning(reset) {
  if (reset) state.learningStatus = $("learning-status").value;
  $("learning-list").innerHTML = skeletonRows(5, 8);
  try {
    const params = new URLSearchParams({ limit: "200" });
    if (state.learningStatus) params.set("status", state.learningStatus);
    const [data, stats] = await Promise.all([
      api("/learning?" + params.toString()),
      api("/learning/stats"),
    ]);
    $("learning-stats").textContent = `（待确认 ${stats.pending} / 已接受 ${stats.accepted} / 已拒绝 ${stats.rejected} / 已归档 ${stats.archived}）`;
    $("learning-list").innerHTML = (data.insights || []).length
      ? data.insights.map((r) =>
        `<tr><td>${r.user_id}</td><td>${learningKind[r.kind] || esc(r.kind)}</td>` +
        `<td class="wrap">${esc(r.content)}</td><td class="mono wrap">${esc(r.evidence)}</td>` +
        `<td>${learningStatusText[r.status] || esc(r.status)}</td><td>${(Number(r.weight) || 0).toFixed(2)}</td>` +
        `<td class="dim">${fmtTime(r.created_at)}</td>` +
        `<td>${r.status === "pending" ? `<button class="btn sm" data-accept="${r.id}">确认</button> <button class="btn sm" data-reject="${r.id}">拒绝</button>` : ""}` +
        `${r.status === "accepted" ? `<button class="btn sm danger" data-del="${r.id}">删除</button>` : ""}</td></tr>`).join("")
      : `<tr><td colspan="8">${emptyState("🧠", "暂无学习印象", "确认后的印象会出现在这里")}</td></tr>`;
    $$("[data-accept]").forEach((b) => {
      b.onclick = async () => {
        try { await api(`/learning/${b.dataset.accept}/accept`, { method: "POST" }); toast("已确认", "ok"); loadLearning(true); }
        catch (e) { if (e.message !== "auth") toast("操作失败：" + e.message, "bad"); }
      };
    });
    $$("[data-reject]").forEach((b) => {
      b.onclick = async () => {
        try { await api(`/learning/${b.dataset.reject}/reject`, { method: "POST" }); toast("已拒绝", "ok"); loadLearning(true); }
        catch (e) { if (e.message !== "auth") toast("操作失败：" + e.message, "bad"); }
      };
    });
    $$("[data-del]").forEach((b) => {
      b.onclick = async () => {
        const okToGo = await confirmModal("删除印象", "确定物理删除这条已接受的印象吗？此操作不可恢复。");
        if (!okToGo) return;
        try { await api(`/learning/${b.dataset.del}`, { method: "DELETE" }); toast("已删除", "ok"); loadLearning(true); }
        catch (e) { if (e.message !== "auth") toast("删除失败：" + e.message, "bad"); }
      };
    });
  } catch (e) {
    if (e.message !== "auth") { $("learning-list").innerHTML = `<tr><td colspan="8">${errorState(() => loadLearning(reset))}</td></tr>`; console.error(e); }
  }
}

$("learning-query").onclick = () => loadLearning(true);

/* ---------------- 实时提醒（轮询 + 通知） ---------------- */

let pollTimer = null;
let pollPrimed = false; // 首次拉取只建立游标，不弹历史事件

async function pollEvents() {
  if (document.visibilityState !== "visible") return;
  enforceSessionTtl(); // 周期复检会话有效期，面板常开也会到期
  try {
    const params = new URLSearchParams({ limit: "50" });
    if (state.eventSince !== null) params.set("since", String(state.eventSince));
    const data = await api("/events?" + params.toString());
    if (data.latest > (state.eventSince ?? 0)) {
      state.eventSince = data.latest;
      if (pollPrimed) showNewEvents(data.events || []);
    }
    pollPrimed = true;
  } catch (e) {
    if (e.message === "auth") {
      stopEventPolling();
    } else {
      // A failed poll must not leave the cursor behind; reset it so the next
      // successful poll re-primes instead of re-counting stale events.
      state.eventSince = null;
      pollPrimed = false;
    }
  }
}

/* 服务端游标按秒且含边界，同一秒内后到的靠事件键去重 */
const seenEventKeys = new Set();

function showNewEvents(events) {
  const fresh = events.filter((ev) => !seenEventKeys.has(ev.key));
  fresh.forEach((ev) => seenEventKeys.add(ev.key));
  while (seenEventKeys.size > 600) {
    seenEventKeys.delete(seenEventKeys.values().next().value);
  }
  if (!fresh.length) return;
  state.eventCount += fresh.length;
  const badge = $("event-badge");
  badge.textContent = state.eventCount > 99 ? "99+" : String(state.eventCount);
  badge.classList.remove("hidden");
  const hidden = document.visibilityState !== "visible";
  fresh.slice(0, 3).forEach((ev) => {
    const text = `[${ev.kind}] ${ev.summary}`;
    if (hidden && "Notification" in window && Notification.permission === "granted") {
      try { new Notification("塔菲面板 · 新事件", { body: ev.summary }); } catch { /* ignore */ }
    } else {
      toast(text, ev.kind.includes("failed") ? "bad" : "info", 5000);
    }
  });
}

function startEventPolling() {
  stopEventPolling();
  state.eventSince = null;
  state.eventCount = 0;
  pollPrimed = false;
  $("event-badge").classList.add("hidden");
  pollTimer = setInterval(pollEvents, 10_000);
  pollEvents();
}

function stopEventPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && pollTimer) pollEvents();
});

$("notify-bell").onclick = async () => {
  if ("Notification" in window && Notification.permission === "default") {
    const permission = await Notification.requestPermission();
    toast(permission === "granted" ? "已开启浏览器通知" : "未授权浏览器通知（仍会显示页内提醒）", permission === "granted" ? "ok" : "warn");
  }
  state.eventCount = 0;
  $("event-badge").classList.add("hidden");
};

/* ---------------- 导航 / 登录 / 时钟 ---------------- */

$$(".side-item[data-tab]").forEach((b) => { b.onclick = () => switchTab(b.dataset.tab); });

function openSidebar() {
  $("sidebar").classList.add("open");
  $("sidebar-backdrop").classList.add("show");
  const hamburger = $("hamburger");
  hamburger.classList.add("open");
  hamburger.textContent = "✕";
}
function closeSidebar() {
  $("sidebar").classList.remove("open");
  $("sidebar-backdrop").classList.remove("show");
  const hamburger = $("hamburger");
  hamburger.classList.remove("open");
  hamburger.textContent = "☰";
}
$("hamburger").onclick = openSidebar;
$("sidebar-backdrop").onclick = closeSidebar;

$("login-btn").onclick = async () => {
  state.token = $("token-input").value.trim();
  $("login-err").classList.add("hidden");
  try {
    await api("/permissions/features");
    // Persist only after the token has been verified server-side.
    localStorage.setItem("taffy_token", state.token);
    localStorage.setItem("taffy_token_ts", String(Date.now()));
    showApp();
  } catch (e) {
    state.token = "";
    if (e.message === "auth") {
      $("login-err").textContent = "令牌无效或管理 API 未启用";
      $("login-err").classList.remove("hidden");
    } else {
      $("login-err").textContent = "网络错误，请稍后再试";
      $("login-err").classList.remove("hidden");
    }
  }
};

$("logout").onclick = () => {
  clearSession();
  showLogin();
};

setInterval(() => {
  const el = $("clock");
  if (el) el.textContent = new Date().toLocaleString("zh-CN", { hour12: false });
}, 1000);

// 会话过期检查：带时间戳的本地令牌超过 12 小时即作废。周期性复检（随事件
// 轮询），面板常开也会到期；旧版本无时间戳的令牌在下次登录时补上时间戳。
function enforceSessionTtl() {
  if (sessionExpired()) {
    clearSession();
    showLogin();
  }
}

enforceSessionTtl(); // 启动时立即复核一次本地令牌的时间戳

/* ---------------- 会话管理（仅运维用途） ---------------- */

let sessionDetailKey = null;

async function loadSessions() {
  $("session-detail-card").classList.add("hidden");
  $("session-detail").innerHTML = "";
  sessionDetailKey = null;
  $("session-list").innerHTML = skeletonRows(5, 4);
  try {
    const data = await api("/sessions?limit=100");
    const items = data.sessions || [];
    $("session-list").innerHTML = items.length
      ? items.map((s) => {
        const key = s.session_key || s.key || "";
        const msgCount = s.message_count != null ? s.message_count : "-";
        const ts = s.updated_at || s.created_at || 0;
        return `<tr><td class="wrap mono">${esc(key)}</td><td>${esc(String(msgCount))}</td>` +
          `<td class="dim">${fmtTime(ts)}</td>` +
          `<td><button class="btn sm" data-session-view="${esc(key)}">查看</button> ` +
          `<button class="btn sm danger" data-session-del="${esc(key)}">删除</button></td></tr>`;
      }).join("")
      : `<tr><td colspan="4">${emptyState("💬", "暂无会话", "用户与塔菲聊天后这里会出现会话记录")}</td></tr>`;
    $$("[data-session-view]").forEach((b) => {
      b.onclick = () => loadSessionDetail(b.dataset.sessionView);
    });
    $$("[data-session-del]").forEach((b) => {
      b.onclick = async () => {
        const okToGo = await confirmModal(
          "删除会话",
          `确定物理删除会话「${b.dataset.sessionDel}」吗？此操作不可恢复（含摘要）。`
        );
        if (!okToGo) return;
        try {
          await api(`/sessions/${encodeURIComponent(b.dataset.sessionDel)}`, { method: "DELETE" });
          toast("会话已删除", "ok");
          loadSessions();
        } catch (e) {
          if (e.message !== "auth") {
            // 409：会话正在被 LLM 使用，明确提示稍后再试。
            toast("删除失败：" + e.message + (e.message.includes("busy") ? "（会话繁忙，请稍后再试）" : ""), "bad", 6000);
          }
        }
      };
    });
  } catch (e) {
    if (e.message !== "auth") {
      $("session-list").innerHTML = `<tr><td colspan="4">${errorState(loadSessions)}</td></tr>`;
      console.error(e);
    }
  }
}

async function loadSessionDetail(key) {
  sessionDetailKey = key;
  const box = $("session-detail");
  $("session-detail-title").textContent = `会话详情：${key}`;
  $("session-detail-card").classList.remove("hidden");
  box.innerHTML = skeletonRows(5, 2);
  try {
    const data = await api(`/sessions/${encodeURIComponent(key)}`);
    const messages = data.messages || [];
    box.innerHTML = messages.length
      ? `<div class="table-wrap"><table><thead><tr><th>角色</th><th>内容</th></tr></thead><tbody>`
        + messages.map((m) =>
          `<tr><td>${m.role === "assistant" ? "🤖 塔菲" : "👤 用户"}</td><td class="wrap">${esc(m.content || "")}</td></tr>`
        ).join("") + `</tbody></table></div>`
      : emptyState("💬", "会话为空");
  } catch (e) {
    if (e.message !== "auth") {
      if (e.message.includes("404")) {
        box.innerHTML = `<p class="err">会话不存在或已被清理：${esc(key)}</p>`;
      } else {
        box.innerHTML = `<p class="err">加载失败：${esc(e.message)}</p>`;
      }
    }
  }
}

$("session-refresh").onclick = () => loadSessions();

(async () => {
  const features = await api("/permissions/features").catch(() => null);
  if (features) {
    $("usage-feature").innerHTML = `<option value="">全部功能</option>`
      + features.features.map((f) => `<option value="${esc(f.name)}">${esc(f.label)}</option>`).join("");
    showApp();
  } else {
    // 网络抖动或令牌失效都会走到这里；clearSession 已由 401/auth 路径处理，
    // 这里只切登录界面，不清除可能仍然有效的本地令牌。
    showLogin();
  }
})();
