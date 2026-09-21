/* main.js — 应用引导：bridge 就绪 → 设置 → 令牌校验 → 轮询状态 → 挂载根组件。
   UI V2：顶栏/侧栏重构（5.1/5.2）、全局搜索、Esc 层叠、主题切换视图重挂载。 */

import * as api from "./api.js";
import { store, loadSettings, applyTheme, setOnline } from "./store.js";
import { routes, currentPath, navigate, installKeyboard, bindRefresh, triggerRefresh, onRouteChange } from "./router.js";
import {
  toast, toastStateView, bindConfirm, dismissToast, TOAST_ICONS,
  consumeEscape, disposeCharts,
} from "./ui.js";

import DashboardView from "./views/dashboard.js";
import ChatView from "./views/chat.js";
import MarketView from "./views/market.js";
import AlertsView from "./views/alerts.js";
import InventoryView from "./views/inventory.js";
import UsersView from "./views/users.js";
import NewsView from "./views/news.js";
import PersonaView from "./views/persona.js";
import AnalyticsView from "./views/analytics.js";
import OpsView from "./views/ops.js";
import SettingsView from "./views/settings.js";

import PaletteOverlay from "./components/palette.js";
import ConfirmModal from "./components/confirm.js";
import PageHeader from "./components/page-header.js";
import StatePanel from "./components/state-panel.js";
import StatCard from "./components/stat-card.js";
import DataTable from "./components/data-table.js";
import AppTabs from "./components/tabs.js";
import ChartCard from "./components/chart-card.js";
import DrawerPanel from "./components/drawer.js";
import KVList from "./components/kv-list.js";
import StatusPill from "./components/status-pill.js";

const viewMap = {
  dashboard: DashboardView,
  chat: ChatView,
  market: MarketView,
  alerts: AlertsView,
  inventory: InventoryView,
  users: UsersView,
  news: NewsView,
  persona: PersonaView,
  analytics: AnalyticsView,
  ops: OpsView,
  settings: SettingsView,
};

/* 侧栏分组（5.2）：核心 / 数据 / 管理；hotkey 沿用 routes 原始顺序 Ctrl+1..9 */
const NAV_GROUPS = [
  { label: "核心", paths: ["dashboard", "chat", "market", "alerts"] },
  { label: "数据", paths: ["inventory", "news", "persona", "analytics"] },
  { label: "管理", paths: ["users", "ops", "settings"] },
];
const NAV_DOTS = { inventory: "新能力：囤货轨迹 / 囤货画像" };

const SCENE_PRESETS = ["早报速览", "盘中盯盘", "收盘复盘", "运维巡检", "默认"];

