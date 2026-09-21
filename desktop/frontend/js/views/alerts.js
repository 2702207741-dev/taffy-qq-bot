/* views/alerts.js — 预警（6.4）：列表 DataTable（方向/状态 StatusPill）/ 三步创建向导 /
   批量取消（危险操作）/ 触发历史时间线。 */

import { get, post, admin } from "../api.js";
import { toast, confirmBox, fmtPrice, fmtTime, pushEsc } from "../ui.js";
import { onRouteChange } from "../router.js";
import StatusPill from "../components/status-pill.js";

export default {
  data() {
    return {
      tab: "list",
      alerts: null,
      loading: false,
      history: [],
      historyLoading: false,
      wizard: { step: 0, query: "", candidates: [], name: "", direction: "below", target: null },
      notifyOn: {},
      creating: false,
      cancellingIds: {},
    };
  },
  computed: {
    activeCount() {
      return this.alerts ? this.alerts.count : 0;
    },
    alertRows() {
      return (this.alerts && this.alerts.alerts) || [];
    },
    selectedForBatch() {
      return this.alertRows.filter((a) => this.notifyOn[a.id] === "batch");
    },
    targetHintValue() {
      const w = this.wizard;
      const num = Number(w.target);
      if (!w.name) return "请先选择饰品";
      if (!Number.isFinite(num) || num <= 0) return "请输入有效的目标价";
      return "";
    },
    /* 第二步校验：目标价与方向合理性（服务端为准，这里做实时提示） */
    targetInvalid() {
      return Boolean(this.wizard.step === 2 && this.wizard.target != null && this.targetHintValue);
    },
    listColumns() {
      const { h } = Vue;
      const self = this;
      return [
        {
          key: "_pick",
          label: "批量",
          width: "52px",
          render: (row) =>
            h("input", {
              type: "checkbox",
              "aria-label": `加入批量取消 ${row.market_name}`,
              checked: self.notifyOn[row.id] === "batch",
              onChange: (e) => self.setNotify(row.id, e.target.checked ? "batch" : null),
            }),
        },
        {
          key: "market_name",
          label: "饰品",
          ellipsis: true,
          minWidth: "160px",
          render: (row) => String(row.market_name),
        },
        {
          key: "direction",
          label: "方向",
          width: "110px",
          render: (row) =>
            h(StatusPill, {
              label: self.directionLabel(row.direction),
              tone: self.directionTone(row.direction),
              dot: false,
            }),
        },
        {
          key: "target_price_cents",
          label: "目标价",
          align: "right",
          sortable: true,
          sortValue: (row) => row.target_price_cents,
          render: (row) => fmtPrice(row.target_price_cents / 100),
        },
        {
          key: "status",
          label: "状态",
          width: "96px",
          sortable: true,
          render: (row) =>
            h(StatusPill, {
              label: self.statusLabel(row.status),
              tone: self.statusTone(row.status),
            }),
        },
        {
          key: "created_at",
          label: "创建时间",
          render: (row) => fmtTime(row.created_at),
        },
        {
          key: "_notify",
          label: "桌面通知",
          width: "84px",
          render: (row) =>
            h("input", {
              type: "checkbox",
              title: "触发时弹桌面通知",
              "aria-label": `桌面通知 ${row.market_name}`,
              checked: self.notifyOn[row.id] === "on",
              onChange: (e) => self.setNotify(row.id, e.target.checked ? "on" : null),
            }),
        },
        {
          key: "_cancel",
          label: "",
          width: "64px",
          align: "right",
          render: (row) =>
            h(
              "button",
              {
                class: "btn sm ghost",
                disabled: Boolean(self.cancellingIds[row.id]),
                onClick: () => self.cancelOne(row),
              },
              self.cancellingIds[row.id] ? "…" : "取消"
            ),
        },
      ];
    },
  },
  mounted() {
    this.load();
    this.offRoute = onRouteChange((path) => {
      if (path === "alerts") {
        const pending = sessionStorage.getItem("alert-wizard-name");
        if (pending) {
          sessionStorage.removeItem("alert-wizard-name");
          this.wizard.query = pending;
          this.wizard.step = 1;
          this.wizardSearch(pending);
        }
        this.load();
      }
    });
    this.onRefresh = () => this.load();
    window.addEventListener("view-refresh", this.onRefresh);
  },
  unmounted() {
    this.offRoute && this.offRoute();
    window.removeEventListener("view-refresh", this.onRefresh);
    if (this._offWizardEsc) this._offWizardEsc();
  },
  watch: {
    "wizard.step"(step) {
      if (this._offWizardEsc) {
        this._offWizardEsc();
        this._offWizardEsc = null;
      }
      if (step > 0) this._offWizardEsc = pushEsc(() => (this.wizard.step = 0));
    },
  },
  methods: {
    fmtPrice,
    fmtTime,
    setNotify(id, value) {
      if (value == null) delete this.notifyOn[id];
      else this.notifyOn[id] = value;
      this.notifyOn = { ...this.notifyOn };
    },
    async load() {
      this.loading = !this.alerts;
      const [alertsRes, eventsRes] = await Promise.all([
        admin("GET", "/api/admin/alerts?limit=200"),
        admin("GET", "/api/admin/events?limit=100"),
      ]);
      this.alerts = alertsRes || null;
      this.history = eventsRes
        ? (eventsRes.events || []).filter((e) => e.kind.startsWith("alert_")).slice(0, 20)
        : [];
      this.historyLoading = false;
      this.loading = false;
    },
    directionTone(direction) {
      return direction === "above" ? "warn" : "info";
    },
    directionLabel(direction) {
      return direction === "above" ? "≥ 高于出货" : "≤ 低于抄底";
    },
    statusTone(status) {
      if (status === "pending") return "warn";
      if (status === "delivered") return "ok";
      if (status === "failed") return "danger";
      return "neutral";
    },
    /* 向导第一步：搜索选品 */
    async wizardSearch(q) {
      const res = await get(`/api/desktop/market/search?q=${encodeURIComponent(q)}&limit=8`);
      this.wizard.candidates = res.status === 200 && res.body.ok ? res.body.data.results : [];
    },
    pickCandidate(item) {
      this.wizard.name = item.market_name;
      this.wizard.step = 2;
    },
    toConfirm() {
      if (this.targetHintValue) {
        toast(this.targetHintValue, "error");
        return;
      }
      this.wizard.step = 3;
    },
    async createAlert() {
      const w = this.wizard;
      this.creating = true;
      const res = await post("/api/desktop/alerts", {
        name: w.name,
        direction: w.direction,
        target: Number(w.target),
      });
      this.creating = false;
      if (res.status === 200 && res.body.ok) {
        toast("预警已创建（QQ「我的预警」同步可见）", "success");
        this.wizard = { step: 0, query: "", candidates: [], name: "", direction: "below", target: null };
        this.tab = "list";
        this.load();
      } else {
        // 服务端报错文案直接展示（如已达标拒绝逻辑）
        toast((res.body && (res.body.message || res.body.error)) || `创建失败（${res.status}）`, "error");
      }
    },
    async batchCancel() {
      const targets = this.selectedForBatch;
      if (!targets.length) return;
      const okBatch = await confirmBox(
        "批量取消预警",
        `将取消 ${targets.length} 条预警（含目标价与方向设置），操作不可恢复。`,
        { danger: true }
      );
      if (!okBatch) return;
      let done = 0;
      for (const a of targets) {
        this.cancellingIds[a.id] = true;
        const res = await admin("DELETE", `/api/admin/alerts/${a.id}`);
        if (res && res.deleted) done += 1;
        delete this.cancellingIds[a.id];
      }
      this.notifyOn = {};
      toast(`批量取消完成：${done}/${targets.length}`, done === targets.length ? "success" : "warning");
      this.load();
    },
    async cancelOne(alert) {
      const okCancel = await confirmBox("取消预警", `确定取消「${alert.market_name}」的预警？`, { danger: true });
      if (!okCancel) return;
      this.cancellingIds[alert.id] = true;
      const res = await admin("DELETE", `/api/admin/alerts/${alert.id}`);
      delete this.cancellingIds[alert.id];
      if (res && res.deleted) {
        toast("预警已取消", "success");
        this.load();
      } else toast("取消失败", "error");
    },
    statusLabel(status) {
      return { pending: "待触发", delivered: "已触发", failed: "失败" }[status] || status;
    },
  },
  template: `
  <div>
    <page-header title="预警"
                 subtitle="价格越过目标即提醒 · 与 QQ 端「我的预警」同源">
      <template #meta>
        <status-pill :label="activeCount + ' 条活跃'" tone="accent" title="当前进行中的预警数量" />
      </template>
      <button v-if="selectedForBatch.length" class="btn danger" @click="batchCancel">
        取消勾选的 {{ selectedForBatch.length }} 条
      </button>
      <button class="btn" @click="wizard.step = 1">＋ 创建预警</button>
    </page-header>

    <app-tabs :tabs="[
      { key: 'list', label: '列表' },
      { key: 'history', label: '触发历史' },
    ]" v-model:active="tab" />

    <!-- ══ 列表 ══ -->
    <template v-if="tab==='list'">
      <div class="card">
        <data-table :columns="listColumns" :rows="alertRows" row-key="id" :loading="loading && !alerts"
                    empty-title="暂无预警" empty-icon="🔔"
                    empty-text="点右上角「＋ 创建预警」，三步完成设置。" />
      </div>
      <p class="faint mt-2">勾选「批量」列后可一键取消；勾选「桌面通知」后，触发事件将进入通知中心规则（可设免打扰/聚合）。</p>
    </template>

    <!-- ══ 触发历史 ══ -->
    <template v-else-if="tab==='history'">
      <div class="card">
        <ul v-if="history.length" class="timeline">
          <li v-for="e in history" :key="e.key">
            <span class="muted num">{{ fmtTime(e.time) }}</span> · {{ e.summary }}
          </li>
        </ul>
        <state-panel v-else-if="!historyLoading" state="empty" icon="🕘" title="暂无触发记录"
                     message="还没有预警响过，塔菲耳朵竖着呢喵。预警触达/失败的事件会沉淀在这里，便于回溯盯盘动作。" tight />
      </div>
    </template>

    <!-- ══ 三步向导弹层 ══ -->
    <div class="modal" v-if="wizard.step > 0" @click.self="wizard.step = 0">
      <div class="modal-box" style="width:520px" role="dialog" aria-modal="true" aria-label="创建预警向导">
        <h4>创建预警 · 第 {{ wizard.step }}/3 步</h4>

        <!-- 步骤1 搜索 -->
        <template v-if="wizard.step === 1">
          <div class="form-row"><label for="wiz-q">搜索饰品</label>
            <input id="wiz-q" type="text" ref="wizQuery" v-model="wizard.query" @input="wizardSearch(wizard.query)"
                   placeholder="如：红线" spellcheck="false" autofocus /></div>
          <div class="palette-list wiz-candidates">
            <div v-for="c in wizard.candidates" :key="c.market_name" class="palette-item"
                 role="button" tabindex="0" @click="pickCandidate(c)"
                 @keydown.enter.prevent="pickCandidate(c)">
              {{ c.display_name }}
            </div>
            <state-panel v-if="!wizard.candidates.length" state="empty" icon="🔎"
                         title="输入关键词搜索" tight />
          </div>
        </template>

        <!-- 步骤2 方向+目标价 -->
        <template v-else-if="wizard.step === 2">
          <div class="form-row"><label>饰品</label><input type="text" :value="wizard.name" disabled /></div>
          <div class="form-row"><label for="wiz-dir">方向</label>
            <select id="wiz-dir" v-model="wizard.direction">
              <option value="below">低于目标价时提醒（抄底）</option>
              <option value="above">高于目标价时提醒（出货）</option>
            </select></div>
          <div class="form-row"><label for="wiz-target">目标价（元）</label>
            <input id="wiz-target" type="number" v-model.number="wizard.target" min="0.01" step="0.01"
                   :class="{ 'is-invalid': targetInvalid }" />
            <span class="field-msg" :class="targetInvalid ? 'err' : 'hint'" role="status">
              {{ targetInvalid ? '⚠️ ' + targetHintValue : '当前价可在市场详情核对；已达标的目标将被服务端拒绝' }}
            </span></div>
          <div class="row" style="justify-content:flex-end; gap:8px">
            <button class="btn ghost" @click="wizard.step = 1">上一步</button>
            <button class="btn" @click="toConfirm">下一步</button>
          </div>
        </template>

        <!-- 步骤3 确认 -->
        <template v-else>
          <p>为「<b>{{ wizard.name }}</b>」创建预警：</p>
          <p class="mt-2 mb-2">当价格 <b>{{ wizard.direction === 'above' ? '高于' : '低于' }}</b>
             <b>{{ fmtPrice(Number(wizard.target)) }}</b> 时提醒你。</p>
          <p class="muted">预警同时归属 QQ 账号，QQ 端「我的预警」可见同一条。</p>
          <div class="row mt-3" style="justify-content:flex-end; gap:8px">
            <button class="btn ghost" @click="wizard.step = 2">上一步</button>
            <button class="btn" :disabled="creating" @click="createAlert">
              <span v-if="creating" class="spin" aria-hidden="true"></span>确认创建
            </button>
          </div>
        </template>
      </div>
    </div>
  </div>
  `,
};
