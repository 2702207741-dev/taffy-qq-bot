/* ui.js — esc 转义、吐司、确认弹窗、浮层 Esc 层叠栈等共享工具（安全要求第 3/5 条）。 */

const toastState = Vue.reactive({ items: [] });
const MAX_TOASTS = 5;
let confirmHandler = null;
let toastUid = 0;

export function esc(value) {
  const div = document.createElement("div");
  div.textContent = String(value == null ? "" : value);
  return div.innerHTML;
}

/* 渲染纯文本（textContent），聊天消息等一律走此函数，禁 HTML。 */
export function asText(value) {
  return String(value == null ? "" : value);
}

export function toast(message, type = "info", ttl = 3600) {
  const id = ++toastUid;
  if (toastState.items.length >= MAX_TOASTS) toastState.items.shift(); // 同屏上限，挤掉最旧
  toastState.items.push({ id, message: asText(message), type });
  setTimeout(() => dismissToast(id), ttl);
}

export function dismissToast(id) {
  const index = toastState.items.findIndex((item) => item.id === id);
  if (index >= 0) toastState.items.splice(index, 1);
}

export function toastStateView() {
  return toastState;
}

export const TOAST_ICONS = { info: "ℹ️", success: "✅", warning: "⚠️", error: "⛔" };

export function bindConfirm(handler) {
  confirmHandler = handler;
}

/* opts: { danger?: bool, requireType?: string, okText?, cancelText? } —— 兼容旧两参调用。 */
export async function confirmBox(title, body, opts = {}) {
  if (!confirmHandler) return window.confirm(`${title}\n${body}`);
  return confirmHandler(title, body, opts);
}

/* ── 浮层 Esc 关闭栈：后开先关（搜索→弹窗→抽屉逐层退出） ── */
const escStack = [];
export function pushEsc(closer) {
  escStack.push(closer);
  return () => {
    const index = escStack.indexOf(closer);
    if (index >= 0) escStack.splice(index, 1);
  };
}
export function consumeEscape() {
  const closer = escStack[escStack.length - 1];
  if (!closer) return false;
  closer();
  return true;
}

export function fmtPrice(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return "--";
  return `¥${num.toFixed(2)}`;
}

export function fmtPercent(value, digits = 2) {
  const num = Number(value);
  if (!Number.isFinite(num)) return "--";
  return `${num >= 0 ? "+" : ""}${num.toFixed(digits)}%`;
}

export function fmtTime(epochSeconds) {
  const num = Number(epochSeconds);
  if (!Number.isFinite(num) || num <= 0) return "--";
  return new Date(num * 1000).toLocaleString("zh-CN", { hour12: false });
}

export function fmtLocalDate(value) {
  if (value instanceof Date) return value.toLocaleDateString("zh-CN");
  return String(value ?? "--");
}

export function downloadCsv(filename, rows) {
  const csvEscape = (cell) => {
    let text = cell == null ? "" : String(cell);
    if (/^[=+\-@]/.test(text)) text = `'${text}`;
    return `"${text.replace(/"/g, '""')}"`;
  };
  const content = rows.map((row) => row.map(csvEscape).join(",")).join("\r\n");
  const blob = new Blob([`\uFEFF${content}`], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

let echartsInstances = [];
export function trackChart(instance) {
  echartsInstances.push(instance);
}
export function disposeCharts() {
  echartsInstances.forEach((chart) => chart.dispose());
  echartsInstances = [];
}
