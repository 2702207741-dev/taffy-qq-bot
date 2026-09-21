/* views/persona.js — 人格印象（6.7）：学习印象审核 DataTable（状态 StatusPill）/
   长期记忆 / 风格档案六维雷达（chart-theme）；全部审核操作带 toast。 */

import { get, post, del, admin } from "../api.js";
import { toast, confirmBox, fmtTime } from "../ui.js";
import { buildChartTheme } from "../components/chart-theme.js";

export default {
  data() {
    return {
      tab: "learning",
      learning: null,
      learningStats: null,
      profile: null,
      profileLoading: false,
      newMemory: "",
      radarChart: null,
      scopeNote: "桌面端展示主人私聊作用域（与 QQ 私聊同一份记忆/风格，天然互通）",
      slang: null,
      slangGroupId: 0,
      slangTerm: "",
      slangMeaning: "",
      relUserId: "",
      rel: null,
      relLoading: false,
    };
  },
  computed: {
    pendingCount() {
      return this.learningStats ? this.learningStats.pending || 0 : 0;
    },
    slangPending() {
      return (this.slang && this.slang.pending) || [];
    },
    slangAccepted() {
      return (this.slang && this.slang.accepted) || [];
    },
    slangColumns() {
      const { h } = Vue;
      const self = this;
      return [
        { key: "term", label: "词条" },
        { key: "meaning", label: "含义", ellipsis: true },
        {
          key: "actions", label: "操作", width: "170px",
          render: (row) => {
            const btn = (label, cls, fn) =>
              h("button", { class: `btn sm ${cls}`, onClick: fn }, label);
            return [
              btn("确认", "", () => self.decideSlang(row.id, "accepted")),
              btn("拒绝", "ghost", () => self.decideSlang(row.id, "rejected")),
              btn("删", "danger", () => self.deleteSlang(row.id)),
            ];
          },
        },
      ];
    },
    slangAcceptedColumns() {
      const { h } = Vue;
      const self = this;
      return [
        { key: "term", label: "词条" },
        { key: "meaning", label: "含义", ellipsis: true },
        {
          key: "actions", label: "操作", width: "90px",
          render: (row) =>
            h("button", { class: "btn sm danger", onClick: () => self.deleteSlang(row.id) }, "删"),
        },
      ];
    },
    insights() {
      return (this.learning && this.learning.insights) || [];
    },
    memories() {
      return (this.profile && this.profile.memories) || [];
    },
    learningColumns() {
      const { h } = Vue;
      const self = this;
      return [
        {
          key: "status",
          label: "状态",
          width: "96px",
          sortable: true,
          render: (row) =>
            h("span", { class: `badge ${self.statusTone(row.status)}` }, self.statusLabel(row.status)),
        },
        { key: "kind", label: "类型", width: "110px" },
        {
          key: "content",
          label: "内容",
          ellipsis: true,
          minWidth: "200px",
          render: (row) => String(row.content),
        },
        {
          key: "confidence",
          label: "置信度",
          align: "right",
          sortable: true,
          sortValue: (row) => row.confidence || 0,
          render: (row) => `${Math.round((row.confidence || 0) * 100)}%`,
        },
        { key: "created_at", label: "时间", render: (row) => fmtTime(row.created_at) },
        {
          key: "_op",
          label: "操作",
          width: "168px",
          render: (row) => {
            const buttons = [];
            if (row.status === "pending") {
              buttons.push(
                h("button", { class: "btn sm", onClick: () => self.review(row, "accept") }, "通过"),
                h("button", { class: "btn sm ghost", onClick: () => self.review(row, "reject") }, "拒绝")
              );
            } else {
              buttons.push(h("button", { class: "btn sm ghost", onClick: () => self.archive(row) }, "归档"));
            }
            return buttons;
          },
        },
      ];
    },
    memoryColumns() {
      const { h } = Vue;
      const self = this;
      return [
        { key: "_idx", label: "#", width: "52px", render: (row) => String(self.memories.indexOf(row) + 1) },
        { key: "content", label: "内容", ellipsis: true, render: (row) => String(row.content) },
        { key: "updated_at", label: "更新时间", render: (row) => fmtTime(row.updated_at) },
        {
          key: "_del",
          label: "",
          width: "72px",
          align: "right",
          render: (row) =>
            h(
              "button",
              { class: "btn sm danger", onClick: () => self.deleteMemory(row, self.memories.indexOf(row)) },
              "忘掉"
            ),
        },
      ];
    },
    stylePairs() {
      const s = this.profile && this.profile.style;
      if (!s) return [];
      return [
        { k: "简洁 concise", v: s.concise },
        { k: "温柔 warm", v: s.warm },
        { k: "毒舌 teasing", v: s.teasing },
        { k: "主动 proactive", v: s.proactive },
        { k: "口癖 catchphrase", v: s.catchphrase },
        { k: "直接 direct", v: s.direct },
      ];
    },
    styleRadarPoints() {
      const s = this.profile ? this.profile.style : null;
      if (!s) return "";
      const dims = ["concise", "warm", "teasing", "proactive", "catchphrase", "direct"];
      return dims.map((d) => `${d}:${s[d] > 0 ? "+" : ""}${s[d]}`).join(" · ");
    },
  },
  mounted() {
    this.loadAll();
    this.onRefresh = () => this.loadAll();
    window.addEventListener("view-refresh", this.onRefresh);
  },
  unmounted() {
    window.removeEventListener("view-refresh", this.onRefresh);
    if (this.radarChartInstance) this.radarChartInstance.dispose();
  },
  methods: {
    fmtTime,
    async loadAll() {
      const [learningRes, statsRes, profileRes] = await Promise.all([
        admin("GET", "/api/admin/learning?limit=100"),
        admin("GET", "/api/admin/learning/stats"),
        get("/api/desktop/persona/profile"),
      ]);
      this.learning = learningRes || null;
      this.learningStats = statsRes || null;
      this.profile = profileRes.status === 200 && profileRes.body.ok ? profileRes.body.data : null;
      this.profileLoading = false;
      this.$nextTick(() => this.renderRadar());
    },
    statusTone(status) {
      return { pending: "warn", accepted: "ok" }[status] || "";
    },
    statusLabel(status) {
      return { pending: "待审", accepted: "已通过", rejected: "已拒绝", archived: "已归档" }[status] || status;
    },
    async review(insight, action) {
      const verb = action === "accept" ? "通过" : "拒绝";
      const okReview = await confirmBox(`${verb}印象`, `内容：${insight.content}\n确认${verb}？`);
      if (!okReview) return;
      const res = await admin("POST", `/api/admin/learning/${insight.id}/${action}`);
      if (res) {
        toast(`已${verb}`, "success");
        /* 通过后生效路径：印象进入 persona 提示词上下文（QQ 与桌面同源），下次对话即体现 */
        this.loadAll();
      } else toast(`${verb}失败`, "error");
    },
    async archive(insight) {
      void insight; // 归档能力由 admin 面板承载；此处保留入口一致性
      toast("归档请在 QQ 端或 admin 面板操作");
    },
    async addMemory() {
      const content = this.newMemory.trim();
      if (!content) return;
      const res = await post("/api/desktop/persona/memory", { content });
      if (res.status === 200 && res.body.ok) {
        this.newMemory = "";
        toast("已记住（QQ 私聊同样可见）", "success");
        this.loadAll();
        return;
      }
      toast((res.body && res.body.message) || "保存失败", "error"); // 容量满 → 429 语义
    },
    async deleteMemory(memory, index) {
      const okDelete = await confirmBox("忘掉这条记忆", memory.content, { danger: true });
      if (!okDelete) return;
      const res = await del(`/api/desktop/persona/memory?index=${index + 1}`);
      if (res.status === 200 && res.body.ok) {
        toast("已删除", "success");
        this.loadAll();
      } else toast((res.body && res.body.message) || "删除失败", "error");
    },
    async setNickname() {
      // eslint-disable-next-line no-alert
      const nickname = window.prompt("设置塔菲对你的称呼（20 字内）", this.profile?.nickname || "");
      if (!nickname) return;
      const res = await post("/api/desktop/persona/memory", { content: nickname, kind: "nickname" });
      toast(
        res.status === 200 ? "称呼已更新" : (res.body?.message || "设置失败"),
        res.status === 200 ? "info" : "error"
      );
      if (res.status === 200) this.loadAll();
    },
    async styleAction(action, title, body, requireType) {
      const opts = { danger: action === "reset" };
      if (requireType) opts.requireType = requireType;
      const ok = await confirmBox(title, body, opts);
      if (!ok) return;
      const res = await post("/api/desktop/persona/style", { action });
      if (res.status === 200 && res.body.ok) {
        toast(`已执行：${title}`, "success");
        this.loadAll();
      } else toast((res.body && res.body.message) || "执行失败", "error");
    },
    onRadarReady(chart) {
      this.radarChartInstance = chart;
      this.renderRadar();
    },
    renderRadar() {
      const chart = this.radarChartInstance;
      if (!chart || chart.isDisposed()) return;
      if (!this.profile || !window.echarts) return;
      if (this.tab !== "style") return; // 隐藏页签容器零宽，等切换后再画
      const theme = buildChartTheme();
      const s = this.profile.style;
      const dims = ["简洁 concise", "温柔 warm", "毒舌 teasing", "主动 proactive", "口癖 catchphrase", "直接 direct"];
      chart.setOption({
        animation: false,
        radar: {
          indicator: dims.map((name) => ({ name, max: 2 })),
          radius: "68%",
          axisName: { fontSize: 11, color: theme.textDim },
          splitLine: { lineStyle: { color: theme.grid } },
          splitArea: { show: false },
          axisLine: { lineStyle: { color: theme.grid } },
        },
        series: [
          {
            type: "radar",
            data: [
              {
                value: [s.concise, s.warm, s.teasing, s.proactive, s.catchphrase, s.direct],
                areaStyle: { opacity: 0.3, color: theme.accent },
                lineStyle: { color: theme.accentStrong },
              },
            ],
          },
        ],
      });
    },
    switchTab(tab) {
      this.tab = tab;
      if (tab === "style") this.$nextTick(() => this.renderRadar());
      if (tab === "slang") this.loadSlang();
    },
    async loadSlang() {
      const res = await get(`/api/desktop/persona/slang?group_id=${this.slangGroupId || 0}`);
      if (res.status === 200 && res.body.ok) this.slang = res.body.data;
      else toast((res.body && res.body.message) || "话术词典加载失败", "error");
    },
    async teachSlang() {
      const term = this.slangTerm.trim();
      const meaning = this.slangMeaning.trim();
      if (!term || !meaning) return toast("请填写词条与含义", "warn");
      const res = await post("/api/desktop/persona/slang", {
        action: "teach", group_id: this.slangGroupId || 0, term, meaning,
      });
      if (res.status === 200 && res.body.ok) {
        toast("已加入待确认列表", "ok");
        this.slangTerm = ""; this.slangMeaning = "";
        await this.loadSlang();
      } else toast((res.body && res.body.message) || "保存失败", "error");
    },
    async decideSlang(entryId, status) {
      const res = await post("/api/desktop/persona/slang", { action: "decide", entry_id: entryId, status });
      if (res.status === 200 && res.body.ok) { toast(status === "accepted" ? "已生效" : "已拒绝", "ok"); await this.loadSlang(); }
      else toast((res.body && res.body.message) || "操作失败", "error");
    },
    async deleteSlang(entryId) {
      if (!(await confirmBox("删除话术", "删除后不可恢复，确认？", "删除"))) return;
      const res = await del(`/api/desktop/persona/slang?id=${entryId}`);
      if (res.status === 200 && res.body.ok) { toast("已删除", "ok"); await this.loadSlang(); }
      else toast((res.body && res.body.message) || "删除失败", "error");
    },
    async loadRelationship() {
      const uid = String(this.relUserId || "").trim();
      if (!uid || !uid.match(/^\d+$/)) return toast("请输入纯数字 QQ 号", "warn");
      this.relLoading = true;
      const res = await get(`/api/desktop/persona/relationship?user_id=${uid}&group_id=${this.slangGroupId || 0}`);
      this.relLoading = false;
      if (res.status === 200 && res.body.ok) this.rel = res.body.data;
      else toast((res.body && res.body.message) || "查询失败", "error");
    },
  },
  template: `
  <div>
    <page-header title="人格与印象" :subtitle="scopeNote">
      <template #meta>
        <status-pill :label="pendingCount + ' 条待审'"
                     :tone="pendingCount > 0 ? 'warn' : 'neutral'" title="等待审核的学习印象" />
      </template>
    </page-header>

    <app-tabs :tabs="[
      { key: 'learning', label: '学习印象', badge: pendingCount || '' },
      { key: 'memory', label: '长期记忆', badge: memories.length || '' },
      { key: 'style', label: '风格档案' },
      { key: 'slang', label: '话术词典', badge: this.slangPending.length || '' },
    ]" v-model:active="tab" @change="switchTab" />

    <!-- ══ 学习印象 ══ -->
    <template v-if="tab==='learning'">
      <div class="card">
        <data-table :columns="learningColumns" :rows="insights" row-key="id"
                    empty-title="暂无印象记录" empty-icon="🐱"
                    empty-text="开启学习后，塔菲会从对话中提炼可复核的印象。" />
        <p class="faint mt-2">通过后：印象写入 persona 提示词上下文 —— QQ 私聊与桌面聊天共用同一作用域，立即对两端生效。</p>
      </div>
    </template>

    <!-- ══ 长期记忆 ══ -->
    <template v-else-if="tab==='memory'">
      <div class="card">
        <div class="row wrap mb-3">
          <input type="text" v-model="newMemory" class="grow" spellcheck="false"
                 placeholder="让塔菲记住一件事…（如：主人偏好简短回复）"
                 @keydown.enter="addMemory" />
          <button class="btn" @click="addMemory">记住</button>
          <button class="btn ghost" @click="setNickname">设置称呼</button>
        </div>
        <data-table :columns="memoryColumns" :rows="memories" row-key="id"
                    empty-title="还没有长期记忆（容量 20 条）" empty-icon="🧠"
                    empty-text="对话里说「记住…」或在上方输入，都会写入这份两端口径的记忆。" />
      </div>
    </template>

    <!-- ══ 风格档案 ══ -->
    <template v-else-if="tab==='style'">
      <state-panel v-if="!profile" state="loading" :rows="4" />
      <div v-else class="grid-2">
        <chart-card title="六维风格雷达" subtitle="范围 -2 ~ +2" :height="300"
                    @ready="onRadarReady" />
        <div class="card">
          <h3>当前六维取值</h3>
          <kv-list :pairs="stylePairs" />
          <div class="row wrap mt-3 gap-2">
            <button class="btn ghost" @click="profile.learning_enabled
              ? styleAction('learning_off','关闭学习','塔菲不再自动调整风格')
              : styleAction('learning_on','开启学习','根据你的反馈持续调整说话风格')">
              {{ profile.learning_enabled ? '关闭学习' : '开启学习' }}
            </button>
            <button class="btn ghost" @click="styleAction('undo','撤销调整','回退上一条风格反馈')">撤销上次</button>
            <button class="btn danger" @click="styleAction('reset','重置风格','六维全部归零，不可撤销。输入「重置」确认。','重置')">重置</button>
          </div>
          <p class="faint mt-2">{{ styleRadarPoints }}</p>
        </div>
      </div>
    </template>
    <!-- ══ 话术词典 ══ -->
    <template v-else-if="tab==='slang'">
      <div class="card mb-3">
        <div class="row wrap mb-3">
          <input type="number" v-model.number="slangGroupId" min="0" placeholder="群号（0=私聊/全局）" style="width:180px" />
          <button class="btn sm ghost" @click="loadSlang">刷新</button>
          <div class="spacer"></div>
          <input type="number" v-model="relUserId" placeholder="QQ 号查熟悉度" style="width:160px" />
          <button class="btn sm ghost" :disabled="relLoading" @click="loadRelationship">{{ relLoading ? '…' : '查熟悉度' }}</button>
        </div>
        <div v-if="rel" class="notice-strip mb-3">
          熟悉度：{{ rel.band }}（{{ rel.intimacy }} / 100）· 互动 {{ rel.interaction_count }} 次 · 已确认印象 {{ rel.accepted_insight_count }} 条 · 风格反馈 {{ rel.style_feedback_count }} 次
        </div>
        <div class="row wrap mb-3">
          <input type="text" v-model="slangTerm" placeholder="词条（如：6）" style="width:140px" />
          <input type="text" v-model="slangMeaning" class="grow" placeholder="含义或用法（如：形容厉害）" />
          <button class="btn" @click="teachSlang">教塔菲</button>
        </div>
        <data-table :columns="slangColumns" :rows="slangPending" row-key="id"
                    empty-title="暂无待确认话术" empty-icon="📖"
                    empty-text="教塔菲说群里的方言：确认后才会进入人格表达素材。" />
        <h3 class="mt-3">已生效</h3>
        <data-table :columns="slangAcceptedColumns" :rows="slangAccepted" row-key="id"
                    empty-title="暂无已生效话术" empty-icon="✅"
                    empty-text="确认后的话术会出现在这里。" />
      </div>
    </template>
  </div>
  `,
};
