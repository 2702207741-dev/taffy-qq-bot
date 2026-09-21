/* router.js — hash 路由 + 键盘流（Ctrl+1..9 切换模块、F5 刷新当前页）。 */

import { store } from "./store.js";

export const routes = [
  { path: "dashboard", title: "仪表盘", icon: "🏠", view: null },
  { path: "chat", title: "聊天", icon: "💬", view: null },
  { path: "market", title: "市场", icon: "📈", view: null },
  { path: "alerts", title: "预警", icon: "🔔", view: null },
  { path: "inventory", title: "库存", icon: "📦", view: null },
  { path: "users", title: "用户", icon: "👥", view: null },
  { path: "news", title: "资讯", icon: "📰", view: null },
  { path: "persona", title: "人格印象", icon: "🐱", view: null },
  { path: "analytics", title: "数据", icon: "📊", view: null },
  { path: "ops", title: "运维", icon: "🛠️", view: null },
  { path: "settings", title: "设置", icon: "⚙️", view: null },
];

const listeners = new Set();

export function currentPath() {
  const hash = location.hash.replace(/^#\//, "") || "dashboard";
  return routes.some((r) => r.path === hash) ? hash : "dashboard";
}

export function navigate(path) {
  location.hash = `#/${path}`;
}

export function onRouteChange(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

let refreshHandler = null;
export function bindRefresh(fn) {
  refreshHandler = fn;
}
export function triggerRefresh() {
  if (refreshHandler) refreshHandler();
}

function onHashChange() {
  const path = currentPath();
  document.title = `${routes.find((r) => r.path === path)?.title || ""} · 塔菲桌面工作台`;
  listeners.forEach((fn) => fn(path));
}

export function installKeyboard({ openPalette, focusSearch }) {
  window.addEventListener("hashchange", onHashChange);

  window.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      openPalette();
      return;
    }
    if ((event.ctrlKey || event.metaKey) && event.key === "/") {
      // Ctrl+/ 聚焦顶栏全局搜索（第 7 节键盘流）
      event.preventDefault();
      focusSearch && focusSearch();
      return;
    }
    if ((event.ctrlKey || event.metaKey) && /^[1-9]$/.test(event.key)) {
      const index = Number(event.key) - 1;
      if (routes[index]) {
        event.preventDefault();
        navigate(routes[index].path);
      }
      return;
    }
    if (event.key === "F5") {
      event.preventDefault();
      triggerRefresh();
    }
  });
}
