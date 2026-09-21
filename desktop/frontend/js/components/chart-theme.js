/* components/chart-theme.js — 从 CSS 设计令牌生成 echarts 调色板（PANEL_UI_V2_PLAN 3.2）。
   每次渲染前调用 buildChartTheme()，主题/accent 切换后由视图重挂载路径自然换肤。 */

function cssVar(name, fallback) {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || fallback;
}

export function buildChartTheme() {
  return {
    accent: cssVar("--accent", "#f472b6"),
    accentStrong: cssVar("--accent-strong", "#f472b6"),
    text: cssVar("--text", "#3c3c4e"),
    textDim: cssVar("--text-dim", "#6a6a80"),
    textFaint: cssVar("--text-faint", "#a0a0b8"),
    border: cssVar("--border", "#dcd8e8"),
    grid: cssVar("--chart-grid", "#eae6f2"),
    axis: cssVar("--chart-axis", "#a0a0b8"),
    tooltipBg: cssVar("--chart-tooltip-bg", "#ffffff"),
    up: cssVar("--up", "#e04a3a"),
    down: cssVar("--down", "#2ab85a"),
    ok: cssVar("--ok", "#2ab85a"),
    warn: cssVar("--warn", "#d98a20"),
    danger: cssVar("--danger", "#e04a3a"),
    fontFamily: 'Segoe UI, Microsoft YaHei, system-ui, sans-serif',
  };
}

/* 追加到任意 option 的公共底座：文字颜色 / 分隔线 / tooltip 皮肤随令牌走。 */
export function chartTextStyles(theme) {
  return {
    textStyle: { color: theme.textDim, fontFamily: theme.fontFamily },
    legend: { textStyle: { color: theme.textDim, fontSize: 10, fontFamily: theme.fontFamily } },
    tooltip: {
      backgroundColor: theme.tooltipBg,
      borderColor: theme.border,
      borderWidth: 1,
      textStyle: { color: theme.text, fontSize: 12, fontFamily: theme.fontFamily },
      extraCssText: "box-shadow: 0 4px 16px rgba(0,0,0,.14); border-radius: 8px;",
    },
  };
}

/* 类目轴/数值轴默认皮肤，调用方按需覆盖特殊字段。 */
export function styledAxes(xConf, yConf, theme) {
  const skinAxis = (axis) => ({
    axisLine: { lineStyle: { color: theme.grid } },
    axisTick: { show: false },
    axisLabel: { color: theme.axis, fontSize: 10 },
    splitLine: { lineStyle: { color: theme.grid } },
    ...axis,
  });
  return Array.isArray(xConf)
    ? [xConf.map(skinAxis), yConf.map(skinAxis)]
    : [skinAxis(xConf), skinAxis(yConf)];
}

export const PALETTE_FALLBACK = ["#f472b6", "#67e8f9", "#a78bfa", "#fb923c", "#4ade80"];
