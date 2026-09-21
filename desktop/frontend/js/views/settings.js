/* views/settings.js — 设置（6.11）：外观 / 场景 / 快捷指令 / 通知规则 / 连接 / 令牌 / 关于。
   UI V2 样板视图：统一表单分区卡 + 表单校验态 + StatusPill + DataTable + KVList。 */

import { store, applyTheme } from "../store.js";
import { toast, confirmBox } from "../ui.js";

const REMOTE_URL_RE = /^https?:\/\/[^\s/:?#]+(:\d{1,5})?$/;

export default {
  data() {
    return {
      // 模板直接引用 store.settings.* 时，Vue options 模板只解析实例属性；
      // 不挂上会渲染崩溃（V0.2 起设置页即白屏，本次修复）。
      store,
      tokenInput: "",
      tokenVerifyState: "",
      tokenState: "", // valid | invalid | ''
      sceneName: "",
      sceneNameError: "",
      remoteUrl: "",
      remoteState: "",
      newAction: { name: "", kind: "chat", template: "" },
      actionError: "",
      presets: ["早报速览", "盘中盯盘", "收盘复盘", "运维巡检"],
    };
  },
  mounted() {
    this.remoteUrl = store.settings.remote_base_url || "";
    this.remoteState = this.remoteUrl
      ? `当前生效：${this.remoteUrl}（数据来自该地址）`
      : "当前为本地默认：http://127.0.0.1:8080（本机塔菲）";
  },
  computed: {
    remoteInvalid() {
      return this.remoteUrl.trim() !== "" && !REMOTE_URL_RE.test(this.remoteUrl.trim());
    },
    remoteHint() {
      if (this.remoteInvalid) return "⚠️ 地址不合法：仅支持 http(s)://host[:port]，不含路径与查询参数";
      return this.remoteState;
    },
    tokenTooShort() {
      const value = this.tokenInput.trim();
      return value !== "" && value.length < 32;
    },
    onlineTone() {
      return this.store.online ? "ok" : "danger";
    },
    version() {
      try {
        return window.__TAFFY_VERSION__ || "";
      } catch {
        return "";
      }
    },
    /* 快捷指令表列定义：render 返回 VNode 用 Vue.h 构造 */
    actionColumns() {
      const { h } = Vue;
      const self = this;
      return [
        { key: "name", label: "名称", minWidth: "110px" },
        {
          key: "kind",
          label: "类型",
          width: "96px",
          render: (row) => (row.kind === "chat" ? "聊天模板" : "命令宏"),
        },
        { key: "template", label: "内容", ellipsis: true, render: (row) => String(row.template || "") },
        {
          key: "_del",
          label: "",
          width: "64px",
          align: "right",
          render: (row) =>
            h(
              "button",
              {
                class: "btn sm danger",
                onClick: () => self.removeQuickAction(store.settings.quick_actions.indexOf(row)),
              },
              "删"
            ),
        },
      ];
    },
    actionRows() {
      return store.settings.quick_actions || [];
    },
    /* 通知规则表列定义 */
    notifyColumns() {
      const { h } = Vue;
      return [
        { key: "label", label: "事件类型", minWidth: "120px" },
        {
          key: "toast",
          label: "Toast",
          render: (row) =>
            h("input", {
              type: "checkbox",
              "aria-label": row.label + " Toast 开关",
              checked: this.ruleValue(row.key, "toast"),
              onChange: (e) => this.updateRule(row.key, "toast", e.target.checked),
            }),
        },
        {
          key: "sound",
          label: "声音",
          render: (row) =>
            h("input", {
              type: "checkbox",
              "aria-label": row.label + " 声音开关",
              checked: this.ruleValue(row.key, "sound"),
              onChange: (e) => this.updateRule(row.key, "sound", e.target.checked),
            }),
        },
        {
          key: "dnd_start",
          label: "免打扰开始",
          render: (row) =>
            h("input", {
              type: "time",
              value: (this.rules[row.key] || {}).dnd_start || "",
              "aria-label": row.label + " 免打扰开始时间",
              onChange: (e) => this.updateRule(row.key, "dnd_start", e.target.value),
            }),
        },
        {
          key: "dnd_end",
          label: "免打扰结束",
          render: (row) =>
            h("input", {
              type: "time",
              value: (this.rules[row.key] || {}).dnd_end || "",
              "aria-label": row.label + " 免打扰结束时间",
              onChange: (e) => this.updateRule(row.key, "dnd_end", e.target.value),
            }),
        },
      ];
    },
    notifyRows() {
      return [
        { key: "alert_delivered", label: "预警触发" },
        { key: "alert_failed", label: "预警失败" },
        { key: "job_failed", label: "后台任务失败" },
        { key: "new_reddit", label: "新爆料帖" },
        { key: "watchlist_low", label: "自选新低（预留）" },
      ];
    },
    customScenes() {
      return Object.keys(store.settings.scenes || {});
    },
    rules() {
      return store.settings.notify_rules || {};
    },
  },
  methods: {
    openChangelog() {
      window.pywebview.api
        .open_external("https://github.com")
        .then((result) => {
          if (result !== "ok") toast("外部链接仅支持 https", "error");
        })
        .catch(() => {});
    },
    setTheme(theme) {
      store.settings.theme = theme;
      applyTheme();
      if (theme === "system") {
        const dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
        document.documentElement.dataset.theme = dark ? "dark" : "light";
      }
    },
    async saveToken() {
      const candidate = this.tokenInput.trim();
      if (!candidate) return;
      if (this.tokenTooShort) {
        this.tokenState = "invalid";
        this.tokenVerifyState = "令牌长度不足：ADMIN_API_TOKEN 通常 ≥32 字符，请检查粘贴完整性";
        toast(this.tokenVerifyState, "error");
        return;
      }
      this.tokenState = "";
      this.tokenVerifyState = "校验中…";
      const result = await window.pywebview.api.verify_and_save_token(candidate);
      if (result && result.saved) {
        this.tokenState = "valid";
        this.tokenVerifyState = `已保存（${result.sink === "keyring" ? "DPAPI 凭据库" : "内存降级"}）`;
        this.tokenInput = "";
        toast("令牌已保存", "success");
        setTimeout(() => location.reload(), 800);
      } else {
        this.tokenState = "invalid";
        this.tokenVerifyState = this.humanReason(result);
        toast(this.tokenVerifyState, "error");
      }
    },
    async importFromEnv() {
      this.tokenVerifyState = "正在从 bot 配置读取并校验…";
      const result = await window.pywebview.api.import_token_from_bot_env();
      if (result && result.saved) {
        this.tokenState = "valid";
        this.tokenVerifyState = `导入成功（${result.sink === "keyring" ? "DPAPI 凭据库" : "内存降级"}）`;
        toast("令牌一键导入成功，数据马上就位", "success");
        setTimeout(() => location.reload(), 800);
      } else {
        this.tokenState = "invalid";
        this.tokenVerifyState = this.humanReason(result);
        toast(this.tokenVerifyState, "error");
      }
    },
    humanReason(result) {
      if (!result) return "调用失败：bridge 未就绪";
      const map = {
        invalid: "校验失败：令牌与 bot 端不一致，或 bot 未运行",
        not_in_env: "仓库 .env 里没有 ADMIN_API_TOKEN",
        no_repo: "未找到 bot 仓库根",
      };
      return map[result.reason] || `失败：${result.reason || "未知"}`;
    },
    async clearSaved() {
      const okClear = await confirmBox("清除已存令牌", "桌面端将无法访问 bot API，直至重新录入。", { danger: true });
      if (!okClear) return;
      await window.pywebview.api.clear_saved_token();
      this.tokenState = "";
      this.tokenVerifyState = "已清除";
      toast("令牌已清除");
    },
    async toggleAutostart(event) {
      store.settings.autostart = event.target.checked;
      // 开机自启通过「启动文件夹快捷方式」实现，打包模式由壳层完成
      if (event.target.checked) {
        toast("将在下次打包版中写入启动项（当前为开发模式标记）");
      }
    },
    async saveScene() {
      const name = this.sceneName.trim();
      if (!name) {
        this.sceneNameError = "请输入场景名";
        return;
      }
      this.sceneNameError = "";
      const okSave = await confirmBox(
        "另存自定义场景",
        `将当前模块可见性 / 刷新间隔 / 通知规则快照保存为「${name}」`
      );
      if (!okSave) return;
      store.settings.scenes = store.settings.scenes || {};
      store.settings.scenes[name] = {
        snapshot: {
          refresh_interval_seconds: store.settings.refresh_interval_seconds,
          dashboard_tiles: [...store.settings.dashboard_tiles],
          active_theme: store.settings.theme,
        },
        saved_at: Date.now(),
      };
      toast(`场景「${name}」已保存`, "success");
      this.sceneName = "";
    },
    loadScene(name) {
      const config = (store.settings.scenes || {})[name];
      if (!config) {
        window.dispatchEvent(new CustomEvent("scene-change", { detail: { name } }));
        return;
      }
      Object.assign(store.settings, config.snapshot);
      applyTheme();
      window.dispatchEvent(new CustomEvent("scene-change", { detail: { name, config } }));
      toast(`已加载场景「${name}」`);
    },
    async deleteScene(name) {
      const okDelete = await confirmBox("删除场景", `确定删除自定义场景「${name}」？`, { danger: true });
      if (!okDelete) return;
      delete store.settings.scenes[name];
    },
    addQuickAction() {
      const action = { ...this.newAction };
      if (!action.name.trim()) {
        this.actionError = "请填写指令名称";
        return;
      }
      this.actionError = "";
      store.settings.quick_actions = store.settings.quick_actions || [];
      store.settings.quick_actions.push(action);
      this.newAction = { name: "", kind: "chat", template: "" };
      toast("快捷指令已添加（Ctrl+K 可见）", "success");
    },
    removeQuickAction(index) {
      store.settings.quick_actions.splice(index, 1);
    },
    updateRule(kind, field, value) {
      store.settings.notify_rules = store.settings.notify_rules || {};
      store.settings.notify_rules[kind] = {
        ...(store.settings.notify_rules[kind] || {}),
        [field]: value,
      };
    },
    ruleValue(kind, field) {
      const rule = this.rules[kind] || {};
      return rule[field] !== undefined ? rule[field] : field === "toast";
    },
    async saveRemoteUrl() {
      const candidate = this.remoteUrl.trim();
      if (candidate && !REMOTE_URL_RE.test(candidate)) {
        this.remoteState = "地址不合法：远程必须 HTTPS；HTTP 仅支持回环 IP（如 127.0.0.1），不能含路径或查询";
        toast(this.remoteState, "error");
        return;
      }
      const result = await window.pywebview.api.set_remote_base_url(candidate);
      if (result && result.ok) {
        store.settings.remote_base_url = result.base_url || "";
        this.remoteState = result.base_url
          ? `已生效：${result.base_url}（数据来自该地址）`
          : "已恢复为本地默认（本机塔菲）";
        toast(result.base_url ? `远程地址已生效：${result.base_url}` : "已恢复为本机塔菲", "success");
        // 触发状态刷新：重新载入后所有请求走新地址。
        setTimeout(() => location.reload(), 800);
      } else {
        const reason =
          result && result.reason === "invalid"
            ? "地址不合法：远程必须 HTTPS；HTTP 仅支持回环 IP（如 127.0.0.1），不能含路径或查询"
            : `保存失败：${(result && result.reason) || "未知"}`;
        this.remoteState = reason;
        toast(reason, "error");
      }
    },
    resetRemoteUrl() {
      this.remoteUrl = "";
      this.saveRemoteUrl();
    },
  },
  template: `
  <div>
    <page-header title="设置"
                 subtitle="个性化 · 场景 · 连接与安全 —— 改动实时持久化到本机">
      <template #meta>
        <status-pill v-if="version" :label="'v' + version" tone="neutral" :dot="false" />
      </template>
    </page-header>

    <!-- 外观 -->
    <div class="card mb-3">
      <h3>🎨 外观</h3>
      <div class="row wrap mb-3 gap-4">
        <label class="checkbox-row"><input type="radio" value="light" name="cfg-theme" :checked="store.settings.theme==='light'" @change="setTheme('light')" /> 亮色</label>
        <label class="checkbox-row"><input type="radio" value="dark" name="cfg-theme" :checked="store.settings.theme==='dark'" @change="setTheme('dark')" /> 暗色</label>
        <label class="checkbox-row"><input type="radio" value="system" name="cfg-theme" :checked="false" @change="setTheme('system')" /> 跟随系统</label>
        <span class="muted">｜主色</span>
        <select v-model="store.settings.accent" aria-label="主色选择" style="width:auto">
          <option value="warm">暖橙</option><option value="rose">玫瑰</option>
          <option value="honey">蜜金</option><option value="cream">奶油</option>
        </select>
      </div>
      <div class="row wrap gap-5">
        <div class="form-row mb-0">
          <label for="cfg-refresh">整页刷新间隔（秒）</label>
          <input id="cfg-refresh" type="number" v-model.number="store.settings.refresh_interval_seconds"
                 min="10" max="600" style="width:120px" />
        </div>
        <label class="checkbox-row self-end">
          <input type="checkbox" :checked="store.settings.autostart" @change="toggleAutostart" /> 开机自启
        </label>
      </div>
    </div>

    <!-- 场景管理 -->
    <div class="card mb-3">
      <h3>🎬 场景管理</h3>
      <div class="row wrap mb-2" style="gap:6px">
        <button class="btn sm ghost" v-for="p in presets" :key="p" @click="loadScene(p)">🚀 {{ p }}</button>
        <span v-for="name in customScenes" :key="name"
              class="pill info" style="cursor:pointer" role="button" tabindex="0"
              title="点击加载该场景"
              @click="loadScene(name)" @keydown.enter.prevent="loadScene(name)">
          📌 {{ name }}
          <b class="chip-x" role="button" :aria-label="'删除场景 ' + name" tabindex="0"
             @click.stop="deleteScene(name)" @keydown.enter.stop.prevent="deleteScene(name)">✕</b>
        </span>
      </div>
      <div class="row wrap">
        <input type="text" v-model="sceneName" placeholder="把当前布局/间隔/通知规则另存为…"
               :class="{ 'is-invalid': !!sceneNameError }" style="width:260px"
               @keydown.enter="saveScene" />
        <button class="btn sm" @click="saveScene">另存为自定义场景</button>
      </div>
      <p v-if="sceneNameError" class="field-msg err mt-1" role="alert">{{ sceneNameError }}</p>
    </div>

    <!-- 快捷指令编辑器 -->
    <div class="card mb-3">
      <h3>⚡ 快捷指令编辑器 <span style="font-weight:400; color:var(--text-faint)">（Ctrl+K 面板可见）</span></h3>
      <data-table :columns="actionColumns" :rows="actionRows" row-key="name"
                  empty-title="还没有快捷指令"
                  empty-text="在下方输入名称与模板即可添加；聊天模板支持 {input} 占位符。"
                  empty-icon="⚡" />
      <div class="row wrap mt-3">
        <input type="text" v-model="newAction.name" placeholder="名称"
               :class="{ 'is-invalid': !!actionError }" style="width:140px" @keydown.enter="addQuickAction" />
        <select v-model="newAction.kind" aria-label="指令类型" style="width:auto">
          <option value="chat">聊天模板</option><option value="command">命令宏</option>
        </select>
        <input type="text" v-model="newAction.template" placeholder='模板，{input} 为占位符' class="grow"
               spellcheck="false" @keydown.enter="addQuickAction" />
        <button class="btn sm" @click="addQuickAction">＋ 添加</button>
      </div>
      <p v-if="actionError" class="field-msg err mt-1" role="alert">{{ actionError }}</p>
    </div>

    <!-- 通知规则中心 -->
    <div class="card mb-3">
      <h3>🔔 通知规则中心</h3>
      <data-table :columns="notifyColumns" :rows="notifyRows" row-key="key" />
      <p class="faint mt-2">同类事件 5 分钟内自动聚合为「等 N 条」；免打扰时段仅记录不弹窗。</p>
    </div>

    <!-- 连接 -->
    <div class="card mb-3">
      <h3>🔗 连接（远程地址）
        <status-pill class="ml-1" :label="store.online ? '在线' : '离线'" :tone="onlineTone"
                     title="当前连接状态" />
      </h3>
      <kv-list class="mb-2" :pairs="[
        { k: '生效地址', v: store.settings.remote_base_url || '本机默认 http://127.0.0.1:8080' },
        { k: '使用说明', v: '留空=本机塔菲；远程填 SSH 隧道地址（如 http://127.0.0.1:18080），保存后立即生效' },
      ]" />
      <div class="row wrap">
        <input type="text" v-model="remoteUrl" placeholder="http://127.0.0.1:18080"
               :class="{ 'is-invalid': remoteInvalid }" spellcheck="false" class="grow"
               @keydown.enter="saveRemoteUrl" />
        <button class="btn" @click="saveRemoteUrl">保存并切换</button>
        <button class="btn ghost" @click="resetRemoteUrl">恢复默认（本机）</button>
      </div>
      <p class="field-msg" :class="remoteInvalid ? 'err' : 'hint'" role="status">{{ remoteHint }}</p>
    </div>

    <!-- 令牌管理 -->
    <div class="card mb-3">
      <h3>🔑 令牌管理</h3>
      <div class="row wrap mb-2">
        <button class="btn" @click="importFromEnv">⚡ 从 bot 配置一键导入（推荐）</button>
        <span class="muted">自动读取仓库 .env 的 ADMIN_API_TOKEN，校验后入 DPAPI，无需手动复制</span>
      </div>
      <div class="row wrap">
        <input type="password" v-model="tokenInput" placeholder="或手动粘贴 ADMIN_API_TOKEN（≥32 字符）"
               :class="{ 'is-invalid': tokenTooShort || tokenState === 'invalid' }"
               autocomplete="off" spellcheck="false" class="grow"
               @keydown.enter="saveToken" />
        <button class="btn ghost" :disabled="!tokenInput.trim()" @click="saveToken">校验并保存</button>
        <button class="btn ghost" @click="clearSaved">清除已存</button>
      </div>
      <p class="field-msg" :class="(tokenTooShort || tokenState === 'invalid') ? 'err' : 'hint'"
         :role="(tokenTooShort || tokenState === 'invalid') ? 'alert' : 'status'">
        {{ tokenVerifyState ||
           (tokenTooShort ? '长度不足 32 字符，疑似粘贴不完整' : '令牌只存 Windows DPAPI 凭据库；日志与页面均不显示明文。') }}
      </p>
    </div>

    <!-- 关于 -->
    <div class="card">
      <h3>🐱 关于</h3>
      <p>塔菲桌面工作台 Taffy Desktop Studio<template v-if="version"> · v{{ version }}</template></p>
      <p class="muted mt-1">单主人单机工具：与 QQ 端塔菲共享同一份人格记忆；请求默认指向本机 127.0.0.1，配置远程地址后经 SSH 隧道直连云端。</p>
      <button class="btn ghost sm mt-2" @click="openChangelog">查看 CHANGELOG 摘要 ↗</button>
    </div>
  </div>
  `,
};
