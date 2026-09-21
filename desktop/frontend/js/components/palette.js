/* components/palette.js — Ctrl+K 全局命令面板（4.9 升级）：
   分组：快捷指令 / 页面跳转 / 操作 / 塔菲命令 / 物品搜索。
   ↑↓ 选择 · Enter 执行 · Esc 关闭 · 无结果态；列表项 aria-selected，输入框 aria-label。 */

import { get, post } from "../api.js";
import { navigate, triggerRefresh } from "../router.js";
import { store, applyTheme } from "../store.js";
import { toast, pushEsc } from "../ui.js";

const TAFFY_COMMANDS = [
  { label: "大盘", hint: "市场大盘概览", run: () => navigate("market") },
  { label: "抄底", hint: "抄底·大盘历史情景", run: () => navigate("market") },
  { label: "预测", hint: "短期走势预测", run: () => navigate("market") },
  { label: "我的预警", hint: "打开预警列表", run: () => navigate("alerts") },
  { label: "学习印象", hint: "审核塔菲的印象", run: () => navigate("persona") },
];

export default {
  name: "PaletteOverlay",
  props: {
    initialQuery: { type: String, default: "" },
  },
  emits: ["close"],
  data() {
    return {
      query: this.initialQuery,
      hl: 0,
      itemHits: [],
      searching: false,
    };
  },
  computed: {
    navPages() {
      const q = this.query.trim().toLowerCase();
      return PAGES.filter((p) => !q || p.label.toLowerCase().includes(q));
    },
    globalActions() {
      const q = this.query.trim().toLowerCase();
      return ACTIONS.filter((a) => !q || a.label.toLowerCase().includes(q));
    },
    commandHits() {
      const q = this.query.trim().toLowerCase();
      if (!q) return TAFFY_COMMANDS;
      return TAFFY_COMMANDS.filter((c) => c.label.toLowerCase().includes(q));
    },
    quickActionHits() {
      const list = store.settings.quick_actions || [];
      const q = this.query.trim().toLowerCase();
      if (!q) return list.slice(0, 4);
      return list.filter((a) => (a.name || "").toLowerCase().includes(q));
    },
    /* sections: [{ label, entries }]；entries 统一为扁平条目流供键盘索引 */
    sections() {
      return [
        {
          label: "快捷指令",
          icon: "⚡",
          entries: this.quickActionHits.map((a) => ({ type: "action", name: a.name, hint: "快捷指令", run: a })),
        },
        {
          label: "页面跳转",
          icon: "🧭",
          entries: this.navPages.map((p) => ({ type: "page", name: p.label, hint: p.hotkey ? `Ctrl+${p.hotkey}` : "打开页面", path: p.path })),
        },
        { label: "操作", icon: "🛠️", entries: this.globalActions.map((a) => ({ type: "op", name: a.label, hint: a.hint, op: a.op })) },
        { label: "塔菲命令", icon: "⌨️", entries: this.commandHits.map((c) => ({ type: "command", name: c.label, hint: c.hint, command: c })) },
        { label: "物品搜索", icon: "📦", entries: this.itemHits.map((i) => ({ type: "item", name: i.display_name, hint: "市场详情", marketName: i.market_name })) },
      ].filter((section) => section.entries.length);
    },
    flat() {
      const list = this.sections.flatMap((section) =>
        section.entries.map((entry) => ({ ...entry, groupLabel: section.label, groupIcon: section.icon }))
      );
      /* 把扁平序号回写到 section 条目，模板里直接用 entry._i 高亮（避免 indexOf 开销） */
      let n = 0;
      for (const section of this.sections) {
        for (const entry of section.entries) entry._i = n++;
      }
      return list;
    },
    hasResults() {
      return this.flat.length > 0;
    },
  },
  watch: {
    query(value) {
      this.hl = 0;
      this.searchItems(value);
    },
  },
  methods: {
    async searchItems(value) {
      const q = value.trim();
      if (!q || q.length < 2) {
        this.itemHits = [];
        return;
      }
      this.searching = true;
      const { body } = await get(`/api/desktop/market/search?q=${encodeURIComponent(q)}&limit=6`);
      this.itemHits = body && body.ok ? body.data.results : [];
      this.searching = false;
    },
    async pick(entry) {
      this.$emit("close");
      if (entry.type === "page") return navigate(entry.path);
      if (entry.type === "command") return entry.command.run();
      if (entry.type === "op") return entry.op();
      if (entry.type === "item") {
        sessionStorage.setItem("market-focus", entry.marketName);
        return navigate("market");
      }
      if (entry.type === "action") {
        const action = entry.run;
        if (action.kind === "chat") {
          let text = String(action.template || "");
          if (text.includes("{input}")) {
            // eslint-disable-next-line no-alert
            const input = window.prompt(action.name);
            if (!input) return;
            text = text.replace("{input}", input);
          }
          await post("/api/desktop/chat", { message: text });
          return navigate("chat");
        }
        toast(`已执行快捷指令：${action.name}`);
      }
    },
    onKey(event) {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        this.hl = Math.min(this.hl + 1, Math.max(this.flat.length - 1, 0));
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        this.hl = Math.max(this.hl - 1, 0);
      } else if (event.key === "Enter") {
        event.preventDefault();
        const entry = this.flat[this.hl];
        if (entry) this.pick(entry);
      }
    },
    scrollActiveIntoView() {
      this.$nextTick(() => {
        const el = this.$el && this.$el.querySelector(".palette-item.hl");
        el && el.scrollIntoView({ block: "nearest" });
      });
    },
  },
  mounted() {
    this.$refs.input && this.$refs.input.focus();
    if (this.query && this.query.trim().length >= 2) this.searchItems(this.query);
    this._offEsc = pushEsc(() => this.$emit("close"));
  },
  unmounted() {
    this._offEsc && this._offEsc();
  },
  updated() {
    this.scrollActiveIntoView();
  },
  template: `
  <div class="overlay" @click.self="$emit('close')">
    <div class="palette" role="dialog" aria-modal="true" aria-label="全局搜索" @keydown="onKey">
      <input ref="input" v-model="query" placeholder="搜索物品 / 输入命令 / 跳转页面…" spellcheck="false" aria-label="全局搜索" />
      <div class="palette-list" role="listbox" aria-label="搜索结果">
        <state-panel v-if="!hasResults" state="empty" icon="🔍" title="没有匹配结果"
                     message="试试页面名（如「预警」）、塔菲命令或物品关键词。" tight />
        <template v-for="section in sections" :key="section.label">
          <div class="palette-group-label" role="presentation">{{ section.icon }} {{ section.label }}</div>
          <div v-for="entry in section.entries" :key="entry.type + ':' + entry.name"
               class="palette-item" :class="{ hl: entry._i === hl }"
               role="option" :aria-selected="String(entry._i === hl)"
               @mouseenter="hl = entry._i" @click="pick(entry)">
            <span>{{ entry.type === 'item' ? '📦' : entry.type === 'action' ? '⚡' : entry.type === 'page' ? '🧭' : entry.type === 'op' ? '🛠️' : '⌨️' }}</span>
            <span>{{ entry.name }}</span>
            <span class="muted spacer"></span>
            <span class="muted">{{ entry.hint }}</span>
          </div>
        </template>
      </div>
      <div class="row muted palette-foot">
        <span>↑↓ 选择</span><span>Enter 执行</span><span>Esc 关闭</span>
      </div>
    </div>
  </div>
  `,
};

