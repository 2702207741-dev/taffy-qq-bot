/* views/inventory.js — 库存监控（6.5）：账号健康卡 / 订阅 DataTable / 变更时间线 /
   囤货轨迹查询 + 画像缓存只读；SteamID 即时校验提示。 */

import { get } from "../api.js";
import { fmtTime } from "../ui.js";

const STEAM_ID_RE = /^\d{17}$/;

export default {
  data() {
    return {
      state: null,
      loading: false,
      addForm: { steamId: "", nickname: "", destType: "group", destId: "" },
      addTried: false,
      trajForm: { steamId: "", days: 30 },
      trajLoading: false,
      trajText: "",
      trajError: "",
      profileForm: { steamId: "" },
      profileLoading: false,
      profileText: "",
      profileError: "",
      profileMeta: "",
    };
  },
  computed: {
    accounts() {
      return (this.state && this.state.accounts) || [];
    },
    subscriptions() {
      return (this.state && this.state.subscriptions) || [];
    },
    events() {
      return (this.state && this.state.events) || [];
    },
    /* 添加账号表单即时校验 */
    steamIdError() {
      const v = String(this.addForm.steamId || "").trim();
      if (!this.addTried && !v) return "";
      if (!STEAM_ID_RE.test(v)) return "SteamID 必须是 17 位数字（Steam64）";
      return "";
    },
    nicknameError() {
      if (!this.addTried) return "";
      return this.addForm.nickname.trim() ? "" : "请填写备注昵称";
    },
    destIdError() {
      if (!this.addTried) return "";
      const id = Number(this.addForm.destId);
      if (!Number.isInteger(id) || id <= 0)
        return this.addForm.destType === "group" ? "群号必须是正整数" : "QQ 号必须是正整数";
      return "";
    },
    subColumns() {
      return [
        { key: "owner_user_id", label: "QQ" },
        {
          key: "_dest",
          label: "目的地",
          render: (row) => (row.destination_type === "group" ? `群 ${row.destination_id}` : "私聊"),
        },
        { key: "steam_id", label: "SteamID", render: (row) => String(row.steam_id) },
        { key: "nickname", label: "备注" },
      ];
    },
  },
  mounted() {
    this.refresh();
    this.onRefresh = () => this.refresh();
    window.addEventListener("view-refresh", this.onRefresh);
  },
  unmounted() {
    window.removeEventListener("view-refresh", this.onRefresh);
  },
  methods: {
    fmtTime,
    validateSteamId(value) {
      const v = String(value || "").trim();
      if (!STEAM_ID_RE.test(v)) return "SteamID 必须是 17 位数字（Steam64）";
      return "";
    },
    healthOf(account) {
      if (account.consecutive_failures > 0) return { tone: "danger", label: "异常" };
      if (Date.now() / 1000 - account.last_success_at > 86400 * 2) return { tone: "warn", label: "久未快照" };
      return { tone: "ok", label: "健康" };
    },
    async refresh() {
      this.loading = !this.state;
      const res = await get("/api/desktop/inventory/state?event_limit=20");
      this.state = res.status === 200 && res.body.ok ? res.body.data : null;
      this.loading = false;
    },
    /* 添加账号/订阅走 QQ 群内命令或 admin 面板同源能力；桌面端此处仅做校验提示 */
    submitAdd() {
      this.addTried = true;
      if (this.steamIdError || this.nicknameError || this.destIdError) return;
      // A3：仓库无"桌面直接添加监控"端点；引导使用与群内一致的命令
      window.alert(
        `请在 QQ 内对塔菲发送：\n库存监控 ${String(this.addForm.steamId).trim()}\n` +
          `（桌面直连添加能力待人工决策后开放）`
      );
    },
    pickAccount(steamId) {
      this.trajForm.steamId = steamId;
      this.profileForm.steamId = steamId;
    },
    async queryTrajectory() {
      const err = this.validateSteamId(this.trajForm.steamId);
      this.trajError = err;
      if (err) {
        this.trajText = "";
        return;
      }
      this.trajLoading = true;
      this.trajText = "";
      const res = await get(
        `/api/desktop/inventory/trajectory?steam_id=${encodeURIComponent(this.trajForm.steamId.trim())}&days=${this.trajForm.days}`
      );
      this.trajLoading = false;
      if (res.status === 200 && res.body.ok) {
        this.trajText = String(res.body.data.ok_text || "");
      } else {
        this.trajError = `查询失败（${res.status}）`;
      }
    },
    async queryProfile() {
      const err = this.validateSteamId(this.profileForm.steamId);
      this.profileError = err;
      if (err) {
        this.profileText = "";
        this.profileMeta = "";
        return;
      }
      this.profileLoading = true;
      this.profileText = "";
      this.profileMeta = "";
      const res = await get(
        `/api/desktop/inventory/profile?steam_id=${encodeURIComponent(this.profileForm.steamId.trim())}`
      );
      this.profileLoading = false;
      if (res.status === 200 && res.body.ok) {
        this.profileText = String(res.body.data.ok_text || "");
        this.profileMeta = res.body.data.cached
          ? `模型 ${res.body.data.model} · 生成于 ${res.body.data.generated_label}（缓存）`
          : "";
      } else {
        this.profileError = `查询失败（${res.status}）`;
      }
    },
  },
  template: `
  <div>
    <page-header title="库存监控"
                 subtitle="多账号库存快照 · 囤货轨迹与画像 · 与群内命令同源">
      <template #meta>
        <span v-if="loading" class="muted">刷新中…</span>
      </template>
      <button class="btn sm ghost" @click="refresh">刷新</button>
    </page-header>

    <!-- 监控账号 -->
    <div class="grid-3 mb-3">
      <div v-for="a in accounts" :key="a.steam_id"
           class="card account-card" role="button" tabindex="0"
           title="点击填入下方查询框"
           @click="pickAccount(a.steam_id)" @keydown.enter.prevent="pickAccount(a.steam_id)">
        <div class="row wrap mb-1">
          <b>{{ a.nickname }}</b>
          <status-pill class="grow-shrink ml-auto"
                       :label="healthOf(a).label" :tone="healthOf(a).tone" />
        </div>
        <kv-list :pairs="[
          { k: 'SteamID', v: a.steam_id },
          { k: '快照', v: fmtTime(a.source_time) },
          { k: '下次检查', v: fmtTime(a.next_check_at) },
        ]" />
        <p v-if="a.last_error" class="field-msg err mt-1" role="alert">{{ a.last_error }}</p>
      </div>
      <state-panel v-if="!loading && !accounts.length" state="empty" icon="📦" title="暂无监控账号"
                   message="在 QQ 内对塔菲发送「库存监控 Steam64」即可添加第一个账号。" />
    </div>

    <div class="grid-2">
      <!-- 囤货轨迹 -->
      <div class="card">
        <h3>囤货轨迹 <span class="faint">30 天进出摘要</span></h3>
        <div class="row wrap">
          <input type="text" v-model="trajForm.steamId" placeholder="Steam64 ID"
                 :class="{ 'is-invalid': !!trajError }" spellcheck="false" class="grow min-180"
                 @keydown.enter="queryTrajectory" />
          <select v-model.number="trajForm.days" aria-label="统计天数" style="width:auto">
            <option :value="7">近7天</option><option :value="30">近30天</option><option :value="90">近90天</option>
          </select>
          <button class="btn" :disabled="trajLoading" @click="queryTrajectory">
            <span v-if="trajLoading" class="spin" aria-hidden="true"></span>{{ trajLoading ? '查询中' : '查轨迹' }}
          </button>
        </div>
        <p v-if="trajError" class="field-msg err mt-1" role="alert">{{ trajError }}</p>
        <pre v-if="trajText" class="code-block mt-2">{{ trajText }}</pre>
      </div>

      <!-- 囤货画像 -->
      <div class="card">
        <h3>囤货画像 <span class="faint">缓存只读</span></h3>
        <div class="row wrap">
          <input type="text" v-model="profileForm.steamId" placeholder="Steam64 ID"
                 :class="{ 'is-invalid': !!profileError }" spellcheck="false" class="grow"
                 @keydown.enter="queryProfile" />
          <button class="btn" :disabled="profileLoading" @click="queryProfile">
            <span v-if="profileLoading" class="spin" aria-hidden="true"></span>{{ profileLoading ? '查询中' : '查画像' }}
          </button>
        </div>
        <p class="faint mt-1">面板不直接调用模型；生成/刷新请在 QQ 内发「囤货画像 SteamID」，此处查看缓存结果。</p>
        <p v-if="profileMeta" class="muted mt-1">{{ profileMeta }}</p>
        <p v-if="profileError" class="field-msg err mt-1" role="alert">{{ profileError }}</p>
        <pre v-if="profileText" class="code-block mt-2">{{ profileText }}</pre>
      </div>
    </div>

    <div class="grid-2 mt-3">
      <!-- 订阅关系 -->
      <div class="card">
        <h3>订阅关系</h3>
        <data-table :columns="subColumns" :rows="subscriptions" row-key="steam_id"
                    :loading="loading && !state"
                    empty-title="暂无订阅" empty-text="添加监控账号后，可指定变更事件推送到群或私聊。"
                    empty-icon="📮" />
      </div>

      <!-- 添加账号 / 订阅 -->
      <div class="card">
        <h3>添加账号 / 订阅</h3>
        <div class="form-row">
          <label for="inv-steam">Steam64 ID</label>
          <input id="inv-steam" type="text" v-model="addForm.steamId" placeholder="76561198000000000"
                 spellcheck="false" :class="{ 'is-invalid': !!steamIdError }" />
          <span v-if="steamIdError" class="field-msg err" role="alert">{{ steamIdError }}</span>
        </div>
        <div class="form-row">
          <label for="inv-nick">备注昵称</label>
          <input id="inv-nick" type="text" v-model="addForm.nickname"
                 :class="{ 'is-invalid': !!nicknameError }" />
          <span v-if="nicknameError" class="field-msg err" role="alert">{{ nicknameError }}</span>
        </div>
        <div class="row wrap">
          <div class="form-row grow">
            <label for="inv-type">类型</label>
            <select id="inv-type" v-model="addForm.destType">
              <option value="group">群</option><option value="private">私聊</option>
            </select>
          </div>
          <div class="form-row grow">
            <label for="inv-dest">群号 / QQ</label>
            <input id="inv-dest" type="number" v-model.number="addForm.destId"
                   :class="{ 'is-invalid': !!destIdError }" />
            <span v-if="destIdError" class="field-msg err" role="alert">{{ destIdError }}</span>
          </div>
        </div>
        <button class="btn mt-2" @click="submitAdd">生成添加指引</button>
      </div>
    </div>

    <!-- 变更事件时间线 -->
    <div class="card mt-3">
      <h3>变更事件时间线</h3>
      <ul v-if="events.length" class="timeline">
        <li v-for="e in events" :key="e.id">
          <b>{{ e.nickname }}</b> <span class="muted num">{{ fmtTime(e.created_at) }}</span>
          <pre class="code-block mt-1">{{ e.summary }}</pre>
        </li>
      </ul>
      <state-panel v-else-if="!loading" state="empty" icon="🕒" title="最近无变更事件"
                   message="账号库存发生变化时会按订阅推送，并在此留痕。" tight />
    </div>
  </div>
  `,
};
