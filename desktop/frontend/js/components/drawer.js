/* components/drawer.js — 右侧详情抽屉（4.10）：open/title/footer slot，
   遮罩点击/Esc 关闭，关闭动效 --dur-norm；焦点管理（打开聚焦关闭钮，还原触发焦点）。 */

import { pushEsc } from "../ui.js";

export default {
  name: "DrawerPanel",
  props: {
    open: Boolean,
    title: { type: String, default: "" },
    width: { type: Number, default: 480 },
  },
  emits: ["close"],
  data() {
    return { closing: false };
  },
  watch: {
    open(value) {
      if (value) {
        this.closing = false;
        this._lastFocus = document.activeElement;
        this._offEsc = pushEsc(() => this.requestClose());
        document.body.style.overflow = "hidden";
        this.$nextTick(() => this.$refs.closeBtn && this.$refs.closeBtn.focus());
      } else {
        this.teardown();
      }
    },
  },
  unmounted() {
    this.teardown();
  },
  methods: {
    requestClose() {
      if (this.closing) return;
      this.closing = true;
      setTimeout(() => {
        this.closing = false;
        this.$emit("close");
      }, 200); // 对齐 --dur-norm 的退出动效
    },
    teardown() {
      if (this._offEsc) {
        this._offEsc();
        this._offEsc = null;
      }
      document.body.style.overflow = "";
      if (this._lastFocus && this._lastFocus.focus) {
        try {
          this._lastFocus.focus();
        } catch {
          /* 触发元素可能已随视图卸载 */
        }
      }
    },
  },
  template: `
  <teleport to="body">
    <div v-if="open" class="drawer-overlay" @click.self="requestClose">
      <div class="drawer" :class="{ closing }" role="dialog" aria-modal="true" :aria-label="title"
           :style="{ width: width + 'px' }" @keydown.esc.prevent="requestClose">
        <div class="drawer-head">
          <h3 class="drawer-title">{{ title }}</h3>
          <div class="spacer"></div>
          <button ref="closeBtn" class="btn icon ghost" aria-label="关闭抽屉" @click="requestClose">✕</button>
        </div>
        <div class="drawer-body"><slot></slot></div>
        <div v-if="$slots.footer" class="drawer-foot"><slot name="footer"></slot></div>
      </div>
    </div>
  </teleport>
  `,
};