/* ── 数据源（模块级常量） ── */
const PAGES = [
  { path: "dashboard", label: "仪表盘", hotkey: 1 },
  { path: "chat", label: "聊天", hotkey: 2 },
  { path: "market", label: "市场", hotkey: 3 },
  { path: "alerts", label: "预警", hotkey: 4 },
  { path: "inventory", label: "库存监控", hotkey: 5 },
  { path: "users", label: "用户中心", hotkey: 6 },
  { path: "news", label: "资讯", hotkey: 7 },
  { path: "persona", label: "人格印象", hotkey: 8 },
  { path: "analytics", label: "数据分析", hotkey: 9 },
  { path: "ops", label: "运维", hotkey: "" },
  { path: "settings", label: "设置", hotkey: "" },
];

function nextScene() {
  const preset = ["早报速览", "盘中盯盘", "收盘复盘", "运维巡检", "默认"];
  const all = [...new Set([...preset, ...Object.keys(store.settings.scenes || {})])];
  const idx = all.indexOf(store.settings.active_scene);
  const next = all[(idx + 1) % all.length];
  store.settings.active_scene = next;
  const custom = (store.settings.scenes || {})[next] || {};
  window.dispatchEvent(new CustomEvent("scene-change", { detail: { name: next, config: custom } }));
  toast(`已切换场景：${next}`);
}

function toggleTheme() {
  store.settings.theme = store.settings.theme === "dark" ? "light" : "dark";
  applyTheme();
}

const ACTIONS = [
  { label: "刷新当前页", hint: "F5", op: () => triggerRefreshSafe() },
  { label: "切换主题", hint: "亮/暗", op: toggleTheme },
  { label: "切换场景", hint: "循环预设场景", op: nextScene },
];
