/* components/confirm.js — 二次确认弹窗（4.8 升级）：
   danger=true 主按钮红；requireType="停止" 需输入匹配文本才能确认（高风险动作）；
   焦点管理：打开聚焦取消按钮，Esc 关闭，关闭后焦点由全局 Esc 栈还原。 */

import { pushEsc } from "../ui.js";

export default {
  name: "ConfirmModal",
  props: {
    title: String,
    body: String,
    danger: Boolean,
    requireType: { type: String, default: "" },
    okText: { type: String, default: "确认执行" },
  },
  emits: ["ok", "cancel"],
  data() {
    return { typed: "" };
  },
  computed: {
    canConfirm() {
      return !this.requireType || this.typed.trim() === this.requireType;
    },
  },
  mounted() {
    this.$refs.cancel && this.$refs.cancel.focus();
    this._offEsc = pushEsc(() => this.$emit("cancel"));
  },
  unmounted() {
    this._offEsc && this._offEsc();
  },
  template: `
  <div class="modal" @click.self="$emit('cancel')">
    <div class="modal-box confirm-input" role="dialog" aria-modal="true" :aria-label="title">
      <h4>{{ title }}</h4>
      <p class="muted mb-3" style="white-space:pre-wrap">{{ body }}</p>
      <div v-if="requireType" class="form-row">
        <label>本操作风险较高：请输入「<b>{{ requireType }}</b>」以确认</label>
        <input v-model="typed" type="text" spellcheck="false"
               :placeholder="'输入 ' + requireType" autocomplete="off" />
      </div>
      <div class="row" style="justify-content:flex-end; gap:8px">
        <button ref="cancel" class="btn ghost" @click="$emit('cancel')">取消</button>
        <button class="btn" :class="{ danger }" :disabled="!canConfirm" @click="canConfirm && $emit('ok')">
          {{ okText }}
        </button>
      </div>
    </div>
  </div>
  `,
};
