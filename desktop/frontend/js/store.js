/* store.js — 全局响应式状态 + %APPDATA% 设置持久化（经 bridge）。 */

const { reactive, watch } = Vue;

function bridgeApi() {
  return window.pywebview && window.pywebview.api;
}

export const store = reactive({
  online: false,
  starting: false,
  tokenOk: false,
  supervisor: null,
  bridge_ready: true,   // false = pywebview 桥未就绪（D13 兜底提示）
  offline_reason: "",
  settings: {
    theme: "dark",
    accent: "warm",
    refresh_interval_seconds: 30,
    dashboard_tiles: ["index", "temperature", "watchlist", "alerts", "events"],
    scenes: {},
    active_scene: "默认",
    quick_actions: [],
    notify_rules: {},
    watchlist_groups: {},
    nav_collapsed: false,
    window: {},
    minimized_to_tray_on_close: false,
    remote_base_url: "",
  },
  refreshTimer: null,
});

export async function loadSettings() {
  try {
    const raw = await bridgeApi().load_settings();
    Object.assign(store.settings, JSON.parse(raw || "{}"));
  } catch (err) {
    /* 首次启动无设置文件，保持默认 */
  }
}

let saveHandle = null;
export function saveSettings() {
  clearTimeout(saveHandle);
  saveHandle = setTimeout(async () => {
    try {
      await bridgeApi().save_settings(JSON.stringify(store.settings));
    } catch (err) {
      /* 持久化失败不阻塞使用 */
    }
  }, 300);
}

watch(() => ({ ...store.settings }), saveSettings);

export function applyTheme() {
  document.documentElement.dataset.theme = store.settings.theme;
  document.documentElement.dataset.accent = store.settings.accent;
}

export function setOnline(online, starting = false) {
  store.online = online;
  store.starting = starting;
}
