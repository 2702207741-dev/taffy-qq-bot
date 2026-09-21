/* components/status-pill.js — 状态签（4.12）：图标点 + 文字。统一替代散落的 badge 组合。
   tone: ok | warn | danger | info | neutral */

export default {
  name: "StatusPill",
  props: {
    label: { type: String, required: true },
    tone: { type: String, default: "neutral" }, // ok | warn | danger | info | neutral
    dot: { type: Boolean, default: true },
    title: { type: String, default: "" },
  },
  template: `
  <span class="pill" :class="tone" :title="title || null">
    <span v-if="dot" class="pill-dot" aria-hidden="true"></span>{{ label }}
  </span>
  `,
};
