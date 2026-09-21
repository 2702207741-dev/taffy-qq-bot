/* views/market.js — 市场（6.3）：搜索（防抖+键盘↑↓）/ 详情 KVList+K线三态 /
   大盘 ChartCard / 榜单 / 自选 DataTable。图表配色全部走 chart-theme 令牌。 */

import { get, post, admin, del as apiDelete } from "../api.js";
import { store } from "../store.js";
import { toast, confirmBox, fmtPrice, fmtPercent } from "../ui.js";
import { buildChartTheme, chartTextStyles } from "../components/chart-theme.js";

export default {
  data() {
    return {
      tab: "search",
      query: "",
      results: [],
      resultHl: -1,
      searching: false,
      detail: null,
      detailError: "",
      currentName: "",
      kline: null,
      klineLoading: false,
      klineError: "",
      klineSource: "",
      compare: null,
      compareError: "",
      score: null,
      wearVariants: [],
      specialStyle: "",
      compare: null,
      compareError: "",
      compareLoading: false,
      score: null,
      scoreLoading: false,
      hoard: null,
      hoardLoading: false,
      caseEv: null,
      caseEvLoading: false,
      overview: null,
      overviewChart: null,
      dipText: "",
      predText: "",
      rankingsBoard: "异动榜",
      rankingsCategory: "",
      rankingsLimit: 10,
      rankingsText: "",
      watchlist: null,
      watchLoading: false,
      removingIndex: -1,
    };
  },
  computed: {
    styles() {
      return ["", "黑珍珠", "蓝宝石", "绿宝石", "红宝石", "单面全蓝", "官图太阳"];
    },
    groups() {
      return Object.keys(store.settings.watchlist_groups || {});
    },
    quoteColumns() {
      return [
        { key: "platform", label: "平台" },
        { key: "sell_price", label: "在售 ¥", align: "right", render: (row) => fmtPrice(row.sell_price) },
        { key: "sell_count", label: "挂单量", align: "right", render: (row) => (row.sell_count ?? "--") },
        { key: "bid_price", label: "求购 ¥", align: "right", render: (row) => (row.bid_price != null ? fmtPrice(row.bid_price) : "--") },
        { key: "bid_count", label: "求购量", align: "right", render: (row) => (row.bid_count ?? "--") },
      ];
    },
    watchColumns() {
      const { h } = Vue;
      const self = this;
      return [
        {
          key: "display_name",
          label: "饰品",
          ellipsis: true,
          render: (row) => String(row.display_name),
        },
        {
          key: "_price",
          label: "最新采样价",
          align: "right",
          sortValue: (row) => (row.snapshot ? row.snapshot.latest_price : null),
          render: (row) => (row.snapshot ? fmtPrice(row.snapshot.latest_price) : "--"),
        },
        {
          key: "_week",
          label: "7日变化",
          align: "right",
          sortable: true,
          sortValue: (row) => (row.snapshot ? row.snapshot.week_change : null),
          render: (row) =>
            row.snapshot
              ? Vue.h(
                  "span",
                  { class: row.snapshot.week_change >= 0 ? "up" : "down" },
                  fmtPercent(row.snapshot.week_change)
                )
              : "--",
        },
        {
          key: "_group",
          label: "分组",
          render: (row) =>
            h("select", {
              value: self.groupOf(row.display_name),
              "aria-label": `设置 ${row.display_name} 的分组`,
              onChange: (e) => self.moveToGroup(row.display_name, e.target.value),
            }, [
              h("option", { value: "" }, "未分组"),
              ...self.groups.map((g) => h("option", { value: g }, g)),
            ]),
        },
        {
          key: "_op",
          label: "",
          width: "56px",
          align: "right",
          render: (row) => {
            const index = self.watchlist.items.indexOf(row);
            return h(
              "button",
              {
                class: "btn sm danger",
                disabled: self.removingIndex === index,
                onClick: () => self.removeWatchItem(row, index),
              },
              self.removingIndex === index ? "…" : "删"
            );
          },
        },
      ];
    },
    csqaqColumns() {
      return [
        { key: "platform", label: "平台" },
        { key: "sell_price", label: "在售 ¥", align: "right", render: (row) => (row.sell_price != null ? fmtPrice(row.sell_price) : "--") },
        { key: "sell_num", label: "在售量", align: "right", render: (row) => (row.sell_num ?? "--") },
        { key: "buy_price", label: "求购 ¥", align: "right", render: (row) => (row.buy_price != null ? fmtPrice(row.buy_price) : "--") },
        { key: "buy_num", label: "求购量", align: "right", render: (row) => (row.buy_num ?? "--") },
      ];
    },
    comparePairs() {
      if (!this.compare) return [];
      const pairs = [];
      const cq = this.compare.csqaq;
      if (cq && cq.supply_count != null) pairs.push({ k: "存世量", v: `${cq.supply_count.toLocaleString()} 件` });
      if (cq && cq.rent) {
        const parts = [];
        if (cq.rent.short_price != null) parts.push(`日租 ¥${cq.rent.short_price}`);
        if (cq.rent.short_annual_yield != null) parts.push(`短租年化 ${cq.rent.short_annual_yield}%`);
        if (cq.rent.long_annual_yield != null) parts.push(`长租年化 ${cq.rent.long_annual_yield}%`);
        if (parts.length) pairs.push({ k: "租金参考", v: parts.join(" · ") });
      }
      for (const s of this.compare.spreads || []) {
        pairs.push({ k: `价差 ${s.platform}`, v: `${s.diff >= 0 ? "+" : ""}${s.diff}（${s.diff_percent}%）` });
      }
      if (this.compare.csqaq_error) pairs.push({ k: "CSQAQ", v: this.compare.csqaq_error });
      return pairs;
    },
    scoreFactors() {
      if (!this.score || !this.score.scored) return [];
      const rows = [];
      for (const side of ["buy", "sell"]) {
        for (const f of this.score[side].factors) {
          rows.push({ key: `${side}-${f.name}`, side: side === "buy" ? "买入" : "卖出", name: f.name, score: f.score, weight: `${Math.round(f.weight * 100)}%`, basis: f.basis });
        }
      }
      return rows;
    },
    detailSummaryPairs() {
      if (!this.detail) return [];
      const pairs = [];
      if (this.detail.average) {
        let v = `7日均价 ${fmtPrice(this.detail.average.overall)}`;
        if (this.detail.average.buff) v += ` · BUFF ${fmtPrice(this.detail.average.buff)}`;
        pairs.push({ k: "均价参考", v });
      }
      if (this.detail.change_24h) {
        pairs.push({ k: "24h", v: `${fmtPrice(this.detail.change_24h.latest_price)}（${fmtPercent(this.detail.change_24h.change_percent)}）` });
      }
      if (this.wearVariants.length) {
        pairs.push({ k: "磨损档", v: this.wearVariants.map((w) => w.wear).join(" / ") });
      }
      return pairs;
    },
    hoardPairs() {
      if (!this.hoard || this.hoard.available === false) return [];
      const pairs = [];
      const h = this.hoard;
      if (h.median_ratio) pairs.push({ k: "挂刀比例中位", v: `${(h.median_ratio * 100).toFixed(2)}%` });
      if (h.p25_ratio) pairs.push({ k: "P25", v: `${(h.p25_ratio * 100).toFixed(2)}%` });
      if (h.p75_ratio) pairs.push({ k: "P75", v: `${(h.p75_ratio * 100).toFixed(2)}%` });
      if (h.hoard_ratio !== undefined && h.hoard_ratio !== null) {
        pairs.push({ k: "本品挂刀比例", v: `${(h.hoard_ratio * 100).toFixed(2)}%` });
      }
      if (h.fetched_at) pairs.push({ k: "快照时间", v: h.fetched_at });
      if (!pairs.length) pairs.push({ k: "状态", v: h.reason || "积累中" });
      return pairs;
    },
    caseEvPairs() {
      if (!this.caseEv || this.caseEv.available === false) return [];
      const c = this.caseEv;
      const pairs = [];
      if (c.display_name) pairs.push({ k: "箱子", v: c.display_name });
      if (c.ev_lower_bound !== undefined) pairs.push({ k: "期望值(EV,下界)", v: fmtPrice(c.ev_lower_bound) });
      if (c.case_price !== undefined) pairs.push({ k: "箱子现价", v: fmtPrice(c.case_price) });
      if (c.return_rate_percent !== undefined && c.return_rate_percent !== null) {
        pairs.push({ k: "回报率", v: `${c.return_rate_percent}%` });
      }
      if (c.special_count) pairs.push({ k: "特殊物品", v: `${c.special_count} 件（不逐一切价，EV 为下界）` });
      return pairs;
    },
  },
  mounted() {
    this.onRefresh = () => {
      if (this.tab === "overview") this.loadOverview();
      if (this.tab === "watchlist") this.loadWatchlist(false);
    };
    window.addEventListener("view-refresh", this.onRefresh);
    const focus = sessionStorage.getItem("market-focus");
    if (focus) {
      sessionStorage.removeItem("market-focus");
      this.query = focus;
      this.tab = "search";
      this.search();
    }
  },
  unmounted() {
    window.removeEventListener("view-refresh", this.onRefresh);
    if (this.chart) this.chart.dispose();
    if (this.overviewChart) this.overviewChart.dispose();
  },
  methods: {
    fmtPrice,
    fmtPercent,
    /* ── 搜索 ── */
    onSearchKeydown(event) {
      if (!this.results.length) return;
      if (event.key === "ArrowDown") {
        event.preventDefault();
        this.resultHl = Math.min(this.resultHl + 1, this.results.length - 1);
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        this.resultHl = Math.max(this.resultHl - 1, 0);
      }
    },
    async search() {
      const q = this.query.trim();
      if (!q) return;
      this.searching = true;
      clearTimeout(this.debounce);
      this.debounce = setTimeout(async () => {
        const res = await get(`/api/desktop/market/search?q=${encodeURIComponent(q)}&limit=20`);
        this.results = res.status === 200 && res.body.ok ? res.body.data.results : [];
        this.resultHl = this.results.length ? 0 : -1;
        this.searching = false;
        if (this.results.length) await this.openDetail(this.results[0].market_name);
      }, 300); // 防抖 300ms
    },
    /* ── 详情 ── */
    async openDetail(marketName) {
      this.detail = null;
      this.detailError = "";
      this.kline = null;
      this.klineError = "";
      this.compare = null;
      this.compareError = "";
      this.score = null;
      this.currentName = marketName;
      this.computeWears(marketName);
      // 报价与K线数据源不同：并行请求、独立成败，互不连坐。
      const [itemRes, klineRes] = await Promise.all([
        get(`/api/desktop/market/item?name=${encodeURIComponent(marketName)}`),
        this.fetchKline(marketName),
      ]);
      if (itemRes.status === 200 && itemRes.body.ok) this.detail = itemRes.body.data;
      else this.detailError = (itemRes.body && itemRes.body.message) || "实时报价暂不可用";
      this.applyKline(klineRes);
    },
    async fetchKline(name) {
      const styleParam = this.specialStyle ? `&style=${encodeURIComponent(this.specialStyle)}` : "";
      return get(`/api/desktop/market/kline?name=${encodeURIComponent(name)}${styleParam}`);
    },
    applyKline(res) {
      if (res.status === 200 && res.body.ok) {
        this.kline = res.body.data;
        this.klineSource = res.body.data.source || "upstream";
        this.klineError = "";
        this.$nextTick(() => this.renderKlineRetry());
      } else {
        this.kline = null;
        this.klineError = (res.body && res.body.message) || "K线暂不可用";
      }
      this.klineLoading = false;
    },
    async loadKline(nameOverride) {
      const name = nameOverride || this.currentName || (this.detail && this.detail.name);
      if (!name) return;
      this.klineLoading = true;
      this.klineError = "";
      this.kline = null;
      this.applyKline(await this.fetchKline(name));
    },
    computeWears(name) {
      this.wearVariants = [];
      const match = name.match(/^(.*\|.*)\s*\((Factory New|Minimal Wear|Field-Tested|Well-Worn|Battle-Scarred)\)$/);
      if (!match) return;
      const base = match[1].trim();
      this.wearVariants = ["Factory New", "Minimal Wear", "Field-Tested", "Well-Worn", "Battle-Scarred"].map(
        (wear) => ({ wear, name: `${base} (${wear})`, active: wear === match[2] })
      );
    },
    async copyMarketName(name) {
      try {
        await navigator.clipboard.writeText(name);
        toast("市场名已复制", "success");
      } catch {
        toast("复制失败（剪贴板不可用）", "error");
      }
    },
    addAlertFor(itemName) {
      sessionStorage.setItem("alert-wizard-name", itemName);
      location.hash = "#/alerts";
    },
    /* ── K线渲染（主题令牌化） ── */
    renderKline() {
      const el = this.$refs.klineChart;
      if (!el || !this.kline || !window.echarts) return false;
      if (el.offsetWidth === 0) return false; // 隐藏/零宽容器：交给上层重试
      const theme = buildChartTheme();
      if (!this.chart || this.chart.isDisposed()) this.chart = window.echarts.init(el);
      else this.chart.resize();
      // 点集格式 [ts, open, close, high, low]
      const dates = this.kline.points.map((p) =>
        new Date(p[0] * 1000).toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" })
      );
      const ohlc = this.kline.points.map((p) => [p[1], p[2], p[4], p[3]]);
      const ma = (n) =>
        ohlc.map((_, i) =>
          i < n - 1 ? "-" : +(ohlc.slice(i - n + 1, i + 1).reduce((s, v) => s + v[1], 0) / n).toFixed(2)
        );
      this.chart.setOption({
        animation: false,
        ...chartTextStyles(theme),
        tooltip: { trigger: "axis", axisPointer: { type: "cross" }, ...chartTextStyles(theme).tooltip },
        legend: { data: ["日K", "MA5", "MA10"], top: 0, textStyle: { fontSize: 10, color: theme.textDim } },
        grid: [
          { left: 56, right: 16, top: 26, height: "62%" },
          { left: 56, right: 16, top: "76%", height: "18%" },
        ],
        xAxis: [
          { type: "category", data: dates, gridIndex: 0, boundaryGap: true, axisLabel: { show: false } },
          { type: "category", data: dates, gridIndex: 1, axisLabel: { fontSize: 9, color: theme.axis } },
        ],
        yAxis: [
          {
            scale: true,
            gridIndex: 0,
            axisLabel: { color: theme.axis, fontSize: 10 },
            splitLine: { lineStyle: { color: theme.grid, opacity: 0.6 } },
          },
          {
            scale: true,
            gridIndex: 1,
            axisLabel: { show: false },
            splitLine: { show: false },
          },
        ],
        dataZoom: [
          { type: "inside", xAxisIndex: [0, 1], start: 40, end: 100 },
          { type: "slider", xAxisIndex: [0, 1], bottom: 2, height: 16 },
        ],
        series: [
          {
            name: "日K",
            type: "candlestick",
            data: ohlc,
            itemStyle: {
              color: theme.up,
              color0: theme.down,
              borderColor: theme.up,
              borderColor0: theme.down,
            },
          },
          { name: "MA5", type: "line", data: ma(5), symbolSize: 0, lineStyle: { width: 1, color: theme.accentStrong } },
          { name: "MA10", type: "line", data: ma(10), symbolSize: 0, lineStyle: { width: 1, color: theme.warn } },
        ],
      });
      return true;
    },
    async renderKlineRetry(attempt = 8) {
      // 容器可能尚未挂载/处于隐藏页签（offsetWidth=0），逐帧重试而不是静默放弃。
      for (let i = 0; i < attempt; i++) {
        if (this.renderKline()) return true;
        await new Promise((resolve) => requestAnimationFrame(resolve));
      }
      return false;
    },
    /* ── 大盘 ── */
    async loadCompare() {
      if (!this.currentName || this.compareLoading) return;
      this.compareLoading = true;
      this.compareError = "";
      const res = await get(
        `/api/desktop/market/compare?item=${encodeURIComponent(this.currentName)}`
      );
      this.compareLoading = false;
      if (res.status === 200 && res.body.ok) this.compare = res.body.data;
      else this.compareError = (res.body && res.body.message) || "比价数据不可用";
    },
    async loadScore() {
      if (!this.currentName || this.scoreLoading) return;
      this.scoreLoading = true;
      const res = await get(
        `/api/desktop/market/score?item=${encodeURIComponent(this.currentName)}`
      );
      this.scoreLoading = false;
      if (res.status === 200 && res.body.ok) this.score = res.body.data;
      else this.score = { scored: false, reason: "评分暂不可用" };
    },
    async loadHoard() {
      if (!this.currentName || this.hoardLoading) return;
      this.hoardLoading = true;
      const res = await get(
        `/api/desktop/market/hoard?item=${encodeURIComponent(this.currentName)}`
      );
      this.hoardLoading = false;
      if (res.status === 200 && res.body.ok) this.hoard = res.body.data;
      else this.hoard = { available: false, reason: "挂刀参考暂不可用" };
    },
    async loadCaseEv() {
      if (!this.currentName || this.caseEvLoading) return;
      this.caseEvLoading = true;
      const res = await get(
        `/api/desktop/market/case-ev?case=${encodeURIComponent(this.currentName)}`
      );
      this.caseEvLoading = false;
      if (res.status === 200 && res.body.ok) this.caseEv = res.body.data;
      else this.caseEv = null;
    },
    async loadOverview() {
      this.dipText = "";
      this.predText = "";
      const res = await get("/api/desktop/market/overview");
      this.overview = res.status === 200 && res.body.ok ? res.body.data : null;
      for (const kind of ["dip", "prediction"]) {
        const r = await get(`/api/desktop/market/${kind}`);
        this[kind === "dip" ? "dipText" : "predText"] =
          r.status === 200 && r.body.ok ? r.body.data.report : "暂不可用";
      }
      await this.loadRankings();
    },
    async onOverviewChartReady(chart) {
      this.overviewChart = chart;
      const res = await get("/api/desktop/market/index-closes?days=90");
      if (!(res.status === 200 && res.body.ok)) return;
      if (!this.overviewChart || this.overviewChart.isDisposed()) return;
      const points = res.body.data.points;
      const theme = buildChartTheme();
      this.overviewChart.setOption({
        animation: false,
        ...chartTextStyles(theme),
        tooltip: { trigger: "axis" },
        grid: { left: 52, right: 14, top: 14, bottom: 24 },
        xAxis: { type: "category", data: points.map((p) => p.date), axisLabel: { fontSize: 10, color: theme.axis } },
        yAxis: {
          type: "value",
          scale: true,
          axisLabel: { color: theme.axis, fontSize: 10 },
          splitLine: { lineStyle: { color: theme.grid } },
        },
        dataZoom: [{ type: "inside" }],
        series: [
          {
            type: "line",
            data: points.map((p) => p.close),
            symbolSize: 0,
            areaStyle: { opacity: 0.14, color: theme.accent },
            lineStyle: { color: theme.accentStrong },
          },
        ],
      });
    },
    async loadRankings() {
      const params = new URLSearchParams({
        board: this.rankingsBoard,
        category: this.rankingsCategory,
        limit: String(this.rankingsLimit),
      });
      const res = await get(`/api/desktop/market/rankings?${params}`);
      this.rankingsText = res.status === 200 && res.body.ok ? res.body.data.report : "榜单暂不可用";
    },
    /* ── 自选 ── */
    async loadWatchlist(refreshPrices) {
      this.watchLoading = true;
      const res = await get(`/api/desktop/market/watchlist-quotes?refresh=${refreshPrices ? 1 : 0}`);
      this.watchlist = res.status === 200 && res.body.ok ? res.body.data : null;
      this.watchLoading = false;
    },
    groupOf(name) {
      const map = store.settings.watchlist_groups || {};
      return Object.keys(map).find((g) => map[g].includes(name)) || "";
    },
    moveToGroup(name, group) {
      const map = store.settings.watchlist_groups || {};
      Object.values(map).forEach((arr) => {
        const i = arr.indexOf(name);
        if (i >= 0) arr.splice(i, 1);
      });
      if (group) {
        if (!map[group]) map[group] = [];
        map[group].push(name);
      }
      store.settings.watchlist_groups = map; // 客户端分组持久化（服务端仍平铺）
    },
    async removeWatchItem(item, index) {
      const okRemove = await confirmBox("删除自选", `确定从自选中移除「${item.display_name}」？`, { danger: true });
      if (!okRemove) return;
      const owner = await this.resolveOwner();
      this.removingIndex = index;
      const res = await apiDelete(`/api/admin/watchlist/${owner}/${index + 1}`);
      this.removingIndex = -1;
      toast(res && res.deleted ? "已删除" : "删除失败", res && res.deleted ? "success" : "error");
      this.loadWatchlist(false);
    },
    async resolveOwner() {
      const profile = await get("/api/desktop/persona/profile");
      if (profile.status === 200 && profile.body.ok) return profile.body.data.owner_user_id;
      throw new Error("owner_unavailable");
    },
    switchTab(tab) {
      this.tab = tab;
      if (tab === "overview" && !this.overview) this.loadOverview();
      if (tab === "watchlist") this.loadWatchlist(false);
      if (tab === "rankings" && !this.rankingsText) this.loadRankings();
      if (tab === "search" && this.kline) this.$nextTick(() => this.renderKlineRetry());
    },
  },
  template: `
  <div>
    <page-header title="市场"
                 subtitle="实时报价 · 原生 K 线 · 大盘与榜单">
      <template #meta>
        <span v-if="searching" class="muted">搜索中…</span>
      </template>
      <input type="text" v-model="query" placeholder="搜索饰品（中英文别名，如：红线）"
             style="width:300px" spellcheck="false" aria-label="搜索饰品"
             @keydown.enter="search" @keydown.down.prevent="onSearchKeydown"
             @keydown.up.prevent="onSearchKeydown" @input="search" />
      <button class="btn" :disabled="searching" @click="search">搜索</button>
    </page-header>

    <app-tabs :tabs="[
      { key: 'search', label: '搜索 / 详情' },
      { key: 'overview', label: '大盘' },
      { key: 'rankings', label: '榜单' },
      { key: 'watchlist', label: '自选' },
    ]" v-model:active="tab" @change="switchTab" />

    <!-- ══ 搜索 / 详情 ══ -->
    <template v-if="tab==='search'">
      <div v-if="results.length" class="card mb-3 search-result-card">
        <div v-for="(r, idx) in results" :key="r.market_name"
             class="palette-item" :class="{ hl: idx === resultHl }"
             role="button" tabindex="0"
             @mouseenter="resultHl = idx" @click="openDetail(r.market_name)"
             @keydown.enter.prevent="openDetail(r.market_name)">
          📦 {{ r.display_name }} <span class="muted spacer"></span><span class="faint">{{ r.market_name }}</span>
        </div>
      </div>

      <div class="card" v-if="detail || detailError">
        <div class="row wrap mb-2">
          <h3 class="card-title-inline">{{ detail ? detail.name : currentName }}</h3>
          <select v-model="specialStyle" @change="loadKline()" aria-label="特殊款式" style="width:auto">
            <option value="">普通款</option>
            <option v-for="s in styles.slice(1)" :key="s" :value="s">{{ s }}</option>
          </select>
          <div class="spacer"></div>
          <button class="btn sm ghost" @click="copyMarketName(currentName)">复制市场名</button>
          <button class="btn sm ghost" @click="addAlertFor(currentName)">设预警</button>
          <button class="btn sm" @click="loadCompare">{{ compareLoading ? '比价中…' : '跨平台比价' }}</button>
          <button class="btn sm" @click="loadScore">{{ scoreLoading ? '评分中…' : '买卖参考' }}</button>
          <button class="btn sm ghost" @click="loadHoard">{{ hoardLoading ? '挂刀中…' : '挂刀参考' }}</button>
          <button class="btn sm ghost" @click="loadCaseEv">{{ caseEvLoading ? 'EV中…' : '开箱EV' }}</button>
        </div>

        <div v-if="wearVariants.length" class="row wrap mb-2">
          <span class="faint">磨损：</span>
          <button v-for="w in wearVariants" :key="w.name" class="btn sm"
                  :class="w.active ? '' : 'ghost'"
                  @click="openDetail(w.name)">{{ w.wear }}</button>
        </div>

        <state-panel v-if="detailError && !detail" state="error" icon="⚠️" :message="detailError"
                     @retry="openDetail(currentName)" tight />
        <template v-else-if="detail">
          <data-table :columns="quoteColumns" :rows="detail.quotes" row-key="platform" />
          <kv-list v-if="detailSummaryPairs.length" class="mt-2" :pairs="detailSummaryPairs" />
        </template>

        <template v-if="compareError">
          <div class="notice-strip mt-2">CSQAQ 侧：{{ compareError }}</div>
        </template>
        <template v-else-if="compare">
          <h3 class="mt-3">🔀 跨平台比价（CSQAQ 快照）</h3>
          <data-table :columns="csqaqColumns" :rows="compare.csqaq ? compare.csqaq.quotes : []" row-key="platform" />
          <kv-list class="mt-2" :pairs="comparePairs" />
          <div class="faint mt-1">两源采集时间可能不同步，价差仅供参考。</div>
        </template>

        <template v-if="score">
          <h3 class="mt-3">⚖️ 买卖参考（确定性规则评分）</h3>
          <div v-if="!score.scored" class="faint">{{ score.reason }}</div>
          <template v-else>
            <data-table :columns="scoreColumns" :rows="scoreFactors" row-key="key" />
            <div class="faint mt-1">买入 {{ score.buy.total }} / 卖出 {{ score.sell.total }} · {{ score.sample_days }} 个日线 · {{ score.disclaimer }}</div>
          </template>
        </template>

        <template v-if="hoard">
          <h3 class="mt-3">🧾 挂刀参考</h3>
          <div v-if="hoard.available === false" class="faint">{{ hoard.reason || '暂不可用' }}</div>
          <kv-list v-else class="mt-2" :pairs="hoardPairs" />
        </template>

        <template v-if="caseEv">
          <h3 class="mt-3">📦 开箱EV</h3>
          <kv-list class="mt-2" :pairs="caseEvPairs" />
          <div class="faint mt-1">概率为 Valve 公开掉率，历史数据不构成收益承诺。</div>
        </template>

        <hr class="divider mt-3" />
        <state-panel v-if="klineLoading" state="loading" :rows="3" tight />
        <state-panel v-else-if="klineError" state="error" icon="📈" title="K线暂不可用" :message="klineError"
                     @retry="loadKline()" tight />
        <template v-else-if="kline">
          <div v-if="klineSource === 'local_snapshot'" class="notice-strip mb-2">
            📊 {{ kline.note || '本地快照聚合K线（口径：每小时在售价）' }}
          </div>
          <div ref="klineChart" class="chart"></div>
          <div class="faint mt-1">
            数据源 {{ klineSource === 'local_snapshot' ? '本地每小时快照聚合' : 'SteamDT' }} · 已通过时效/OHLC 校验，坏数据拒绝展示
          </div>
        </template>
      </div>
      <state-panel v-else-if="tab==='search'" state="empty" icon="🔎" title="输入名称开始搜索"
                   message="支持中英文别名模糊匹配；点击结果查看报价详情与原生交互式 K 线。" />
    </template>

    <!-- ══ 大盘 ══ -->
    <template v-else-if="tab==='overview'">
      <div class="mb-3">
        <chart-card title="装备指数走势" subtitle="近 90 日收盘" :height="340"
                    :loading="!overviewChart" :empty="false" empty-text=""
                    @ready="onOverviewChartReady" />
      </div>
      <div class="grid-2">
        <div class="card"><h3>📉 抄底 · 大盘历史情景</h3>
          <pre class="code-block">{{ dipText || '加载中…' }}</pre></div>
        <div class="card"><h3>🔮 短期走势预测</h3>
          <pre class="code-block">{{ predText || '加载中…' }}</pre></div>
      </div>
    </template>

    <!-- ══ 榜单 ══ -->
    <template v-else-if="tab==='rankings'">
      <div class="card">
        <div class="row wrap mb-3">
          <select v-model="rankingsBoard" @change="loadRankings" aria-label="榜单类型">
            <option>异动榜</option><option>涨跌榜</option><option>流动性榜</option>
          </select>
          <input type="text" v-model="rankingsCategory" placeholder="分类（刀具/手套/AK-47…）" style="width:180px" />
          <input type="number" v-model.number="rankingsLimit" min="5" max="20" aria-label="条数上限" style="width:80px" />
          <button class="btn sm" @click="loadRankings">刷新</button>
        </div>
        <pre class="code-block">{{ rankingsText || '加载中…' }}</pre>
      </div>
    </template>

    <!-- ══ 自选 ══ -->
    <template v-else-if="tab==='watchlist'">
      <div class="card">
        <div class="row wrap mb-3">
          <button class="btn sm ghost" :disabled="watchLoading" @click="loadWatchlist(true)">
            {{ watchLoading ? '刷新中…' : '刷新实时价' }}
          </button>
          <span class="faint">分组存于本机设置；服务端仍为平铺列表</span>
        </div>
        <data-table :columns="watchColumns" :rows="(watchlist && watchlist.items) || []"
                    row-key="display_name" :loading="watchLoading && !watchlist"
                    empty-title="自选为空"
                    empty-text="在 QQ 里对塔菲发送「加自选 饰品名」，或在上方搜索后添加。"
                    empty-icon="⭐" />
      </div>
    </template>
  </div>
  `,
};
