/* components/data-table.js — 通用数据表格（4.4）。
   columns: [{ key, label, minWidth?, width?, align?, sortable?, ellipsis?,
               render?(row) => string|VNode|VNode[], sortValue?(row) }]
   字符串一律按纯文本（text node）渲染 —— 用户数据永不走 innerHTML；
   VNode 由调用方用 h() 构造（StatusPill / 按钮等）。排序在组件内完成。
   通过 render() 函数实现（无模板编译歧义），沿用全局 Vue 运行时。 */

import StatePanel from "./state-panel.js";

export default {
  name: "DataTable",
  props: {
    columns: { type: Array, required: true },
    rows: { type: Array, default: () => [] },
    rowKey: { type: [String, Function], default: "" },
    loading: Boolean,
    emptyTitle: { type: String, default: "暂无数据" },
    emptyText: { type: String, default: "" },
    emptyIcon: { type: String, default: "" },
    clickableRows: Boolean,
    initialSort: { type: Object, default: null }, // { key, dir }
  },
  emits: ["row-click"],
  data() {
    return {
      sortKey: (this.initialSort && this.initialSort.key) || "",
      sortDir: (this.initialSort && this.initialSort.dir) || "asc",
    };
  },
  computed: {
    sortedRows() {
      if (!this.sortKey) return this.rows;
      const col = this.columns.find((c) => c.key === this.sortKey);
      const getVal = col && col.sortValue ? col.sortValue : (row) => row[this.sortKey];
      const dir = this.sortDir === "desc" ? -1 : 1;
      return [...this.rows].sort((a, b) => {
        const va = getVal(a);
        const vb = getVal(b);
        if (va == null && vb == null) return 0;
        if (va == null) return 1;
        if (vb == null) return -1;
        if (typeof va === "number" && typeof vb === "number") return (va - vb) * dir;
        return String(va).localeCompare(String(vb), "zh-CN") * dir;
      });
    },
  },
  methods: {
    keyOf(row, index) {
      if (typeof this.rowKey === "function") return this.rowKey(row);
      if (this.rowKey) return row[this.rowKey];
      return index;
    },
    toggleSort(col) {
      if (!col.sortable) return;
      if (this.sortKey === col.key) this.sortDir = this.sortDir === "asc" ? "desc" : "asc";
      else {
        this.sortKey = col.key;
        this.sortDir = "asc";
      }
    },
    arrowFor(col) {
      if (!col.sortable || this.sortKey !== col.key) return "";
      return this.sortDir === "asc" ? "▲" : "▼";
    },
    renderCell(row, col) {
      const out = col.render ? col.render(row) : row[col.key];
      const children = Array.isArray(out) ? out : [out];
      return children.map((child) =>
        typeof child === "string" || typeof child === "number"
          ? Vue.h("span", { class: col.ellipsis ? "cell-ellipsis" : "" }, String(child))
          : child
      );
    },
  },
  render() {
    const { h } = Vue;

    if (this.loading && !this.rows.length) {
      return h(StatePanel, { state: "loading", rows: 4 });
    }
    if (!this.sortedRows.length) {
      return h(StatePanel, { state: "empty", title: this.emptyTitle, message: this.emptyText, icon: this.emptyIcon });
    }

    const headerCells = this.columns.map((col) =>
      h(
        "th",
        {
          key: col.key,
          class: [col.align === "right" ? "th-num" : "", col.sortable ? "sortable" : ""],
          style: [
            col.width ? `width:${col.width}` : "",
            col.minWidth ? `min-width:${col.minWidth}` : "",
          ].join(";"),
          scope: "col",
          ariaSort: col.sortable ? (this.sortKey === col.key ? (this.sortDir === "asc" ? "ascending" : "descending") : "none") : undefined,
          onClick: () => this.toggleSort(col),
          onKeydown: col.sortable
            ? (e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  this.toggleSort(col);
                }
              }
            : undefined,
          tabIndex: col.sortable ? 0 : undefined,
        },
        [col.label, this.arrowFor(col) ? h("span", { class: "sort-arrow", "aria-hidden": "true" }, this.arrowFor(col)) : null]
      )
    );

    const bodyRows = this.sortedRows.map((row, rowIndex) =>
      h(
        "tr",
        {
          key: this.keyOf(row, rowIndex),
          class: this.clickableRows ? "row-click" : "",
          onClick: () => this.$emit("row-click", row),
        },
        this.columns.map((col) =>
          h(
            "td",
            { key: col.key, class: col.align === "right" ? "td-num" : "" },
            this.renderCell(row, col)
          )
        )
      )
    );

    return h("div", { class: "dt-wrap" }, [
      h("table", { class: "dt-table" }, [
        h("thead", [h("tr", headerCells)]),
        h("tbody", bodyRows),
      ]),
    ]);
  },
};
