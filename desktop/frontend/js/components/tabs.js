/* components/tabs.js — 统一页签（4.5）：下划线滑块（位移动画）、badge 角标、键盘 ←/→。
   用法：<app-tabs :tabs="[{key,label,badge}]" v-model:active="tab" @change="fn" /> */

export default {
  name: "AppTabs",
  props: {
    tabs: { type: Array, required: true }, // [{ key, label, badge? }]
    active: { type: String, required: true },
  },
  emits: ["update:active", "change"],
  data() {
    return { sliderStyle: { transform: "translateX(0)", width: "0px" } };
  },
  mounted() {
    this.measure();
    window.addEventListener("resize", this.measure);
  },
  updated() {
    /* active 切换 / 文案变化后重测滑块位置 */
    this.$nextTick(() => this.measure());
  },
  unmounted() {
    window.removeEventListener("resize", this.measure);
  },
  methods: {
    select(key) {
      if (key === this.active) return;
      this.$emit("update:active", key);
      this.$emit("change", key);
    },
    onKeydown(event) {
      const keys = this.tabs.map((t) => t.key);
      const idx = keys.indexOf(this.active);
      let next = -1;
      if (event.key === "ArrowRight") next = (idx + 1) % keys.length;
      else if (event.key === "ArrowLeft") next = (idx - 1 + keys.length) % keys.length;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = keys.length - 1;
      if (next < 0) return;
      event.preventDefault();
      this.select(keys[next]);
      this.$nextTick(() => {
        const btns = (this.$refs.scroller && this.$refs.scroller.querySelectorAll(".app-tab")) || [];
        btns[next] && btns[next].focus();
      });
    },
    measure() {
      const scroller = this.$refs.scroller;
      if (!scroller) return;
      const idx = this.tabs.findIndex((t) => t.key === this.active);
      const el = scroller.querySelectorAll(".app-tab")[idx];
      const next = el
        ? { transform: `translateX(${el.offsetLeft}px)`, width: `${el.offsetWidth}px` }
        : { transform: "translateX(0)", width: "0px" };
      /* 值未变化时不赋值，避免 updated→measure→update 死循环 */
      if (this.sliderStyle.transform === next.transform && this.sliderStyle.width === next.width) return;
      this.sliderStyle = next;
    },
  },
  template: `
  <div class="app-tabs">
    <div ref="scroller" class="app-tabs-scroll" role="tablist" aria-label="页签" @keydown="onKeydown">
      <button v-for="t in tabs" :key="t.key"
              class="app-tab" role="tab" type="button"
              :aria-selected="String(t.key === active)" :tabindex="t.key === active ? 0 : -1"
              @click="select(t.key)">
        {{ t.label }}
        <span v-if="t.badge !== undefined && t.badge !== null && t.badge !== ''" class="tab-badge">{{ t.badge }}</span>
      </button>
    </div>
    <div class="tab-slider" :style="sliderStyle"></div>
  </div>
  `,
};
