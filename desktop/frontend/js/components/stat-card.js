/* components/stat-card.js — 汇总数字卡（4.3）：label / 大数字 / 涨跌 / 迷你 sparkline / 状态点。
   点击整卡 emit("click")；delta 正负自动用 --up/--down 语义色（红涨绿跌）。 */

export default {
  name: "StatCard",
  props: {
    label: { type: String, required: true },
    value: { type: [String, Number], required: true },
    unit: { type: String, default: "" },
    delta: { type: Number, default: null },          // 百分比数值，null 不显示
    deltaLabel: { type: String, default: "" },
    points: { type: Array, default: null },          // 迷你趋势数据
    status: { type: String, default: "" },           // ok | warn | danger → 顶部状态点
    clickable: Boolean,
  },
  emits: ["click"],
  computed: {
    deltaClass() {
      if (this.delta == null || !Number.isFinite(Number(this.delta))) return "";
      return Number(this.delta) >= 0 ? "up" : "down";
    },
    deltaText() {
      const num = Number(this.delta);
      if (!Number.isFinite(num)) return this.deltaLabel;
      return `${num >= 0 ? "+" : ""}${num.toFixed(2)}%`;
    },
    sparklinePoints() {
      if (!this.points || this.points.length < 2) return "";
      const nums = this.points.map((p) => Number(p)).filter((n) => Number.isFinite(n));
      if (nums.length < 2) return "";
      const min = Math.min(...nums), max = Math.max(...nums);
      const range = max - min || 1;
      const stepX = 100 / (nums.length - 1);
      return nums
        .map((n, i) => `${(i * stepX).toFixed(1)},${(30 - ((n - min) / range) * 26 + 1).toFixed(1)}`)
        .join(" ");
    },
    statusColor() {
      return { ok: "var(--ok)", warn: "var(--warn)", danger: "var(--danger)" }[this.status] || "";
    },
  },
  template: `
  <div class="card stat-card" :class="{ clickable }" @click="clickable && $emit('click')"
       :tabindex="clickable ? 0 : null" :role="clickable ? 'button' : null"
       @keydown.enter="clickable && $emit('click')">
    <span v-if="statusColor" class="stat-status-dot" :style="{ background: statusColor }" aria-hidden="true"></span>
    <div class="stat-label">{{ label }}</div>
    <div class="stat-value-row">
      <span class="stat-value">{{ value }}</span>
      <span v-if="unit" class="stat-unit">{{ unit }}</span>
      <span v-if="delta != null" class="stat-delta" :class="deltaClass">{{ deltaText }}</span>
      <span v-else-if="deltaLabel" class="muted">{{ deltaLabel }}</span>
    </div>
    <slot name="footer"></slot>
    <svg v-if="sparklinePoints" class="stat-trend" width="110" height="34" viewBox="0 0 100 32"
         preserveAspectRatio="none" aria-hidden="true">
      <polyline :points="sparklinePoints" fill="none" stroke="var(--accent)"
                stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
                vector-effect="non-scaling-stroke" />
    </svg>
  </div>
  `,
};
