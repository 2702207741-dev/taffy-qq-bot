/* components/page-header.js — 全站统一页头（4.1）：标题 + 副标题 + 状态区 + 操作区。 */

export default {
  name: "PageHeader",
  props: {
    title: { type: String, required: true },
    subtitle: { type: String, default: "" },
  },
  template: `
  <header class="page-header">
    <div class="page-header-main">
      <h2 class="page-title">{{ title }}</h2>
      <slot name="meta"></slot>
      <span v-if="subtitle" class="page-subtitle">{{ subtitle }}</span>
    </div>
    <div class="page-header-actions"><slot></slot></div>
  </header>
  `,
};
