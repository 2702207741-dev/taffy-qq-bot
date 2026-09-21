/* components/chart-card.js — 图表包装卡（4.11）：标题/副标题/高度 + 空态/加载态，
   内部创建并 trackChart 登记 echarts 实例，通过 @ready 把实例交给视图 setOption。
   主题切换后由 main.js 的 viewEpoch 重挂载路径统一重建（palette 随令牌刷新）。 */

import StatePanel from "./state-panel.js";
import { trackChart } from "../ui.js";

export default {
  name: "ChartCard",
  props: {
    title: { type: String, default: "" },
    subtitle: { type: String, default: "" },
    height: { type: Number, default: 280 }, // px
    loading: Boolean,
    empty: Boolean,
    emptyText: { type: String, default: "" },
  },
  emits: ["ready"],
  watch: {
    /* loading→false 或 empty→false 任一到达即尝试建实例（此前画布可能处于隐藏态） */
    loading() {
      this.maybeInit();
    },
    empty(value) {
      if (!value) this.maybeInit();
      else if (this.chart) {
        this.chart.clear();
      }
    },
    height() {
      if (this.chart) this.chart.resize();
    },
  },
  mounted() {
    this._onResize = () => this.chart && this.chart.resize();
    window.addEventListener("resize", this._onResize);
    if (!this.empty && !this.loading) this.init();
  },
  unmounted() {
    window.removeEventListener("resize", this._onResize);
    /* 实例仍登记在 ui.disposeCharts 里随路由切换释放；本地引用同步清空 */
    this.chart = null;
  },
  methods: {
    maybeInit() {
      if (this.loading || this.empty) return;
      this.$nextTick(() => this.init());
    },
    init() {
      const el = this.$refs.canvas;
      if (!el || !window.echarts || this.chart) return;
      if (el.offsetWidth === 0) return; // 处于隐藏容器：等数据/可见后再试
      try {
        this.chart = window.echarts.init(el);
        trackChart(this.chart);
        this.$emit("ready", this.chart);
      } catch {
        this.chart = null;
      }
    },
  },
  template: `
  <div class="card">
    <div v-if="title || $slots.default" class="chart-card-head">
      <span v-if="title" class="chart-title">{{ title }}</span>
      <span v-if="subtitle" class="chart-subtitle">{{ subtitle }}</span>
      <slot name="head-extra"></slot>
      <slot></slot>
    </div>
    <state-panel v-if="loading" state="loading" :rows="3" tight />
    <state-panel v-else-if="empty" state="empty" icon="📉" :message="emptyText" tight />
    <div v-show="!loading && !empty" ref="canvas"
         :style="{ width: '100%', height: height + 'px' }"></div>
  </div>
  `,
  components: { StatePanel },
};
