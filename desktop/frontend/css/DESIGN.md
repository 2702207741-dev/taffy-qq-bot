# 塔菲桌面工作台 · 设计系统 V3（霓虹猫娘）

> 取代 V2 亚麻暖橙主题。方向：**暗夜霓虹 + 猫娘粉 + 电竞青**，贴合塔菲猫娘人设与 CS2 饰品市场的年轻电竞调性。

## 1. 主题方向

**霓虹猫娘 / Neon Neko**

- 深紫黑背景营造暗夜氛围
- 霓虹粉（#ec4899）为主色，直接关联猫娘人设，暗色下高饱和发光
- 霓虹青（#06b6d4）为辅色，用于图表系列、数据高亮、科技感装饰
- 卡片用微妙的边框 + 内发光区分层级，告别 V2 同色系扁平堆叠
- 保留中国市场语义：红涨绿跌

## 2. 色板

### 暗色主题（默认）

| 角色 | 变量 | Hex | 用途 |
|------|------|-----|------|
| 页面底 | `--bg` | `#0a0a14` | 最底层，深紫黑 |
| 面板底 | `--bg-panel` | `#12121f` | 卡片/输入框底 |
| 悬停底 | `--bg-hover` | `#1a1a30` | 行 hover/按钮 ghost hover |
| 激活底 | `--bg-active` | `#252545` | 选中/按下 |
| 表面1 | `--surface-1` | `#12121f` | 侧栏/卡片 |
| 表面2 | `--surface-2` | `#1a1a2e` | 下拉/气泡 |
| 表面3 | `--surface-3` | `#22223a` | 弹窗/抽屉 |
| 正文 | `--text` | `#e4e4f0` | 主文字 |
| 强调文字 | `--text-strong` | `#ffffff` | 大数字/标题 |
| 次级文字 | `--text-dim` | `#9a9ab8` | 说明/标签 |
| 弱文字 | `--text-faint` | `#6a6a8a` | 占位/禁用 |
| 边框 | `--border` | `#2c2c4a` | 默认边框 |
| 强边框 | `--border-strong` | `#3e3e62` | 表头/分割 |
| 弱边框 | `--border-soft` | `#222238` | 行分割 |
| **主色** | `--accent` | `#ec4899` | 霓虹粉，品牌主色 |
| 主色强 | `--accent-strong` | `#f472b6` | 亮粉，主按钮/强调文字 |
| 主色软 | `--accent-soft` | `rgba(236,72,153,.15)` | 选中背景/标签底 |
| 辅色 | `--accent-2` | `#06b6d4` | 霓虹青，图表第二色/科技装饰 |
| 涨 | `--up` | `#ef4444` | 红涨（中国市场语义） |
| 跌 | `--down` | `#22c55e` | 绿跌 |
| 警告 | `--warn` | `#f59e0b` | 琥珀 |
| 危险 | `--danger` | `#ef4444` | 红 |
| 成功 | `--ok` | `#22c55e` | 绿 |

### 亮色主题

| 角色 | Hex | 说明 |
|------|-----|------|
| `--bg` | `#f0f0f7` | 冷灰底 |
| `--bg-panel` | `#ffffff` | 白卡片 |
| `--text` | `#1a1a2e` | 深紫黑文字 |
| `--text-dim` | `#5a5a7a` | |
| `--accent` | `#db2777` | 深粉（亮色背景可读） |
| `--border` | `#d8d8e8` | |

## 3. 字体

- **中文/界面**：`"Segoe UI Variable Text", "Segoe UI", "Microsoft YaHei", system-ui, sans-serif`（零新依赖）
- **数字/代码**：`Consolas, "Cascadia Mono", "JetBrains Mono", monospace`（系统等宽栈）
- 大数字（`.big-num`/`.stat-value`）强制 `font-variant-numeric: tabular-nums` + 等宽栈

## 4. 形状与质感

### 圆角
- `--radius-sm: 6px` — 按钮/输入框
- `--radius-md: 10px` — 导航项/小卡片
- `--radius-lg: 16px` — 卡片/弹窗
- `--radius-full: 999px` — 胶囊/状态点

### 阴影（暗色发光质感）
- `--shadow-1`：`0 1px 2px rgba(0,0,0,.4), 0 2px 8px rgba(0,0,0,.3)` — 卡片
- `--shadow-2`：`0 4px 12px rgba(0,0,0,.5), 0 8px 24px rgba(0,0,0,.35)` — 浮层
- `--shadow-3`：`0 12px 32px rgba(0,0,0,.6), 0 24px 64px rgba(0,0,0,.5)` — 弹窗
- **霓虹发光**：主色元素带 `box-shadow: 0 0 12px rgba(236,72,153,.4)`（仅用于 active 指示/状态点/主按钮 hover）

### 边框
- 卡片：`1px solid var(--border)` + 微妙内发光 `inset 0 1px 0 rgba(255,255,255,.03)`
- 输入框 focus：`border-color: var(--accent)` + `box-shadow: 0 0 0 3px rgba(236,72,153,.25)`
- 侧栏：右侧 `1px solid var(--border)`

### 组件规范
| 组件 | 规范 |
|------|------|
| 卡片 | `surface-1` 底 + 1px border + radius-lg + shadow-1 |
| 主按钮 | `accent-strong` 底 + 白字 + radius-sm + hover 发光 |
| ghost 按钮 | 透明底 + border + hover 时 bg-hover |
| 输入框 | `bg-panel` 底 + 1px border + radius-sm |
| 表格 | 表头 `text-dim` + 强边框底，行 hover `bg-hover` |
| 标签/胶囊 | radius-full + 语义色软底 + 1px 软边框 |
| 图表 | 主色 accent，系列色 [accent, accent-2, ok, warn, danger, #a855f7] |

## 5. 品牌资产

| 资产 | 形式 | 用途 | 路径 |
|------|------|------|------|
| Logo | PNG 512×512 透明底 | 侧栏品牌区 / 关于页 | `assets/logo.png` |
| Logo 小 | PNG 64×64 | 顶栏/托盘（可选） | `assets/logo-sm.png` |
| 空状态装饰 | CSS/SVG 内联 | 自选为空/暂无预警 | 内联，不单独存文件 |
| 托盘图标 | ICO | 系统托盘 | `taffy_desktop/assets/taffy.ico`（沿用或替换） |

**Logo 设计要求**：猫娘头像剪影，霓虹粉发光轮廓，左眼带青色电竞高光，背景透明，风格简约矢量感（非写实插画），适合小尺寸显示。
