/* components/state-panel.js — 统一三态组件（4.2）：loading 骨架 / empty 图标+行动 / error 重试。
   无障碍：骨架 aria-hidden；错误区 role="alert"；空态图标装饰性 aria-hidden。 */

const STATE_ICONS = { empty: "🗂️", error: "⚠️", loading: "" };

export default {
  name: "StatePanel",
  props: {
    state: { type: String, default: "empty" }, // loading | empty | error
    icon: { type: String, default: "" },
    title: { type: String, default: "" },
    message: { type: String, default: "" },
    rows: { type: Number, default: 3 }, // 骨架行数
    tight: Boolean,
  },
  emits: ["retry"],
  computed: {
    resolvedIcon() {
      return this.icon || STATE_ICONS[this.state] || "🗂️";
    },
    resolvedTitle() {
      if (this.title) return this.title;
      return this.state === "error" ? "加载失败" : "暂无内容";
    },
  },
  template: `
  <div class="state-panel" :class="[state, { tight }]">

    <div v-if="state === 'loading'" class="sp-lines" aria-hidden="true">
      <div v-for="i in rows" :key="i" class="sp-line"
           :style="{ width: (100 - (i % 3) * 14) + '%' }"></div>
    </div>

    <div v-else-if="state === 'error'" role="alert">
      <div class="sp-icon" aria-hidden="true">{{ resolvedIcon }}</div>
      <div class="state-empty-title">加载失败</div>
      <p class="state-desc" v-if="message">{{ message }}</p>
      <div class="row" style="justify-content:center; gap:8px">
        <button class="btn sm ghost" @click="$emit('retry')">重试</button>
        <slot></slot>
      </div>
    </div>

    <div v-else>
      <div class="sp-icon" aria-hidden="true">{{ resolvedIcon }}</div>
      <div class="state-empty-title">{{ resolvedTitle }}</div>
      <p class="state-desc" v-if="message">{{ message }}</p>
      <slot></slot>
    </div>
  </div>
  `,
};
