/* views/users.js — 用户（6.10）：用户列表 DataTable；单人详情改为右侧 Drawer 抽屉
   （概览 KVList / 聊天流水 / 功能封禁 / 群务管控 / 数据管理权 / 使用时间线）；
   高风险操作全部 requireType 危险确认，操作前后有 toast 反馈。 */

import { get, post, admin } from "../api.js";
import { toast, confirmBox, fmtTime } from "../ui.js";
import StatusPill from "../components/status-pill.js";

const HIGH_RISK_ACTIONS = {
  clear_session: { label: "清空该用户全部会话", require: "清空" },
  clear_memory: { label: "清空该用户长期记忆", require: "清空" },
  cancel_alerts: { label: "取消其全部进行中预警", require: "取消" },
  clear_watchlist: { label: "删除其全部自选", require: "删除" },
};

export default {
  data() {
    return {
      searchQ: "",
      users: [],
      loading: false,
      /* 抽屉状态 */
      drawerOpen: false,
      drawerTab: "overview",
      selected: null,
      overview: null,
      overviewLoading: false,
      events: [],
      nextCursor: 0,
      daysFilter: 7,
      timelineLoading: false,
      chatScope: "group",
      chatDays: 3,
      chatMessages: [],
      chatPrivacyNotice: null,
      chatLoaded: false,
      chatLoading: false,
      features: [],
      overrides: {},
      groups: [],
      members: [],
      selectedGroup: null,
      memberFilter: "",
      muteMinutes: 10,
      kickConfirmInput: "",
      recallMsgId: null,
    };
  },
  computed: {
    moderationTrail() {
      return (this.overview && this.overview.moderation_trail) || [];
    },
    filteredMembers() {
      const q = this.memberFilter.trim().toLowerCase();
      if (!q) return this.members.slice(0, 50);
      return this.members
        .filter((m) => String(m.user_id).includes(q) || (m.nickname || "").toLowerCase().includes(q))
        .slice(0, 50);
    },
    userColumns() {
      const { h } = Vue;
      const self = this;
      return [
        { key: "user_id", label: "QQ", width: "150px" },
        {
          key: "last_active_at",
          label: "最近活跃",
          sortable: true,
          sortValue: (row) => row.last_active_at,
          render: (row) => fmtTime(row.last_active_at),
        },
        { key: "total_calls_30d", label: "30天调用", align: "right", sortable: true },
        {
          key: "_features",
          label: "功能分布",
          render: (row) =>
            (row.features || []).slice(0, 5).map((f, i) =>
              h(StatusPill, { key: f.feature + i, label: `${f.feature}×${f.calls}`, tone: "neutral", dot: false })
            ),
        },
        {
          key: "_op",
          label: "",
          width: "76px",
          align: "right",
          render: (row) =>
            h("button", { class: "btn sm ghost", onClick: () => self.openUser(row.user_id) }, "详情"),
        },
      ];
    },
  },
  mounted() {
    this.loadUsers();
    this.onRefresh = () => this.loadUsers();
    window.addEventListener("view-refresh", this.onRefresh);
  },
  unmounted() {
    window.removeEventListener("view-refresh", this.onRefresh);
  },
  methods: {
    fmtTime,
    /* ── 列表 ── */
    async loadUsers() {
      this.loading = true;
      const suffix = this.searchQ.trim() ? `?q=${encodeURIComponent(this.searchQ.trim())}` : "";
      const res = await get(`/api/desktop/users${suffix}`);
      this.users = res.status === 200 && res.body.ok ? res.body.data.users : [];
      this.loading = false;
    },
    async openUser(qq) {
      this.selected = qq;
      this.drawerOpen = true;
      this.events = [];
      this.nextCursor = 0;
      this.chatMessages = [];
      this.chatLoaded = false;
      this.chatPrivacyNotice = null;
      await Promise.all([this.loadOverview(), this.loadTimeline(false), this.loadPermissions(), this.loadGroups()]);
    },
    closeDrawer() {
      this.drawerOpen = false;
      this.selectedGroup = null;
      this.kickConfirmInput = "";
    },
    /* ── 概览 / 时间线 ── */
    async loadOverview() {
      this.overviewLoading = !this.overview || this.overview.user_id !== this.selected;
      const res = await get(`/api/desktop/users/${this.selected}/overview`);
      this.overview = res.status === 200 && res.body.ok ? res.body.data : null;
      this.overviewLoading = false;
    },
    async loadTimeline(append) {
      if (!this.selected) return;
      this.timelineLoading = true;
      const cursor = append ? this.nextCursor : 0;
      const params = new URLSearchParams({ days: String(this.daysFilter), limit: "50" });
      if (cursor) params.set("cursor", String(cursor));
      const res = await get(`/api/desktop/users/${this.selected}/timeline?${params}`);
      if (res.status === 200 && res.body.ok) {
        this.events = append ? this.events.concat(res.body.data.events) : res.body.data.events;
        this.nextCursor = res.body.data.next_cursor;
      } else if (!append) {
        this.events = [];
      }
      this.timelineLoading = false;
    },
    /* ── 聊天流水 ── */
    async loadChat(forceConfirm) {
      if (!this.selected) return;
      this.chatLoading = true;
      const suffix = this.chatScope === "private" && forceConfirm ? "&confirm_view_private=1" : "";
      const params = new URLSearchParams({ scope: this.chatScope, days: String(this.chatDays) });
      const res = await get(`/api/desktop/users/${this.selected}/chat-log?${params}${suffix}`);
      this.chatLoading = false;
      if (res.status === 403) {
        // 隐私红线：他人私聊需要二段确认。
        const ok = await confirmBox(
          "查看他人私聊",
          `即将查看用户 ${this.selected} 的私聊内容。此操作会记录审计日志，仅自己可见。`,
          { danger: true }
        );
        if (ok) return this.loadChat(true);
        return;
      }
      if (res.status === 200 && res.body.ok) {
        this.chatMessages = res.body.data.messages;
        this.chatPrivacyNotice = res.body.data.privacy_notice;
        this.chatLoaded = true;
      } else {
        this.chatMessages = [];
        this.chatLoaded = false;
        toast((res.body && res.body.message) || "聊天记录暂不可用", "error");
      }
    },
    /* ── 权限封禁 ── */
    async loadPermissions() {
      if (!this.selected) return;
      const [featuresRes, overridesRes] = await Promise.all([
        admin("GET", "/api/admin/permissions/features"),
        admin("GET", `/api/admin/permissions?user=${this.selected}`),
      ]);
      this.features = (featuresRes && featuresRes.features) || [];
      const map = {};
      for (const item of (overridesRes && overridesRes.permissions) || []) {
        map[item.feature] = item.allowed;
      }
      this.overrides = map;
    },
    async setFeature(feature, allowed) {
      const res = await admin(
        allowed === null ? "DELETE" : "PUT",
        `/api/admin/permissions/${this.selected}/${feature}`,
        allowed === null ? null : { allowed }
      );
      if (res) {
        if (allowed === null) delete this.overrides[feature];
        else this.overrides[feature] = allowed;
        this.overrides = { ...this.overrides };
        toast(allowed === null ? "已恢复默认（允许）" : allowed ? "已允许" : "已封禁");
      } else {
        toast("权限设置失败", "error");
      }
    },
    async banAll() {
      const ok = await confirmBox(
        "全禁该用户",
        `将封禁 ${this.selected} 的聊天/行情/娱乐等全部功能（预警、自选等数据保留）。`,
        { danger: true }
      );
      if (!ok) return;
      for (const f of this.features) {
        if (["chat", "market", "entertainment"].includes(f.name)) {
          await this.setFeature(f.name, false);
        }
      }
      toast("全禁执行完毕", "warning");
    },
    /* ── 数据管理权（高风险 requireType） ── */
    async dataAction(kind) {
      const meta = HIGH_RISK_ACTIONS[kind];
      const detailText =
        kind === "cancel_alerts"
          ? `${meta.label}（当前 ${this.overview.alerts.active} 条）`
          : kind === "clear_watchlist"
            ? `${meta.label}（当前 ${this.overview.watch_count} 条）`
            : meta.label;
      const ok = await confirmBox("数据管理 · 高风险操作", `确定「${detailText}」？此操作不可恢复。`, {
        danger: true,
        requireType: meta.require,
        okText: `确认${meta.require}`,
      });
      if (!ok) return;
      const res = await post(`/api/desktop/users/${this.selected}/${kind.replace(/_/g, "-")}`);
      if (res.status === 200 && res.body.ok) {
        toast(`已完成（影响 ${JSON.stringify(res.body.data)}）`, "success");
        this.loadOverview();
        this.loadTimeline(false);
      } else {
        toast((res.body && res.body.message) || "操作失败", "error");
      }
    },
    /* ── 群务管控 ── */
    async loadGroups() {
      if (this.groups.length) return;
      const res = await get("/api/desktop/moderation/groups");
      if (res.status === 200 && res.body.ok) this.groups = res.body.data.groups;
      else if (res.status === 503) this.groups = [{ group_id: -1, group_name: res.body.message }];
    },
    async pickGroup(groupId) {
      this.selectedGroup = groupId;
      this.members = [];
      if (!groupId || groupId === -1) return;
      const res = await get(`/api/desktop/moderation/groups/${groupId}/members`);
      if (res.status === 200 && res.body.ok) this.members = res.body.data.members;
      else toast((res.body && res.body.message) || "群成员获取失败", "error");
    },
    async muteUser(qq, minutes) {
      if (!this.selectedGroup || this.selectedGroup === -1) {
        toast("请先选择群", "error");
        return;
      }
      const label = minutes > 0 ? `禁言 ${qq} ${minutes} 分钟` : `解除 ${qq} 的禁言`;
      if (!(await confirmBox("群务管控", `确定在群 ${this.selectedGroup} ${label}？`))) return;
      const res = await post("/api/desktop/moderation/mute", {
        group_id: Number(this.selectedGroup),
        user_id: Number(qq),
        minutes: Number(minutes),
      });
      toast(
        res.status === 200 ? `已${minutes > 0 ? "禁言" : "解除"}（${qq}）` : res.body.message,
        res.status === 200 ? "success" : "error"
      );
    },
    async kickUser(qq) {
      if (!this.selectedGroup || this.selectedGroup === -1) {
        toast("请先选择群", "error");
        return;
      }
      const expect = String(this.selectedGroup).slice(-4);
      if (this.kickConfirmInput.trim() !== expect) {
        toast(`防误伤：请先在输入框填入群号后四位（${expect}）`, "error");
        return;
      }
      if (
        !(await confirmBox(
          "踢出群聊（不可轻易撤销）",
          `确定把 ${qq} 踢出群 ${this.selectedGroup}？该动作会记录审计日志。输入群号后四位（${expect}）以确认。`,
          { danger: true, requireType: expect }
        ))
      )
        return;
      const res = await post("/api/desktop/moderation/kick", {
        group_id: Number(this.selectedGroup),
        user_id: Number(qq),
        reject_add_request: true,
      });
      toast(res.status === 200 ? `已踢出（${qq}）` : res.body.message, res.status === 200 ? "success" : "error");
      this.kickConfirmInput = "";
    },
    async recallMessage() {
      if (!this.recallMsgId) {
        toast("请填写消息 ID", "error");
        return;
      }
      if (!(await confirmBox("撤回消息", `确定撤回消息 ${this.recallMsgId}？`, { danger: true }))) return;
      const res = await post("/api/desktop/moderation/recall", {
        message_id: Number(this.recallMsgId),
        user_id: Number(this.selected),
      });
      toast(res.status === 200 ? "已撤回" : res.body.message, res.status === 200 ? "success" : "error");
      this.recallMsgId = null;
    },
  },
  template: `
  <div>
    <page-header title="用户"
                 subtitle="按人看使用情况 · 数据管理权 · 功能封禁 · 群务管控">
      <input type="text" v-model="searchQ" placeholder="按QQ号过滤后回车"
             style="width:200px" spellcheck="false" @keydown.enter="loadUsers" />
      <button class="btn sm ghost" :disabled="loading" @click="loadUsers">刷新</button>
    </page-header>

    <div class="card">
      <data-table :columns="userColumns" :rows="users" row-key="user_id"
                  :loading="loading && !users.length" clickable-rows
                  @row-click="(row) => openUser(row.user_id)"
                  empty-title="近 30 天没有使用记录" empty-icon="👥"
                  empty-text="塔菲被使用后，用户会按活跃度出现在这里。" />
    </div>

    <!-- ══ 单人详情抽屉 ══ -->
    <drawer-panel :open="drawerOpen" title="用户详情" :width="560" @close="closeDrawer">
      <template v-if="!overview">
        <state-panel v-if="overviewLoading" state="loading" :rows="5" />
      </template>
      <template v-else>
        <!-- 概览 -->
        <section>
          <div class="section-title">概览</div>
          <div class="row wrap mb-2">
            <b class="drawer-user-id">👤 {{ overview.user_id }}</b>
            <status-pill v-if="overview.is_owner" label="主人" tone="accent" />
          </div>
          <kv-list :pairs="[
            { k: '7天调用', v: overview.calls_7d + ' 次' },
            { k: '30天调用', v: overview.calls_30d + ' 次' },
            { k: '进行中预警', v: overview.alerts.active + ' 条' },
            { k: '已触发 / 失败', v: overview.alerts.delivered + ' / ' + overview.alerts.failed },
            { k: '自选', v: overview.watch_count + ' 条' },
          ]" />
          <p v-if="overview.persona" class="faint mt-2">
            人格学习{{ overview.persona.learning_enabled ? '开' : '关' }} · 记忆 {{ overview.persona.memory_count }} 条
          </p>
          <div v-if="(overview.top_commands || []).length" class="mt-2">
            <span class="faint">常用命令：</span>
            <span v-for="c in overview.top_commands.slice(0, 6)" :key="c.command" class="pill neutral cmd-pill">
              {{ c.command }} ×{{ c.calls }}
            </span>
          </div>
        </section>

        <hr class="divider" />

        <!-- 时间线 -->
        <section>
          <div class="row wrap mb-2">
            <div class="section-title grow mb-0">使用时间线</div>
            <select v-model.number="daysFilter" aria-label="时间范围" style="width:auto" @change="loadTimeline(false)">
              <option :value="1">近1天</option><option :value="7">近7天</option>
              <option :value="30">近30天</option><option :value="90">近90天</option>
            </select>
          </div>
          <state-panel v-if="timelineLoading && !events.length" state="loading" :rows="3" tight />
          <div v-else-if="events.length">
            <div v-for="e in events" :key="e.id" class="tl-row">
              <span class="faint num">{{ fmtTime(e.created_at) }}</span>
              [{{ e.feature }}] {{ e.command }}
              <span v-if="e.group_id" class="faint">· 群 {{ e.group_id }}</span>
            </div>
            <button v-if="nextCursor" class="btn sm ghost mt-2" :disabled="timelineLoading" @click="loadTimeline(true)">
              {{ timelineLoading ? '加载中…' : '加载更多' }}
            </button>
          </div>
          <state-panel v-else state="empty" icon="🕘" title="所选时间范围无使用记录" tight />
        </section>

        <hr class="divider" />

        <!-- 聊天流水 -->
        <section>
          <app-tabs :tabs="[
            { key: 'group', label: '聊天流水' },
            { key: 'private', label: '私聊（需确认）' },
          ]" v-model:active="chatScope" />
          <div class="row wrap mb-2 mt-2">
            <select v-model.number="chatDays" aria-label="聊天范围" style="width:auto">
              <option :value="1">近1天</option><option :value="3">近3天</option>
              <option :value="7">近7天</option><option :value="30">近30天</option>
            </select>
            <button class="btn sm" :disabled="chatLoading" @click="loadChat(false)">
              {{ chatScope==='private' ? '二段确认后查看私聊' : '加载聊天流水' }}
            </button>
            <span class="faint">最多200条 · 查看他人私聊会记录审计</span>
          </div>
          <div v-if="chatPrivacyNotice" class="alert-strip thin">{{ chatPrivacyNotice }}</div>
          <div v-if="chatMessages.length" class="chat-log-preview">
            <div v-for="(m, i) in chatMessages" :key="i" class="log-line">
              <span class="faint num">{{ fmtTime(m.created_at) }} [{{ m.session_key }}] {{ m.role==='user' ? '👤' : '🐱' }}</span>
              <div class="pre-wrap">{{ m.content }}</div>
            </div>
          </div>
          <state-panel v-else state="empty" icon="💬" :title="chatLoaded ? '无记录' : '点击上方按钮加载'"
                       message="桌面不主动拉取历史消息，需手动触发。" tight />
        </section>

        <hr class="divider" />

        <!-- 功能封禁 -->
        <section>
          <div class="section-title">🚫 功能封禁 <span class="faint">即时生效：下一条命令即吃瘪</span></div>
          <button class="btn sm danger mb-2" @click="banAll">全禁（聊天+行情+娱乐）</button>
          <div class="ban-grid">
            <div v-for="f in features" :key="f.name" class="row ban-item">
              <span>{{ f.label || f.name }}</span>
              <div class="spacer"></div>
              <status-pill v-if="overrides[f.name]===false" label="已封禁" tone="danger" />
              <button class="btn sm" :class="overrides[f.name]===false ? 'ghost' : ''"
                      @click="setFeature(f.name, overrides[f.name]!==false ? false : null)">
                {{ overrides[f.name]===false ? '恢复默认' : '封禁' }}
              </button>
            </div>
          </div>
        </section>

        <hr class="divider" />

        <!-- 群务管控 -->
        <section>
          <div class="section-title">🛡️ 群务管控 <span class="faint">需塔菲在该群为管理员</span></div>
          <div class="row wrap mb-2">
            <select style="width:auto" aria-label="选择群" @change="pickGroup($event.target.value)">
              <option :value="null">选择群…</option>
              <option v-for="g in groups" :key="g.group_id" :value="g.group_id">{{ g.group_name }}（{{ g.group_id }}）</option>
            </select>
            <input type="text" v-model="memberFilter" placeholder="过滤群成员" style="width:150px" spellcheck="false" />
          </div>
          <div class="row wrap mb-2">
            <span class="faint">对 {{ selected }}：</span>
            <input type="number" v-model.number="muteMinutes" min="0" max="43200" aria-label="禁言分钟数" style="width:80px" />
            <button class="btn sm" @click="muteUser(selected, muteMinutes)">禁言(分钟)</button>
            <button class="btn sm ghost" @click="muteUser(selected, 0)">解除禁言</button>
          </div>
          <div class="row wrap mb-2">
            <input type="text" v-model="kickConfirmInput" placeholder="群号后四位" style="width:110px" spellcheck="false" />
            <button class="btn sm danger" @click="kickUser(selected)">踢出本群</button>
            <input type="number" v-model.number="recallMsgId" placeholder="消息ID（撤回）" aria-label="消息ID" style="width:160px" />
            <button class="btn sm ghost" @click="recallMessage">撤回</button>
          </div>
          <details v-if="members.length">
            <summary class="muted clickable-summary">群成员列表（点击对他人操作）</summary>
            <div class="member-list mt-2">
              <div v-for="m in filteredMembers" :key="m.user_id" class="row member-row">
                <span>{{ m.user_id }} {{ m.nickname }}</span>
                <div class="spacer"></div>
                <button class="btn sm ghost" @click="muteUser(m.user_id, 10)">禁10min</button>
                <button class="btn sm ghost ml-1" @click="muteUser(m.user_id, 0)">解禁</button>
              </div>
            </div>
          </details>
        </section>

        <hr class="divider" />

        <!-- 数据管理权 -->
        <section>
          <div class="section-title">🧹 数据管理权 <span class="faint">均需二次确认</span></div>
          <div class="row wrap">
            <button class="btn sm ghost" @click="dataAction('clear_session')">清空会话</button>
            <button class="btn sm ghost" @click="dataAction('clear_memory')">清空长期记忆</button>
            <button class="btn sm ghost" @click="dataAction('cancel_alerts')">取消全部预警</button>
            <button class="btn sm danger" @click="dataAction('clear_watchlist')">删除全部自选</button>
          </div>
        </section>

        <hr class="divider" />

        <!-- 近期管理动作 -->
        <section v-if="moderationTrail.length">
          <div class="section-title">近期管理动作（谁何时动了TA）</div>
          <div v-for="(t, i) in moderationTrail.slice(0, 6)" :key="i" class="tl-row muted">
            {{ fmtTime(t.created_at) }} · {{ t.detail }}
          </div>
        </section>
      </template>
    </drawer-panel>
  </div>
  `,
};
