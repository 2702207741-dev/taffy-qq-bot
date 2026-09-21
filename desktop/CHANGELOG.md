# Changelog — taffy-desktop-studio

所有显著变更记录于此。格式参考 Keep a Changelog；版本语义 0.x 阶段。

## [0.5.0] - 2026-08-31

V0.5 增量（对齐 bot 侧 V0.4 P4/V0.5 窗口 A/B 功能面）：市场详情新增挂刀参考与
开箱EV 查询卡；版本号三处同步（pyproject / `__init__` / 本文件）。

### Added — 市场页
- 「挂刀参考」按钮：单品挂刀比例（快照跟踪才显示；未跟踪/无快照如实
  available=false，绝不编造）。
- 「开箱EV」按钮：EV 下界 / 箱子现价 / 回报率（Valve 公开掉率 × 本地已校验
  报价），固定附"历史数据不构成收益承诺"。

### Changed
- 版本号统一 0.5.0（此前 pyproject 0.4.0 与 `__init__` 0.3.1 不同步）。

## [0.3.1] - 2026-08-27

V0.3.1 增量：库存监控页接入囤货雷达（对应 bot 侧 P0+P1）。

### Added — 库存页
- 新增「囤货轨迹」卡：按 SteamID + 天数（7/30/90）查询进出摘要与时间线，
  复用 QQ 同源服务层（owner 订阅校验在服务内部，越权 fail-closed）。
- 新增「囤货画像（缓存只读）」卡：仅展示 6 小时内已生成的画像（含模型与
  生成时间），面板不直接调用 LLM；未命中缓存时引导去 QQ 生成。
- 监控账号卡点击即把 SteamID 填入两个查询框。

### bot 侧（desktop_api.py，薄门面）
- 新端点 `GET /api/desktop/inventory/trajectory`（转调 service.trajectory）与
  `GET /api/desktop/inventory/profile`（只读 reports 缓存）。
- 契约测试 +4：trajectory 委派参数断言、无 owner fail-closed、画像缓存命中/
  未命中语义、越权 owner 拒绝且不发起缓存查询。全量 1304 passed。

## [0.3.0] - 2026-08-27

V0.3 增量（任务单 `TAFFY_DESKTOP_REMOTE_TASK.md`：远程化——直连云端塔菲）。

### Added — 连接远程地址（bot 侧零改动）
- 设置页新增「连接（远程地址）」卡片：填入 SSH 隧道地址（如
  `http://127.0.0.1:18080`）后，仪表盘/市场/用户/令牌校验等全部请求改走
  该地址，数据直连云端塔菲；留空恢复本机默认。
- 校验规则：仅 http(s) 绝对地址、host 非空、不含路径/查询/片段/内嵌凭据、
  端口合法且非 0；非法值保存时拒绝并提示，绝不静默改默认。
- **即时生效**：每次请求重解析 %APPDATA% 设置（零缓存），状态轮询/通知
  轮询/自检 ④⑤ 自动跟随，无需重启。
- **隧道随软件托管**（`taffy_desktop/tunnel.py`）：打开工作台自动拉起 SSH
  隧道（外部已开则接管统一托管），关闭窗口或托盘退出自动关闭隧道；断线
  15 秒内自动重连；托盘退出从 `os._exit` 改为优雅关闭（清理链包含隧道）。
  手动工具 `scripts/taffy-tunnel.ps1` 保留。
- 安全边界：令牌仍只存 DPAPI、经 Python 侧注入，前端不接触新秘密；不新增
  监听端口；请求不跟随重定向（防被诱导跳转他域）；日志不含 URL 全文。

### Fixed — 设置页白屏（历史 bug，V0.2 起存在）
- `settings.js` 模板直接引用 `store.settings.*` 但组件 data() 未挂 `store`，
  渲染即崩（CDP 实机定位）；设置页整页空白至今才被用户撞见。data() 挂
  `store` 修复，CDP 复测 11 视图全正常。
