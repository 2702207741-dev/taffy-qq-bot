/* views/analytics.js — 数据分析（6.8）：功能调用量堆叠图 + 预警趋势 ChartCard /
   Top 用户统一 CSS 条形；图表走 chart-theme 令牌色板；CSV 导出保留。 */

import { get, post, del } from "../api.js";
import { downloadCsv, toast, confirmBox } from "../ui.js";
import { buildChartTheme, chartTextStyles } from "../components/chart-theme.js";

export default {
  data() {
    return {
      days: 30,
      usage: null,
      alerts: null,
      loading: false,
      usageChart: null,
      alertChart: null,
      stickers: null,
      stickerTags: "",
    };
  },
  computed: {
    topUsers() {
      if (!this.usage || !this.usage.top_users.length) return [];
      const max = Math.max(...this.usage.top_users.map((u) => u.calls));
      return this.usage.top_users.map((u) => ({ ...u, widthPct: `${(u.calls / max) * 100}%` }));
    },
    stickerTagText() {
      const list = (this.stickers && this.stickers.stickers) || [];
      return list
        .map((s) => `${s.file}（${s.tags.join("、")}）`)
        .join("；");
    },
    statusPairs() {
      if (!this.alerts || !this.alerts.by_status.length) return [];
      return this.alerts.by_status.map((s) => ({ k: s.status, v: s.count }));
    },
  },
  mounted() {
    this.load();
    this.loadStickers();
    this.onRefresh = () => this.load();
    window.addEventListener("view-refresh", this.onRefresh);
  },
  unmounted() {
    window.removeEventListener("view-refresh", this.onRefresh);
    /* 实例均已 trackChart 登记，随 disposeCharts 释放；这里兜底 */
    if (this.usageChart && !this.usageChart.isDisposed()) this.usageChart.dispose();
    if (this.alertChart && !this.alertChart.isDisposed()) this.alertChart.dispose();
  },
  methods: {
    setDays(days) {
      this.days = days;
      this.load();
    },
    async loadStickers() {
      const res = await get("/api/desktop/stickers");
      if (res.status === 200 && res.body.ok) this.stickers = res.body.data;
    },
    async uploadSticker(event) {
      const file = event.target.files && event.target.files[0];
      if (!file) return;
      const tagsRaw = this.stickerTags.trim();
      if (!tagsRaw) return toast("先填标签（用 / 分隔，如：开心/大笑）", "warn");
      if (file.size > 8 * 1024 * 1024) return toast("图片超过 8MB", "error");
      const buffer = await file.arrayBuffer();
      const bytes = new Uint8Array(buffer);
      let binary = "";
      for (let i = 0; i < bytes.length; i += 0x8000)
        binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
      const res = await post("/api/desktop/stickers", {
        file_name: file.name,
        tags: tagsRaw.split("/").map((t) => t.trim()).filter(Boolean),
        data_base64: btoa(binary),
      });
      if (res.status === 200 && res.body.ok) {
        toast("表情包已入库", "ok");
        this.stickerTags = "";
        event.target.value = "";
        await this.loadStickers();
      } else toast((res.body && res.body.message) || "上传失败", "error");
    },
    async deleteSticker(fileName) {
      if (!(await confirmBox("删除表情包", "同时会从清单移除，确认？", "删除"))) return;
      const res = await del(`/api/desktop/stickers?file=${encodeURIComponent(fileName)}`);
      if (res.status === 200 && res.body.ok) { toast("已删除", "ok"); await this.loadStickers(); }
      else toast((res.body && res.body.message) || "删除失败", "error");
    },
    async load() {
      this.loading = true;
      const [usageRes, alertsRes] = await Promise.all([
        get(`/api/desktop/analytics/usage?days=${this.days}`),
        get(`/api/desktop/analytics/alerts?days=${this.days}`),
      ]);
      this.usage = usageRes.status === 200 && usageRes.body.ok ? usageRes.body.data : null;
      this.alerts = alertsRes.status === 200 && alertsRes.body.ok ? alertsRes.body.data : null;
      this.loading = false;
      // 数据到达 → watch 触发 ChartCard 建实例 → @ready 回调里渲染
      this.$nextTick(() => {
        this.renderUsage();
        this.renderAlerts();
      });
    },
    onUsageReady(chart) {
      this.usageChart = chart;
      this.renderUsage();
    },
    onAlertReady(chart) {
      this.alertChart = chart;
      this.renderAlerts();
    },
    renderUsage() {
      const chart = this.usageChart;
      if (!chart || chart.isDisposed() || !this.usage || !window.echarts) return;
      const theme = buildChartTheme();
      const daysList = [...new Set(this.usage.daily.map((d) => d.day))].sort();
      const features = [...new Set(this.usage.daily.map((d) => d.feature))];
      chart.setOption({
        animation: false,
        color: features.map((_, i) =>
          [theme.accentStrong, theme.accent, theme.warn, theme.ok, theme.textFaint, theme.down, theme.up][i % 7]
        ),
        ...chartTextStyles(theme),
        tooltip: { trigger: "axis" },
        legend: { top: 0, type: "scroll", textStyle: { fontSize: 10, color: theme.textDim } },
        grid: { left: 44, right: 14, top: 34, bottom: 26 },
        xAxis: { type: "category", data: daysList, axisLabel: { color: theme.axis, fontSize: 10 } },
        yAxis: {
          type: "value",
          axisLabel: { color: theme.axis, fontSize: 10 },
          splitLine: { lineStyle: { color: theme.grid } },
        },
        series: features.map((feature) => ({
          name: feature,
          type: "bar",
          stack: "total",
          barMaxWidth: 26,
          data: daysList.map((day) => {
            const hit = this.usage.daily.find((d) => d.day === day && d.feature === feature);
            return hit ? hit.calls : 0;
          }),
        })),
      });
    },
    renderAlerts() {
      const chart = this.alertChart;
      if (!chart || chart.isDisposed() || !this.alerts || !window.echarts) return;
      const theme = buildChartTheme();
      const delivered = this.alerts.delivered_daily;
      const failed = this.alerts.failed_daily;
      const daysList = [
        ...new Set([...delivered.map((d) => d.day), ...failed.map((d) => d.day)]),
      ].sort();
      chart.setOption({
        animation: false,
        ...chartTextStyles(theme),
        tooltip: { trigger: "axis" },
        legend: { top: 0, textStyle: { fontSize: 10, color: theme.textDim } },
        grid: { left: 40, right: 14, top: 34, bottom: 26 },
        xAxis: { type: "category", data: daysList, axisLabel: { color: theme.axis, fontSize: 10 } },
        yAxis: {
          type: "value",
          minInterval: 1,
          axisLabel: { color: theme.axis, fontSize: 10 },
          splitLine: { lineStyle: { color: theme.grid } },
        },
        series: [
          {
            name: "触发",
            type: "line",
            smooth: true,
            data: daysList.map((d) => {
              const hit = delivered.find((x) => x.day === d);
              return hit ? hit.count : 0;
            }),
            areaStyle: { opacity: 0.15, color: theme.accent },
            lineStyle: { color: theme.accentStrong },
            symbolSize: 0,
          },
          {
            name: "失败",
            type: "line",
            smooth: true,
            data: daysList.map((d) => {
              const hit = failed.find((x) => x.day === d);
              return hit ? hit.count : 0;
            }),
            lineStyle: { color: theme.danger },
            itemStyle: { color: theme.danger },
            symbolSize: 0,
          },
        ],
      });
    },
    exportUsageCsv() {
      if (!this.usage) return;
      const rows = [["day", "feature", "calls"]];
      this.usage.daily.forEach((d) => rows.push([d.day, d.feature, d.calls]));
      downloadCsv(`taffy-usage-${this.days}d.csv`, rows);
    },
    exportAlertsCsv() {
      if (!this.alerts) return;
      const rows = [["kind", "day", "count"]];
      this.alerts.delivered_daily.forEach((d) => rows.push(["delivered", d.day, d.count]));
      this.alerts.failed_daily.forEach((d) => rows.push(["failed", d.day, d.count]));
      this.alerts.by_status.forEach((s) => rows.push(["status_" + s.status, "-", s.count]));
      downloadCsv(`taffy-alerts-${this.days}d.csv`, rows);
    },
    async exportAuditCsv() {
      /* 审计全量导出由 admin CSV 端点生成（含公式注入防护）；
         bridge 代理面向 JSON，浏览器直连会被 CORS 拦截，故经外部浏览器下载。 */
      const result = await window.pywebview.api.open_external(
        "https://127.0.0.1.invalid/usage-export-hint"
      );
      void result;
      toast("请使用运维页 → 使用审计 → /api/admin/usage/export 获取全量 CSV");
    },
  },
  template: `
  <div>
    <page-header title="数据分析"
                 subtitle="功能调用量 · Top 用户 · 预警趋势">
      <select :value="days" aria-label="统计范围" @change="setDays(Number($event.target.value))">
        <option :value="7">近7天</option><option :value="30">近30天</option><option :value="90">近90天</option>
      </select>
      <button class="btn sm ghost" @click="exportUsageCsv">导出使用 CSV</button>
      <button class="btn sm ghost" @click="exportAlertsCsv">导出预警 CSV</button>
      <button class="btn sm ghost" @click="exportAuditCsv">审计全量导出</button>
    </page-header>

    <div class="mb-3">
      <chart-card title="各功能调用量（按日堆叠）"
                  :subtitle="usage ? '区间总调用 ' + usage.total + ' 次' : ''"
                  :height="320" :loading="loading && !usage" :empty="!loading && !usage"
                  empty-text="暂无使用数据 —— 与塔菲互动后这里会长出柱子。"
                  @ready="onUsageReady" />
    </div>

    <div class="grid-2">
      <div class="card">
        <h3>Top 活跃用户</h3>
        <template v-if="topUsers.length">
          <div v-for="u in topUsers" :key="u.user_id" class="top-user-row">
            <div class="row muted"><span>{{ u.user_id }}</span><span class="grow"></span><span class="num">{{ u.calls }} 次</span></div>
            <div class="bar-track mt-1"><div class="bar-fill" :style="{ width: u.widthPct }"></div></div>
          </div>
        </template>
        <state-panel v-else state="empty" icon="👥" title="区间内无记录"
                     message="所选时间范围内还没有用户使用记录。" tight />
      </div>

      <chart-card title="预警触发 / 失败趋势" :height="240"
                  :loading="loading && !alerts" :empty="!loading && !alerts"
                  empty-text="暂无预警事件。" @ready="onAlertReady">
        <template #head-extra>
          <kv-list v-if="statusPairs.length" class="ml-3" label-width="90px"
                   :pairs="statusPairs.slice(0, 3)" />
        </template>
      </chart-card>

      <div class="card mt-3">
        <h3>🖼️ 表情包库</h3>
        <div v-if="stickers" class="notice-strip mb-2">
          {{ stickers.enabled ? "功能已开启（QQ 侧按概率/冷却/每日上限触发）" : "功能未开启：需设置 TAFFY_STICKERS_ENABLED=true" }}
        </div>
        <div class="row wrap mb-2">
          <input type="text" v-model="stickerTags" class="grow"
                 placeholder="标签（用 / 分隔，如：开心/大笑；也可含场景：闲聊/安慰）" />
          <input type="file" accept=".png,.jpg,.jpeg,.webp,.gif,.bmp" @change="uploadSticker" />
        </div>
        <div v-if="stickers && stickers.stickers.length">
          <div v-for="s in stickers.stickers" :key="s.file" class="row muted mb-1">
            <span>{{ s.file }}</span>
            <span class="faint grow">{{ s.tags.join("、") }}</span>
            <button class="btn sm danger" @click="deleteSticker(s.file)">删</button>
          </div>
          <p class="faint mt-1">{{ stickerTagText }}</p>
        </div>
        <state-panel v-else state="empty" icon="🖼️" title="表情包库为空"
                     message="上传图片并打标签后，塔菲才会在合适的社交场景回应表情包；库为空时功能整体静默。" tight />
      </div>
    </div>
  </div>
  `,
};
