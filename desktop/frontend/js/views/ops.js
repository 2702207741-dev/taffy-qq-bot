/* views/ops.js — 运维（6.9）：进程看护（停止需输入「停止」确认）/ 状态总览 / 日志尾随 /
   事件流 / 广播与定时 / 权限 / 使用审计；全部表格 DataTable 化。 */

import { get, admin } from "../api.js";
import { store } from "../store.js";
import { toast, confirmBox, fmtTime } from "../ui.js";

export default {
  data() {
    return {
      tab: "status",
      summary: null,
      loading: false,
      logs: "",
      logLines: 120,
      logLevelFilter: "",
      logLoading: false,
      events: [],
      eventsLoading: false,
      schedulerCfg: null,
      permissions: null,
      auditRows: null,
      broadcastText: "",
      broadcastGroups: "",
      broadcastResult: null,
      tailTimer: null,
      botActionLoading: "", // start | restart | stop
    };
  },
  computed: {
    supervisor() {
      return store.supervisor;
    },
    botStateLabel() {
      const map = {
        running: "运行中",
        starting: "启动中（冷启动 30-90s）",
        stopped: "已停止",
        foreign_port: "⚠️ 端口被占用",
      };
      return this.supervisor ? map[this.supervisor.bot_state] || this.supervisor.bot_state : "—";
    },
    botStateTone() {
      if (!this.supervisor) return "neutral";
      if (this.supervisor.bot_state === "running") return "ok";
      if (this.supervisor.bot_state === "starting") return "warn";
      return "danger";
    },
    foreignPort() {
      return Boolean(this.supervisor && this.supervisor.bot_state === "foreign_port");
    },
    filteredLogs() {
      if (!this.logLevelFilter) return this.logs;
      return this.logs
        .split("\n")
        .filter((line) => line.includes(this.logLevelFilter))
        .join("\n");
    },
    healthPairs() {
      if (!this.summary) return [];
      const checks = this.summary.health.checks || {};
      const rows = [];
      for (const [name, check] of Object.entries(checks)) {
        if (name === "scheduler") continue;
        rows.push({ id: name, name, status: check.status });
      }
      return rows;
    },
    schedulerTasks() {
      if (!this.summary) return [];
      const tasks = this.summary.health.checks.scheduler.tasks || {};
      return Object.entries(tasks).map(([name, state]) => ({ id: name, name, state }));
    },
    healthColumns() {
      const { h } = Vue;
      return [
        { key: "name", label: "检查项" },
        {
          key: "status",
          label: "状态",
          width: "120px",
          render: (row) =>
            h("span", { class: `badge ${row.status === "pass" ? "ok" : "danger"}` }, row.status),
        },
      ];
    },
    taskColumns() {
      const { h } = Vue;
      return [
        { key: "name", label: "任务" },
        {
          key: "state",
          label: "状态",
          width: "120px",
          render: (row) =>
            h("span", { class: `badge ${row.state === "running" ? "ok" : ""}` }, row.state),
        },
      ];
    },
    featureColumns() {
      return [
        { key: "name", label: "功能名", width: "150px" },
        { key: "label", label: "说明" },
      ];
    },
    overrideColumns() {
      const { h } = Vue;
      const self = this;
      return [
        { key: "user_id", label: "用户", width: "130px" },
        { key: "feature", label: "功能" },
        {
          key: "_allowed",
          label: "允许",
          width: "70px",
          render: (row) =>
            h("input", {
              type: "checkbox",
              "aria-label": `允许 ${row.user_id} 使用 ${row.feature}`,
              checked: row.allowed,
              onChange: (e) => self.setPermission(row.user_id, row.feature, e.target.checked),
            }),
        },
        {
          key: "_clear",
          label: "",
          width: "72px",
          align: "right",
          render: (row) =>
            h("button", { class: "btn sm ghost", onClick: () => self.clearPermission(row) }, "清除"),
        },
      ];
    },
    auditColumns() {
      return [
        { key: "id", label: "#", width: "70px" },
        { key: "user_id", label: "用户", width: "130px" },
        { key: "feature", label: "功能", width: "110px" },
        { key: "command", label: "命令", width: "120px" },
        { key: "input_text", label: "输入摘要", ellipsis: true, render: (row) => String(row.input_text ?? "") },
        { key: "created_at", label: "时间", render: (row) => fmtTime(row.created_at) },
      ];
    },
    auditUsage() {
      return (this.auditRows && this.auditRows.usage) || [];
    },
  },
  mounted() {
    this.loadStatus();
    this.onRefresh = () => this.loadStatus();
    window.addEventListener("view-refresh", this.onRefresh);
  },
  unmounted() {
    window.removeEventListener("view-refresh", this.onRefresh);
    clearInterval(this.tailTimer);
  },
  methods: {
    fmtTime,
    async loadStatus() {
      this.loading = !this.summary;
      const res = await get("/api/desktop/system/summary");
      this.summary = res.status === 200 && res.body.ok ? res.body.data : null;
      if (this.summary) this.schedulerCfg = this.summary.scheduler || null;
      this.loading = false;
    },
    switchTab(tab) {
      this.tab = tab;
      clearInterval(this.tailTimer);
      if (tab === "logs") {
        this.refreshLogs();
        this.tailTimer = setInterval(this.refreshLogs, 5000); // 日志尾随
      } else if (tab === "events") this.loadEvents();
      else if (tab === "scheduler") this.loadScheduler();
      else if (tab === "permissions") this.loadPermissions();
      else if (tab === "audit") this.loadAudit();
    },
    async refreshLogs() {
      this.logLoading = true;
      const payload = await admin("GET", `/api/admin/logs?lines=${this.logLines}`);
      if (payload && typeof payload.content === "string") this.logs = payload.content;
      this.logLoading = false;
    },
    async loadEvents() {
      this.eventsLoading = !this.events.length;
      const payload = await admin("GET", "/api/admin/events?limit=100");
      this.events = payload ? payload.events || [] : [];
      this.eventsLoading = false;
    },
    async loadScheduler() {
      await this.loadStatus();
    },
    async loadPermissions() {
      const features = await admin("GET", "/api/admin/permissions/features");
      const overrides = await admin("GET", "/api/admin/permissions?limit=200");
      this.permissions = {
        features: features ? features.features : [],
        overrides: overrides ? overrides.permissions : [],
      };
    },
    async setPermission(userId, feature, allowed) {
      const okSet = await confirmBox(
        "修改权限",
        `用户 ${userId} 的「${feature}」将被设为 ${allowed ? "允许" : "拒绝"}。`
      );
      if (!okSet) return;
      const ok = await admin("PUT", `/api/admin/permissions/${userId}/${feature}`, { allowed });
      toast(ok ? "权限已更新" : "更新失败", ok ? "success" : "error");
      this.loadPermissions();
    },
    async clearPermission(item) {
      const okClear = await confirmBox("清除覆盖", `恢复「${item.feature}」对用户 ${item.user_id} 的默认权限？`);
      if (!okClear) return;
      const ok = await admin("DELETE", `/api/admin/permissions/${item.user_id}/${item.feature}`);
      toast(ok ? "已恢复默认" : "操作失败", ok ? "success" : "error");
      this.loadPermissions();
    },
    async loadAudit() {
      const payload = await admin("GET", "/api/admin/usage?limit=200");
      this.auditRows = payload || null;
    },
    async sendBroadcast() {
      const groups = this.broadcastGroups
        .split(/[,，\s]+/)
        .map((g) => Number(g.trim()))
        .filter((n) => Number.isInteger(n) && n > 0);
      const text = this.broadcastText.trim();
      if (!groups.length || !text) {
        toast("群号与内容不能为空", "error");
        return;
      }
      const okSend = await confirmBox("手动广播", `将向 ${groups.length} 个群发送：${text.slice(0, 60)}…`, {
        danger: true,
      });
      if (!okSend) return;
      const result = await admin("POST", "/api/admin/broadcast", { group_ids: groups, payload_text: text });
      this.broadcastResult = result;
      if (result) toast(`广播完成：成功 ${result.succeeded}/${result.total}`, "success");
    },
    async saveScheduler() {
      const cfg = this.schedulerCfg;
      if (!cfg) return;
      const okSave = await confirmBox(
        "保存定时配置",
        `启用=${cfg.enabled ? "是" : "否"}，时间点 ${(cfg.times || []).join(" ")}，群数 ${(cfg.groups || []).length}`
      );
      if (!okSave) return;
      const updated = await admin("PUT", "/api/admin/scheduler", {
        enabled: !!cfg.enabled,
        times: cfg.times,
        groups: cfg.groups,
        expected_version: cfg.version,
      });
      if (updated) {
        this.schedulerCfg = { ...this.schedulerCfg, ...updated };
        toast("定时配置已保存", "success");
      } else toast("保存失败（可能版本冲突，请刷新重试）", "error");
    },
    addTime() {
      // eslint-disable-next-line no-alert
      const t = window.prompt("新增时间点（HH:MM）");
      if (t && /^\d{2}:\d{2}$/.test(t) && this.schedulerCfg) {
        this.schedulerCfg.times = [...(this.schedulerCfg.times || []), t].sort();
      }
    },
    /* ── 进程看护卡（supervisor 提供） ── */
    async botAction(action) {
      this.botActionLoading = action;
      try {
        if (action === "start") {
          const result = await window.pywebview.api.start_bot();
          toast(
            result === "started"
              ? "塔菲启动中…已开始计时"
              : result === "port_foreign"
                ? "端口被无关进程占用，禁止启动"
                : result === "already_running"
                  ? "塔菲已在运行"
                  : `结果：${result}`,
            result === "started" || result === "already_running" ? "info" : "error"
          );
        } else if (action === "stop") {
          // 高危：先确认（输入「停止」），后触碰桥接
          const okStop = await confirmBox(
            "停止塔菲",
            "QQ 端服务将下线，群内命令立即失效。此操作影响所有正在使用的用户。",
            { danger: true, requireType: "停止", okText: "确认停止" }
          );
          if (!okStop) return;
          const result = await window.pywebview.api.stop_bot();
          toast(result === "stopped" ? "已停止" : "停止超时，请检查进程", result === "stopped" ? "warning" : "error");
        } else if (action === "restart") {
          const okRestart = await confirmBox("重启塔菲", "冷启动约需 30-90 秒。确定重启？", { danger: true });
          if (!okRestart) return;
          toast(await window.pywebview.api.restart_bot());
        }
      } catch (err) {
        toast(`操作失败：${err}`, "error");
      } finally {
        this.botActionLoading = "";
      }
      setTimeout(() => this.$parent && this.$parent.pollStatus && this.$parent.pollStatus(), 1500);
    },
    async toggleWatchdog() {
      const enabled = !(this.supervisor && this.supervisor.watchdog_enabled);
      await window.pywebview.api.set_watchdog(enabled);
      toast(enabled ? "看护已开启（10s 巡检 / 30s 退避）" : "看护已关闭");
    },
    removeTime(t) {
      if (this.schedulerCfg) this.schedulerCfg.times = this.schedulerCfg.times.filter((x) => x !== t);
    },
  },
  template: `
  <div>
    <page-header title="运维"
                 :subtitle="summary ? '版本 ' + summary.version + ' · 已运行 ' + Math.floor(summary.uptime_seconds/3600) + ' 小时' : '进程看护与平台管理'">
      <template #meta><span v-if="loading" class="muted">刷新中…</span></template>
      <button class="btn sm ghost" @click="loadStatus">刷新</button>
    </page-header>

    <!-- 端口占用警示 -->
    <div v-if="foreignPort" class="alert-strip mb-3">
      🚫 {{ supervisor.note }} —— 已禁止提供【启动】按钮
    </div>

    <!-- 进程看护卡 -->
    <div class="card mb-3 card-accent">
      <h3>进程看护</h3>
      <div class="row wrap">
        <status-pill :label="'塔菲：' + botStateLabel" :tone="botStateTone" />
        <status-pill v-if="supervisor"
                     :label="'NapCat：' + (supervisor.napcat_state === 'running' ? '运行中' : '未运行')"
                     :tone="supervisor.napcat_state === 'running' ? 'ok' : 'neutral'" />
        <span v-if="supervisor && supervisor.bot_pids.length" class="faint num">pid {{ supervisor.bot_pids.join(', ') }}</span>
        <span v-if="supervisor && !supervisor.port_open && supervisor.bot_pids.length" class="field-msg err">端口未就绪</span>
        <div class="spacer"></div>
        <button class="btn sm ghost" :disabled="botActionLoading !== ''" @click="botAction('start')">
          {{ botActionLoading === 'start' ? '启动中…' : '启动塔菲' }}
        </button>
        <button class="btn sm ghost" :disabled="botActionLoading !== ''" @click="botAction('restart')">
          {{ botActionLoading === 'restart' ? '重启中…' : '重启' }}
        </button>
        <button class="btn sm danger" :disabled="botActionLoading !== ''" @click="botAction('stop')">
          {{ botActionLoading === 'stop' ? '停止中…' : '停止' }}
        </button>
        <label class="checkbox-row">
          <input type="checkbox" :checked="supervisor && supervisor.watchdog_enabled" @change="toggleWatchdog" />
          看护（10s巡检/30s退避）
        </label>
      </div>
    </div>

    <app-tabs :tabs="[
      { key: 'status', label: '状态总览' },
      { key: 'logs', label: '日志' },
      { key: 'events', label: '事件流' },
      { key: 'scheduler', label: '广播与定时' },
      { key: 'permissions', label: '权限' },
      { key: 'audit', label: '使用审计' },
    ]" v-model:active="tab" @change="switchTab" />

    <!-- ══ 状态总览 ══ -->
    <template v-if="tab==='status'">
      <state-panel v-if="loading && !summary" state="loading" :rows="5" />
      <div v-else-if="summary" class="grid-2">
        <div class="card">
          <h3>健康检查
            <status-pill class="ml-auto" :label="summary.health.status"
                         :tone="summary.health.ready ? 'ok' : 'danger'" />
          </h3>
          <data-table :columns="healthColumns" :rows="healthPairs" row-key="id"
                      empty-title="暂无检查项" />
        </div>
        <div class="card">
          <h3>调度任务</h3>
          <data-table :columns="taskColumns" :rows="schedulerTasks" row-key="id" empty-title="暂无任务" />
          <p class="muted mt-2">{{
            Object.values(summary.health.checks.scheduler.tasks || {}).filter(v => v === 'running').length
          }}/{{ summary.health.checks.scheduler.required }} 在运行</p>
        </div>
      </div>
      <state-panel v-else state="error" message="无法获取系统摘要（离线或桥接异常）" @retry="loadStatus" />
    </template>

    <!-- ══ 日志 ══ -->
    <template v-else-if="tab==='logs'">
      <div class="card">
        <div class="row wrap mb-2">
          <select v-model.number="logLines" @change="refreshLogs" aria-label="日志行数">
            <option :value="50">50 行</option><option :value="120">120 行</option><option :value="300">300 行</option>
          </select>
          <select v-model="logLevelFilter" aria-label="日志级别过滤">
            <option value="">全部级别</option><option value="ERROR">ERROR</option>
            <option value="WARNING">WARNING</option><option value="INFO">INFO</option>
          </select>
          <button class="btn sm ghost" :disabled="logLoading" @click="refreshLogs">刷新尾部</button>
          <span class="faint">每 5 秒尾随刷新</span>
        </div>
        <pre class="code-block log-view">{{ filteredLogs || '暂无日志内容' }}</pre>
      </div>
    </template>

    <!-- ══ 事件流 ══ -->
    <template v-else-if="tab==='events'">
      <div class="card">
        <ul v-if="events.length" class="timeline">
          <li v-for="e in events" :key="e.key">
            <span class="badge" :class="e.kind==='alert_delivered' ? 'ok' : e.kind.includes('failed') ? 'danger' : ''">{{ e.kind }}</span>
            {{ e.summary }} <span class="faint num">{{ fmtTime(e.time) }}</span>
          </li>
        </ul>
        <state-panel v-else state="empty" icon="🗒️" title="暂无事件"
                     message="预警、定时播报等事件会在这里滚动展示。" tight />
      </div>
    </template>

    <!-- ══ 广播与定时 ══ -->
    <template v-else-if="tab==='scheduler'">
      <div class="grid-2">
        <div class="card" v-if="schedulerCfg">
          <h3>定时播报</h3>
          <div class="form-row"><label class="checkbox-row" for="sched-enable">
            <input id="sched-enable" type="checkbox" v-model="schedulerCfg.enabled" /> 启用定时播报</label></div>
          <div class="form-row"><label>时间点（点击 ✕ 移除）</label>
            <div class="row wrap gap-2">
              <span v-for="t in schedulerCfg.times || []" :key="t" class="pill info" role="button" tabindex="0"
                    title="点击移除该时间点" @click="removeTime(t)" @keydown.enter.prevent="removeTime(t)">
                🕐 {{ t }} ✕
              </span>
              <button class="btn sm ghost" @click="addTime">＋ 时间点</button>
            </div></div>
          <div class="form-row"><label for="sched-groups">目标群（逗号分隔）</label>
            <input id="sched-groups" type="text" spellcheck="false"
                   :value="(schedulerCfg.groups || []).join(',')"
                   @change="schedulerCfg.groups = $event.target.value.split(/[,，\\s]+/).map(Number).filter(n => n > 0)" /></div>
          <button class="btn" @click="saveScheduler">保存（版本 {{ schedulerCfg.version }}）</button>
        </div>
        <div class="card">
          <h3>手动广播</h3>
          <div class="form-row"><label for="bc-groups">目标群号（逗号分隔，≤50 个）</label>
            <input id="bc-groups" type="text" v-model="broadcastGroups" placeholder="123456, 654321" spellcheck="false" /></div>
          <div class="form-row"><label for="bc-text">内容（≤2000 字）</label>
            <textarea id="bc-text" v-model="broadcastText" rows="4"></textarea></div>
          <button class="btn danger" @click="sendBroadcast">发送广播</button>
          <p v-if="broadcastResult" class="muted mt-2">
            成功 {{ broadcastResult.succeeded }}/{{ broadcastResult.total }}；失败可在投递台账重试（admin 面板）
          </p>
        </div>
      </div>
    </template>

    <!-- ══ 权限 ══ -->
    <template v-else-if="tab==='permissions'">
      <div class="grid-2">
        <div class="card">
          <h3>功能清单</h3>
          <data-table :columns="featureColumns" :rows="(permissions && permissions.features) || []"
                      row-key="name" empty-title="加载中…" />
        </div>
        <div class="card">
          <h3>覆盖规则</h3>
          <data-table :columns="overrideColumns" :rows="(permissions && permissions.overrides) || []" row-key="user_id"
                      empty-title="暂无覆盖规则" empty-text="全员默认放行；在列表页可按人封禁。" />
        </div>
      </div>
    </template>

    <!-- ══ 使用审计 ══ -->
    <template v-else-if="tab==='audit'">
      <div class="card">
        <data-table :columns="auditColumns" :rows="auditUsage" row-key="id"
                    empty-title="暂无审计记录"
                    empty-text="用户每次使用功能都会在此留痕（输入摘要仅存脱敏文本）。" />
        <p class="faint mt-2">全量导出走 admin CSV（含公式注入防护）：/api/admin/usage/export</p>
      </div>
    </template>
  </div>
  `,
};