- WebView2 缓存目录固定 + 版本变更自动清缓存；关于页显示真实版本号。

### 测试
- +17 例：normalize 校验矩阵、resolve 回退、settings round-trip、MockTransport
  验证自定义 base 请求 URL、同一 client 连续请求设置变更即时生效、
  Bridge get/set 校验（非法值不破坏已存）、http_request 跟随远程 base、
  save_settings 对非法值消毒、隧道托管 7 例（起/停/接管/重连/缺 ssh 降级）。
- 全量 68 passed；ruff / format / mypy 全绿。

## [0.2.0] - 2026-08-23

V0.2 增量（任务单 `TAFFY_DESKTOP_V0.2_TASK.md`：K线体验 / 用户中心 / 管控权）。

### Added — bot 侧（desktop_api.py，仍为薄门面/零 schema）
- K 线兜底：上游失败时用本地每小时快照聚合真实日线（≥14 采样日才出线，
  `source=local_snapshot` + 口径标注；缺表/数据不足 fail-closed 不造线）。
- 用户中心 8 端点：`/users` 列表聚合、`/overview` 360°（含管控留痕）、`/timeline`
  id 游标流水、`/chat-log`（群聊直读；**他人私聊默认 403，需显式二次确认**）、
  四个数据管理动作（清会话/清记忆/取消预警/删自选，全部走现有仓储）。
- 群务管控 5 端点：群列表/群成员/禁言(0=解除)/踢人/撤回，经 OneBot
  `connected_bot()`；未连接→503、权限不足→中文提示、全部审计留痕。

### Added — 前端
- 新视图「👥 用户」（第 11 个导航项）：列表→单人 360°（概览/使用时间线/聊天流水/
  功能封禁面板/群务管控面板/数据管理权）；私聊二段确认弹窗 + 警示条；踢人需
  输入群号后四位防误伤；封禁面板复用 admin `/permissions*` 端点即时生效。
- 市场 K 线体验重构：图表容器常驻 + 三态占位（加载中/无数据+重试/图表）、
  报价与 K 线并行独立成败（报价挂了 K 线照画）、零宽容器逐帧重试（最多 8 次）、
  本地聚合线显著口径标注、磨损一键切换五档 chips。

### Fixed
- 主人实测"搜怪兽在B无K线"：根因是前端多条静默失败路径（详见任务单 F-A2），
  上游数据本身完好——本次重构后三态显式化，不再静默消失。

### 已知边界
- 撤回消息 UI 为"填消息 ID"（session_messages 不存 OneBot message_id，A2 禁改
  schema）；端点已备，见 CONTRACT.md V0.2 节。

## [0.1.1] - 2026-08-23

复核修复轮（复核评分 84 → 见 DELIVERY.md「复核修复记录」）。

### Fixed — bot 侧
- `/api/desktop/market/search` 前缀查询崩溃：`sorted(prefix_hits)` 在 rank 并列时
  比较 `CatalogEntry`（不可排序）→ TypeError。改为仅按 rank 排序（稳定插入序）。
- 同端点混合别名查询（如「AK红线」）永远空结果：新增分词 AND 兜底——原始
  needle 无命中时，要求查询的全部 ASCII/CJK 词元同时出现在条目名中；
  「AK红线」现可命中 AK-47 | Redline 全磨损系列（Q8 由此真正可复现）。
- 新增契约测试：前缀 rank 并列不崩、混合别名兜底命中（bot 侧 1257 passed）。

### Fixed — 桌面壳
- 离线态 `--self-test` 崩溃：`health_ready()` 违反自身 docstring（不可达应返回
  None 却抛 ConnectionError），自检④随之崩溃。已按契约修复，⑤补"Bot 离线，
  开关状态未知"降级分支；新增连接拒绝回归测试。此前交付只测过 503 mock，
  真离线路径未被覆盖——本次停机窗口实测补采了双状态自检证据。