const App = {
  data() {
    return {
      path: currentPath(),
      paletteOpen: false,
      searchDraft: "",
      confirmState: {
        open: false,
        title: "",
        body: "",
        danger: false,
        requireType: "",
        okText: "确认执行",
        resolve: null,
      },
      toasts: toastStateView(),
      toastIcons: TOAST_ICONS,
      appVersion: "",
      store, // 模板需要直接访问全局 store
      viewEpoch: 0, // 主题/accent 切换后强制重挂载当前视图（图表换肤）
    };
  },
  computed: {
    currentView() {
      return viewMap[this.path] || DashboardView;
    },
    pageTitle() {
      return (routes.find((r) => r.path === this.path) || {}).title || "";
    },
    navGroups() {
      return NAV_GROUPS.map((group) => ({
        label: group.label,
        items: routes
          .filter((route) => group.paths.includes(route.path))
          .map((route) => ({
            ...route,
            active: this.path === route.path,
            hotkey: routes.indexOf(route) < 9 ? String(routes.indexOf(route) + 1) : "",
            dot: NAV_DOTS[route.path] && this.path !== route.path ? NAV_DOTS[route.path] : "",
          })),
      })).filter((group) => group.items.length);
    },
    scenes() {
      const custom = Object.keys(store.settings.scenes || {});
      return [...new Set([...SCENE_PRESETS, ...custom])];
    },
    /* 连接三态（5.1）：本机在线 / 远程在线(chip) / 离线 */
    remoteHost() {
      const raw = store.settings.remote_base_url;
      if (!raw) return "";
      try {
        return new URL(raw).host;
      } catch {
        return raw;
      }
    },
    connLabel() {
      if (!store.online) return "离线";
      return this.remoteHost ? "远程在线" : "本机在线";
    },
    connTone() {
      return store.online ? "ok" : "danger";
    },
    connHint() {
      if (!store.online) {
        return store.offline_reason
          ? `离线原因：${store.offline_reason}`
          : "无法连接塔菲，请检查 bot 进程或在「运维」页启动";
      }
      return this.remoteHost ? `数据来自 ${store.settings.remote_base_url}` : "数据来自本机塔菲服务";
    },
    bridgeDown() {
      return store.bridge_ready === false;
    },
  },
  watch: {
    /* 主题热切换 → dispose 图表并整视图重挂载（3.2：palette 随令牌刷新） */
    "store.settings.theme"() {
      this.retheme();
    },
    "store.settings.accent"() {
      this.retheme();
    },
  },
  mounted() {
    this._rethemeTimer = null;
    this.path = currentPath();
    api.on("status", (payload) => {
      setOnline(Boolean(payload && payload.online));
      if (payload && !payload.online && payload.reason != null) {
        store.offline_reason = String(payload.reason);
      }
    });
    api.on("unauthorized", () => {
      store.tokenOk = false;
      toast("令牌无效，请到「设置 → 令牌管理」重录", "error");
    });
    // 等 bridge 就绪 → 载入个性化设置 → 应用主题，再开始轮询。
    api.ready().then(async (ready) => {
      if (!ready) {
        store.bridge_ready = false; // D13：桥接失败全局兜底提示
        toast("桌面桥接未就绪，界面功能受限；请重启塔菲工作台", "error");
      }
      await loadSettings();
      applyTheme();
      try {
        store.tokenOk = await window.pywebview.api.has_token();
      } catch {
        store.tokenOk = false;
      }
      setInterval(this.pollStatus, 10000);
      this.pollStatus();
    });
    onRouteChange((path) => {
      this.path = path;
    });
    installKeyboard({
      openPalette: () => this.openPalette(),
      focusSearch: () => this.focusGlobalSearch(),
    });
    bindRefresh(() => window.dispatchEvent(new CustomEvent("view-refresh")));
    bindConfirm(
      (title, body, opts = {}) =>
        new Promise((resolve) => {
          this.confirmState = {
            open: true,
            title,
            body,
            danger: Boolean(opts.danger),
            requireType: opts.requireType || "",
            okText: opts.okText || "确认执行",
            resolve,
          };
        })
    );
    window.addEventListener("scene-change", (event) => {
      const { name } = event.detail || {};
      if (name === "盘中盯盘") navigate("market");
      else if (name === "早报速览") navigate("dashboard");
      else if (name === "收盘复盘") navigate("analytics");
      else if (name === "运维巡检") navigate("ops");
    });
    applyTheme(); // 先以默认主题着色，设置载入后覆盖
  },
  unmounted() {
    clearTimeout(this._rethemeTimer);
  },
  methods: {
    toggleNav() {
      store.settings.nav_collapsed = !store.settings.nav_collapsed;
    },
    go(path) {
      navigate(path);
    },
    openPalette(query = "") {
      this.searchDraft = query || "";
      this.paletteOpen = true;
    },
    focusGlobalSearch() {
      const el = this.$refs.globalSearchInput;
      el && el.focus();
    },
    applyScene(name) {
      store.settings.active_scene = name;
      const custom = (store.settings.scenes || {})[name] || {};
      window.dispatchEvent(new CustomEvent("scene-change", { detail: { name, config: custom } }));
      if (name === "默认") triggerRefresh();
      toast(`已切换场景：${name}`);
    },
    cycleTheme() {
      store.settings.theme = store.settings.theme === "dark" ? "light" : "dark";
      applyTheme();
    },
    retheme() {
      clearTimeout(this._rethemeTimer);
      // 等 CSS 变量落盘一帧再重建图表色板（dispose 后靠视图重挂载/refresh 重建）
      this._rethemeTimer = setTimeout(() => {
        disposeCharts();
        this.viewEpoch += 1;
        triggerRefresh();
      }, 60);
    },
    async pollStatus() {
      try {
        const state = await window.pywebview.api.get_state();
        // 关于页版本号来源：bridge 快照首次到达即注入。
        if (state && state.version) {
          window.__TAFFY_VERSION__ = state.version;
          this.appVersion = String(state.version);
        }
        setOnline(Boolean(state && state.online));
        store.supervisor = state && state.supervisor ? state.supervisor : null;
      } catch (err) {
        setOnline(false);
        store.offline_reason = String(err).includes("BridgeNotReady") ? "桌面桥接未就绪" : "";
      }
    },
  },
  template: `
  <div class="layout" :data-path="path">
    <aside class="sidebar" :class="{ collapsed: store.settings.nav_collapsed }">
      <div class="brand"><img class="brand-logo" src="assets/logo.svg" alt="塔菲" /><span class="brand-text">塔菲工作台</span></div>
      <nav class="nav" aria-label="主导航">
        <div v-for="g in navGroups" :key="g.label">
          <div class="nav-group-label">{{ g.label }}</div>
          <div v-for="r in g.items" :key="r.path"
               class="nav-item" :class="{ active: r.active }" role="link" tabindex="0"
               :data-tip="store.settings.nav_collapsed ? (r.title + (r.hotkey ? ' (Ctrl+' + r.hotkey + ')' : '')) : null"
               :title="(!store.settings.nav_collapsed && r.dot) ? r.dot : null"
               @click="go(r.path)" @keydown.enter.prevent="go(r.path)">
            <span class="ico" aria-hidden="true">{{ r.icon }}</span>
            <span class="nav-label">{{ r.title }}</span>
            <span v-if="r.dot" class="dot" aria-hidden="true"></span>
          </div>
        </div>
      </nav>
      <div class="side-foot">
        <span class="side-version">Taffy Studio<template v-if="appVersion"> · v{{ appVersion }}</template></span>
      </div>
    </aside>

    <div class="main">
      <header class="topbar">
        <button class="btn sm ghost icon" aria-label="折叠/展开侧栏" data-tip="折叠侧栏" @click="toggleNav">☰</button>

        <span class="conn-pill" role="status"><status-pill :label="connLabel" :tone="connTone" :title="connHint" /></span>
        <span v-if="remoteHost && store.online" class="remote-chip" :title="store.settings.remote_base_url">{{ remoteHost }}</span>

        <span class="topbar-title">{{ pageTitle }}</span>
        <div class="spacer"></div>

        <div class="global-search">
          <span aria-hidden="true">🔎</span>
          <input ref="globalSearchInput" v-model="searchDraft" type="text" placeholder="搜索页面 / 命令 / 物品…"
                 aria-label="全局搜索" spellcheck="false"
                 @keydown.enter.prevent="openPalette(searchDraft)" />
        </div>

        <span class="scene-label">场景</span>
        <select class="scene-switcher" :value="store.settings.active_scene" aria-label="切换场景"
                @change="applyScene($event.target.value)">
          <option v-for="s in scenes" :key="s" :value="s">{{ s }}</option>
        </select>
        <span class="kbd-hint" role="button" tabindex="0"
              @click="openPalette()" @keydown.enter.prevent="openPalette()">Ctrl K</span>
        <button class="theme-btn" :aria-label="store.settings.theme === 'dark' ? '切换亮色主题' : '切换暗色主题'"
                :title="store.settings.theme === 'dark' ? '切换亮色' : '切换暗色'"
                @click="cycleTheme">{{ store.settings.theme === 'dark' ? '🌙' : '☀️' }}</button>
      </header>

      <section class="content" :class="{ 'fill-mode': path === 'chat' }">
        <div v-if="bridgeDown" class="alert-strip mb-3 bridge-banner">
          ⛔ 桌面桥接未就绪 —— 请关闭后重新打开塔菲工作台；数据类页面将显示为离线状态。
        </div>
        <component :is="currentView" :key="path + ':' + viewEpoch" class="page-wrap" />
      </section>
    </div>

    <PaletteOverlay v-if="paletteOpen" :initial-query="searchDraft" @close="paletteOpen = false" />
    <ConfirmModal
      v-if="confirmState.open"
      :title="confirmState.title" :body="confirmState.body"
      :danger="confirmState.danger" :require-type="confirmState.requireType"
      :ok-text="confirmState.okText"
      @ok="confirmState.resolve(true); confirmState.open = false"
      @cancel="confirmState.resolve(false); confirmState.open = false"
    />
    <div class="toasts" aria-live="polite">
      <div v-for="t in toasts.items" :key="t.id" class="toast" :class="t.type" role="status">
        <span class="t-icon" aria-hidden="true">{{ toastIcons[t.type] || toastIcons.info }}</span>
        <span>{{ t.message }}</span>
        <button class="t-close" aria-label="关闭提醒" @click="dismissToast(t.id)">✕</button>
      </div>
    </div>
  </div>
  `,
};

/* 全局键盘：Esc 走浮层层叠栈（7）；Ctrl+/ 在 router.installKeyboard 内接入 */
window.addEventListener("keydown", (event) => {
  if (event.key === "Escape") consumeEscape();
});

const bootApp = Vue.createApp(App);
bootApp.component("PaletteOverlay", PaletteOverlay);
bootApp.component("ConfirmModal", ConfirmModal);
bootApp.component("PageHeader", PageHeader);
bootApp.component("StatePanel", StatePanel);
bootApp.component("StatCard", StatCard);
bootApp.component("DataTable", DataTable);
bootApp.component("AppTabs", AppTabs);
bootApp.component("ChartCard", ChartCard);
bootApp.component("DrawerPanel", DrawerPanel);
bootApp.component("KVList", KVList);
bootApp.component("StatusPill", StatusPill);
bootApp.mount("#app");

export {};
