/* views/dashboard.js — 仪表盘（6.1）：可配置磁贴 StatCard 化 + 统一三态，
   装备指数带 sparkline（index-closes 尾段）；拖拽排序与 dashboard_tiles 记忆保留。 */

import { get, admin } from "../api.js";
import { store } from "../store.js";

export default {
  data() {
    return {
      store, // 页头副标题/磁贴记忆需要模板直接访问
      overview: null,
      overviewError: "",
      watchlist: null,
      alertsSummary: null,
      events: [],
      indexPoints: null,
      loading: false,
      lastRefreshedAt: "",
      dragIndex: -1,
      overIndex: -1,
      csqaqStatus: null,
      tiles: [...(store.settings.dashboard_tiles || [])],
    };
  },
  computed: {
    tileOrder() {
      return this.tiles.filter((tile) =>
        ["index", "temperature", "breadth", "watchlist", "alerts", "events"].includes(tile)
      );
    },
    firstLoad() {
      return this.loading && !this.overview && !this.watchlist && !this.events.length;
    },
    csqaqTone() {
      const s = this.csqaqStatus;
      if (!s || s.state === "closed") return "ok";
      return s.state === "open" ? "danger" : "warn";
    },
    csqaqLabel() {
      const s = this.csqaqStatus;
      if (!s) return "未配置";
      if (s.state === "open") return "熔断中";
      if (s.state === "half_open") return "试探恢复";
      return s.recent_failures > 0 ? `正常（近错 ${s.recent_failures}）` : "正常";
    },
    temperatureAvailable() {
      return Boolean(this.overview && this.overview.temperature && this.overview.temperature.available);
    },
  },
  mounted() {
    this.refresh();
    this.timer = setInterval(this.refresh, (store.settings.refresh_interval_seconds || 30) * 1000);
    this.onRefresh = () => this.refresh();
    window.addEventListener("view-refresh", this.onRefresh);
    this.onScene = () => {
      if (store.settings.active_scene === "早报速览") this.tiles = ["index", "temperature", "watchlist", "events"];
    };
    window.addEventListener("scene-change", this.onScene);
  },
  unmounted() {
    clearInterval(this.timer);
    window.removeEventListener("view-refresh", this.onRefresh);
    window.removeEventListener("scene-change", this.onScene);
  },
  methods: {
    async refresh() {
      this.loading = true;
      this.overviewError = "";
      const [overviewRes, watchlistRes, alertsRes, eventsRes] = await Promise.allSettled([
        get("/api/desktop/market/overview"),
        get("/api/desktop/market/watchlist-quotes"),
        admin("GET", "/api/admin/alerts?limit=200&status=pending"),
        admin("GET", "/api/admin/events?limit=20"),
      ]);
      if (overviewRes.status === "fulfilled" && overviewRes.value.body.ok) {
        this.overview = overviewRes.value.body.data;
        this.loadIndexTrend(); // sparkline 数据源独立成败
      } else if (overviewRes.status === "rejected") {
        this.overviewError = "大盘磁贴数据加载失败";
      }
      if (watchlistRes.status === "fulfilled" && watchlistRes.value.body.ok)
        this.watchlist = watchlistRes.value.body.data;
      if (alertsRes.status === "fulfilled" && alertsRes.value)
        this.alertsSummary = { count: alertsRes.value.count, total: alertsRes.value.total };
      if (eventsRes.status === "fulfilled" && eventsRes.value) this.events = eventsRes.value.events || [];
      const summaryRes = await admin("GET", "/api/desktop/system/summary");
      if (summaryRes && summaryRes.csqaq_circuits) {
        const detail = summaryRes.csqaq_circuits["provider.csqaq_detail"] || { state: "closed", recent_failures: 0 };
        this.csqaqStatus = detail;
      }
      this.lastRefreshedAt = new Date().toLocaleTimeString("zh-CN", { hour12: false });
      this.loading = false;
    },
    async loadIndexTrend() {
      const res = await get("/api/desktop/market/index-closes?days=30");
      if (res.status === 200 && res.body.ok) {
        const closes = (res.body.data.points || []).map((p) => p.close);
        this.indexPoints = closes.slice(-24); // 近一个月收盘作为迷你趋势
      }
    },
    onDragStart(index) {
      this.dragIndex = index;
    },
    onDragOver(index) {
      this.overIndex = index;
    },
    onDrop(targetIndex) {
      const from = this.dragIndex;
      if (from < 0 || from === targetIndex) return;
      const order = [...this.tiles];
      const [moved] = order.splice(from, 1);
      order.splice(targetIndex, 0, moved);
      this.tiles = order;
      store.settings.dashboard_tiles = order; // 拖拽后记忆（Q7）
      this.dragIndex = -1;
      this.overIndex = -1;
    },
    go(path) {
      location.hash = `#/${path}`;
    },
    tempStateTone(state) {
      return { 过热: "danger", 偏热: "warn", 适中: "ok", 偏冷: "info", 冰点: "info" }[state] || "neutral";
    },
  },
  template: `
  <div>
    <page-header title="仪表盘"
                 :subtitle="'每 ' + (store.settings.refresh_interval_seconds || 30) + ' 秒自动刷新' + (lastRefreshedAt ? ' · 最近刷新 ' + lastRefreshedAt : '')">
      <template #meta>
        <status-pill :label="store.online ? '在线' : '离线'" :tone="store.online ? 'ok' : 'danger'" title="塔菲连接状态" />
        <status-pill :label="'比价源 ' + csqaqLabel" :tone="csqaqTone" title="CSQAQ 比价数据源熔断状态" />
      </template>
      <span class="faint">拖拽磁贴可排序（自动记忆）</span>
      <button v-if="overviewError" class="btn sm ghost" @click="refresh">重试</button>
    </page-header>

    <div class="grid-2">
      <template v-for="(tile, i) in tileOrder" :key="tile">
        <div class="tile"
             :class="{ dragging: dragIndex === i, 'drag-over': overIndex === i }"
             draggable="true"
             @dragstart="onDragStart(i)" @dragover.prevent="onDragOver(i)"
             @drop.prevent="onDrop(i)" @dragend="dragIndex = -1">

          <!-- 装备指数 -->
          <stat-card v-if="tile === 'index' && !firstLoad" label="装备指数"
                     :value="overview && overview.broad ? overview.broad.index.toFixed(1) : '--'"
                     :delta="overview && overview.broad ? overview.broad.change_percent : null"
                     :points="indexPoints" clickable @click="go('market')" />
          <!-- 市场温度 -->
          <stat-card v-else-if="tile === 'temperature' && !firstLoad" label="市场温度"
                     :value="overview && overview.temperature && overview.temperature.available
                              ? (overview.temperature.score > 0 ? '+' : '') + Math.round(overview.temperature.score)
                              : '--'"
                     unit="°" clickable @click="go('market')">
            <template #footer>
              <div v-if="overview && overview.temperature && overview.temperature.available" class="row wrap mt-1">
                <status-pill :label="overview.temperature.state" :tone="tempStateTone(overview.temperature.state)" />
                <span class="faint">
                  3日 {{ overview.temperature.factors.change_3d.toFixed(1) }}% ·
                  7日 {{ overview.temperature.factors.change_7d.toFixed(1) }}% ·
                  偏离 {{ overview.temperature.factors.deviation_7d.toFixed(1) }}%
                </span>
              </div>
              <span v-else-if="overview" class="muted">暂不可用（{{ overview.temperature?.reason || '-' }}）</span>
            </template>
          </stat-card>
          <!-- 市场广度 -->
          <stat-card v-else-if="tile === 'breadth' && !firstLoad" label="市场广度"
                     :value="overview && overview.breadth ? overview.breadth.rising_ratio.toFixed(1) : '--'"
                     unit="%上涨占比" clickable @click="go('market')">
            <template #footer>
              <span v-if="overview && overview.breadth" class="muted">
                {{ overview.breadth.compared }} 件同样本 · 中位涨跌 {{ overview.breadth.median_change.toFixed(2) }}%
              </span>
            </template>
          </stat-card>
          <!-- 预警 -->
          <stat-card v-else-if="tile === 'alerts' && !firstLoad" label="活跃预警"
                     :value="alertsSummary ? alertsSummary.count : '--'"
                     :delta-label="alertsSummary ? '共 ' + alertsSummary.total + ' 条历史' : ''"
                     clickable @click="go('alerts')">
            <template #footer><span class="muted">点按进入预警管理</span></template>
          </stat-card>

          <!-- 我的自选：列表型磁贴 -->
          <div v-else-if="tile === 'watchlist' && !firstLoad" class="card stat-card">
            <h3>我的自选 <button class="btn sm ghost ml-auto" @click="go('market')">详情 →</button></h3>
            <div v-if="watchlist && watchlist.items.length">
              <div v-for="item in watchlist.items.slice(0, 6)" :key="item.display_name"
                   class="row watch-row" role="button" tabindex="0" @click="go('market')"
                   @keydown.enter.prevent="go('market')">
                <span class="cell-ellipsis grow" :title="item.display_name">{{ item.display_name }}</span>
                <span v-if="item.snapshot" class="td-num num">{{ item.snapshot.latest_price.toFixed(2) }}</span>
                <span v-else class="muted">--</span>
                <span v-if="item.snapshot" class="num wl-week"
                      :class="item.snapshot.week_change >= 0 ? 'up' : 'down'">
                  {{ item.snapshot.week_change >= 0 ? '+' : '' }}{{ item.snapshot.week_change.toFixed(1) }}%
                </span>
              </div>
            </div>
            <state-panel v-else state="empty" icon="⭐" title="自选为空"
                         message="行情在跑，塔菲在盯盘喵～在市场页搜索并添加，或在 QQ 里对塔菲发「加自选 饰品名」。">
              <button class="btn sm ghost" @click="go('market')">去市场页添加</button>
            </state-panel>
          </div>

          <!-- 事件流 -->
          <div v-else-if="tile === 'events' && !firstLoad" class="card">
            <h3>事件流 <span class="faint">最近 20 条</span></h3>
            <ul v-if="events.length" class="timeline">
              <li v-for="event in events.slice(0, 8)" :key="event.key">
                <span class="muted num">{{ new Date(event.time * 1000).toLocaleTimeString('zh-CN', { hour12: false }) }}</span>
                {{ event.summary }}
              </li>
            </ul>
            <state-panel v-else state="empty" icon="🗒️" title="暂无事件"
                         message="风平浪静，塔菲在角落打盹喵…预警触发、定时任务等事件将在此滚动展示。" tight />
          </div>

          <!-- 首次加载骨架 -->
          <div v-else class="card"><state-panel state="loading" :rows="2" tight /></div>
        </div>
      </template>
    </div>
  </div>
  `,
};
