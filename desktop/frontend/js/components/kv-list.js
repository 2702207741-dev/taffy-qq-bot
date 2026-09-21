/* components/kv-list.js — 键值对排布（4.12）：[{k, v, cls?}]，值一律纯文本渲染（安全）。 */

export default {
  name: "KVList",
  props: {
    pairs: { type: Array, default: () => [] }, // [{ k: '键', v: '值', cls?: 'up' }]
    labelWidth: { type: String, default: "" },
  },
  template: `
  <dl class="kv-list" :style="labelWidth ? { gridTemplateColumns: labelWidth + ' 1fr' } : null">
    <template v-for="(p, i) in pairs" :key="i">
      <dt class="kv-k">{{ p.k }}</dt>
      <dd class="kv-v" :class="p.cls || ''">{{ p.v == null || p.v === '' ? '--' : p.v }}</dd>
    </template>
  </dl>
  `,
};