- 自检③进程树去重：新增 `ProcInfo.ppid` 与 `SupervisorStatus.bot_instances`，
  显示改为「bot 实例=1（进程树 pids=[…]）」，不再把 uv 启动器/跳板/真实进程
  误读成 3 个实例。

## [0.1.0] - 2026-08-23

首个可用版本（批次 0-5 全量交付）。

### Added — bot 侧
- `src/plugins/desktop_api.py`：`/api/desktop/*` 薄门面，25 个端点
  （chat/history、market 全家桶含原生 K 线 JSON、alerts 创建、inventory 只读态、
  news 官方+爆料+已读标记、persona 三件套、analytics 聚合、system summary）。
  总开关 `TAFFY_DESKTOP_API_ENABLED` 默认 false（fail-closed→404）；
  Bearer 复用 admin 纪律；chat 12/min/会话、行情 30/min、写 10/min 滑窗限流；
  全端点审计 usage_log（feature=desktop.*，聊天正文截 200）。
- `tests/test_desktop_api.py`：46 条契约测试（401/404/429/503/信封/fail-closed/审计），
  desktop_api 行覆盖率 86%。
- `bot.py` `_PLUGINS` 追加一行；`scripts/check_secrets.py` 扫描 desktop/；
  `.env.example` 补两个键说明。

### Added — 桌面壳
- `taffy_desktop/config.py`：仓库根向上定位、%APPDATA% settings.json 读写。
- `taffy_desktop/credentials.py`：令牌 DPAPI(keyring) 存储 + 内存降级。
- `taffy_desktop/bot_api.py`：health/token/desktop API 探测客户端。
- `taffy_desktop/supervisor.py`：uv 跳板进程树发现、8080 端口归属交叉验证
  （BOT_PORT_FOREIGN 态禁启动）、两段式停止（温和5s→taskkill /T /F）、
  DETACHED 启动（关软件不停 bot）、10s 巡检 / 30s 重启退避看护。覆盖率 90%。
- `taffy_desktop/single_instance.py`：命名互斥体 + 标志文件双通道单实例锁与窗口激活。
- `taffy_desktop/tray.py`：状态色托盘（ctypes Shell_NotifyIconW，零额外依赖）。
- `taffy_desktop/notify.py` + `app.EventPoller`：通知中心——事件轮询去重、
  toast/免打扰时段/5 分钟聚合规则，windows-toasts 失败降级历史列表。
- `taffy_desktop/app.py`：js bridge（http_request 统一代理——令牌不出 Python 侧、
  get_token/get_state/open_external 仅 https、设置持久化）、--self-test 七步自检、
  --minimized、凭据首启向导（在线校验后入 DPAPI）。

### Added — 前端 SPA（vendored Vue3.4 + ECharts5.5，无构建）
- 十大视图：仪表盘（可拖拽磁贴）、聊天（气泡流/命令补全/限流提示/人格上下文条）、
  市场（搜索防抖/原生 candlestick K线 MA5/MA10 缩放平移/大盘日线/榜单/自选分组）、
  预警（三步向导/触发时间线）、库存、资讯（未读加粗/标已读）、人格印象
  （印象审核/长期记忆/6 维雷达）、数据分析（堆叠柱状/趋势/CSV 导出）、
  运维（状态/日志尾随/广播定时/权限/审计 + 进程看护卡）、设置全项。
- 个性化：四预置场景+自定义另存、快捷指令（Ctrl+K 命令面板三类命中）、
  通知规则中心、Ctrl+1..9 / F5 键盘流、亮暗主题×4 主色×布局记忆。

### Security
- CSP 收敛到 'self'；connect-src 'none'（全部请求经 bridge 代理）。
- check_secrets 通过（含 desktop 目录）。

### Notes
- 前端直连 fetch 因 file:// 源无 CORS 头不可行 → 改为 bridge HTTP 代理，
  令牌完全不进 DOM（比任务单"前端内存持有"更强的边界），详见 DELIVERY.md 设计偏差节。
