# CONTRACT — /api/desktop/* 契约文档

> 生成依据：`src/plugins/desktop_api.py`（批次 0）+ FastAPI 自动 schema。
> 通用约定见下；每端点给出请求/响应示例。错误形状分两类，见「响应形状」。

## 通用纪律

| 项 | 约定 |
|---|---|
| Base URL | `http://127.0.0.1:8080` |
| 鉴权 | `Authorization: Bearer <ADMIN_API_TOKEN>`；弱令牌→整体 503；错令牌→401（同 admin 纪律，失败计数限流 429） |
| 总开关 | `TAFFY_DESKTOP_API_ENABLED=false` 时**所有** desktop 端点 404 |
| owner | `DESKTOP_OWNER_QQ` → 否则回退**唯一的** superuser。有**多个** superusers 而 `DESKTOP_OWNER_QQ` 未配置时**拒绝解析**（不替运维挑账号，见 M-110）；与「两者皆缺」同样返回 503 `OwnerNotConfiguredError`（提示信息区分两种情形） |
| 限流 | chat 12/min/会话、market 30/min、写 10/min；超限 429 + `Retry-After: 60` |
| 审计 | 全端点写 usage_log：feature=`desktop.<模块>`、command=端点路径、input_text≤200 字符（聊天正文截 200 存档） |

## 响应形状

**成功与处理器内业务错误**（信封）：

```json
{"ok": true,  "data": { ... }}
{"ok": false, "error": "<异常类型名>", "message": "<中文一句话>"}
```

业务错误状态码映射：400 InvalidParameterError · 404 NotFoundError · 409 ConflictError ·
429 CapacityError/限流 · 503 OwnerNotConfiguredError / UpstreamUnavailableError / MarketDataError。

**传输层门禁**（开关 404、Bearer 401、依赖级限流 429）与 admin API 同形状：
FastAPI 默认 `{"detail": "..."}`。此为有意设计——不改变 admin 既有契约，避免零回归风险。

---

## Chat

### POST /api/desktop/chat
```json
请求: {"message": "查一下红线", "session": "d_10001_alt?"}   // session 可省略，默认 d_<owner>；必须 ^d_[A-Za-z0-9_-]{1,80}$
响应: {"ok":true,"data":{"reply":"好的喵～","intent":"price","elapsed_ms":1832,"session":"d_10001"}}
```
- 复用 `chat_service.chat(persona_scope=PersonaScope(owner,0))` —— 与 QQ 私聊同一记忆域。
- intent 为 fact_intent 检测结果（"price"/"market"/...），无匹配为 null。

### GET /api/desktop/chat/history?limit=20&session=
```json
{"ok":true,"data":{"session":"d_10001","messages":[{"role":"user","content":"…"}],"count":2}}
```

### DELETE /api/desktop/chat/history?session=
`{"ok":true,"data":{"session":"d_10001","deleted":true}}`；会话被 LLM 占用时 409 ConflictError。

## Market

### GET /api/desktop/market/overview
任一部分失败置 null 不整体失败：
```json
{"ok":true,"data":{
  "broad": {"index":102.5,"change_percent":1.2,"change_value":1.22,"timestamp":1756000000.0},
  "broad_error": null,
  "temperature": {"available":true,"score":-15.0,"state":"偏冷","detail":"…",
                  "factors":{"change_1d":-0.5,"change_3d":-1.2,"change_7d":-2.0,"deviation_7d":-0.8}},
  "breadth": {"compared":2500,"rising_ratio":42.0,"median_change":-0.31,
              "sell_depth":12000,"bid_depth":8000,"latest_bucket":1234567800}}}
```

### GET /api/desktop/market/index-closes?days=90   （days 7..365）
`{"ok":true,"data":{"points":[{"date":"2026-08-21","close":103.0}],"count":90}}`

### GET /api/desktop/market/dip · /prediction
`{"ok":true,"data":{"report":"📉 【抄底·大盘历史情景】\n…"}}`（QQ 同一管线文本卡）

### GET /api/desktop/market/rankings?board=涨跌榜&category=刀具&limit=10
board ∈ {异动榜,涨跌榜,流动性榜}，否则 400。`{"ok":true,"data":{"report":"📊 …"}}`

### GET /api/desktop/market/search?q=红线&limit=20
`{"ok":true,"data":{"results":[{"market_name":"AK-47 | Redline (Field-Tested)","display_name":"AK-47 | 红线 (久经)"}],"count":1}}`

### GET /api/desktop/market/item?name=红线
resolve 失败→400；上游报价过期→503 MarketDataError（fail-closed 文案透传）。
```json
{"ok":true,"data":{"name":"AK-47 | Redline (Field-Tested)",
  "quotes":[{"platform":"BUFF","sell_price":12.34,"sell_count":88,"bid_price":11.0,"bid_count":5,"timestamp":1755000000.0}],
  "average":{"overall":12.9,"buff":13.1},
  "change_24h":{"latest_price":12.34,"change_percent":2.5}}}
```

### GET /api/desktop/market/kline?name=AK-47%20Redline&style=
style 可选官方特殊款代码（黑珍珠/蓝宝石/…/一档），未知→400。
点集经 fresh_kline_points 校验（<2 点拒绝、3 天时效、OHLC 一致性），格式 `[ts,o,c,h,l]`：
```json
{"ok":true,"data":{"name":"AK-47 Redline","style":null,"points":[[1754980000,10.0,10.5,11.0,9.5]],"count":30}}
```

### GET /api/desktop/market/watchlist-quotes?refresh=0
本地快照为主；refresh=1 逐项实时报价（限流内，最多前 20 项）：
`{"ok":true,"data":{"items":[{"display_name":"…","snapshot":{...},"quotes":null}],"count":1,"truncated":false}}`

## Alerts

### POST /api/desktop/alerts
```json
请求: {"name":"AK-47 | Redline (Field-Tested)","direction":"below","target":9.9}   // direction ∈ above|below
响应: {"ok":true,"data":{"id":"abc123","market_name":"…","direction":"below","target_price_cents":990,"status":"pending","created_at":1755000000}}
```
配额满（20 条）→ 429 CapacityError；其余 CRUD 走 `/api/admin/alerts*`。

## Inventory

### GET /api/desktop/inventory/state?event_limit=20
只读三段（参数化 SQL，admin events 同纪律）：
```json
{"ok":true,"data":{
  "accounts":[{"steam_id":"7656…","nickname":"主号","source_time":1755000000,"last_success_at":1755000000,"next_check_at":1755003600,"last_error":null,"consecutive_failures":0}],
  "subscriptions":[{"owner_user_id":10001,"destination_type":"group","destination_id":123,"steam_id":"7656…","nickname":"主号","created_at":1755000000}],
  "events":[{"id":"e1","steam_id":"7656…","nickname":"主号","summary":"新增物品…","created_at":1755000000}]}}
```

## News

### GET /api/desktop/news/official
`{"ok":true,"data":{"text":"🎮 【官方更新】…","updates":[{…}]|null,"upcoming":[{…}]|null,"completed":[{…}]|null}}`
结构化条目字段：title/summary/url/league/team_a/team_b/score_a/score_b/time(iso)。

### GET /api/desktop/news/reddit?unread=1
`{"ok":true,"data":{"posts":[{"post_id":"t3_abc","subreddit":"CS2Leak","title":"…","summary":"","author":"","url":"https://…","published_at":1755000000,"unread":true}],"count":1,"unread_only":true}}`
unread=0 返回最近已投递列表。

### POST /api/desktop/news/reddit/read
`请求 {"post_ids":["t3_abc"]}` → `{"ok":true,"data":{"requested":1,"marked":1}}`
（A3 补充端点：admin 无 reddit 能力，理由记录于 DELIVERY.md）

## Persona

### GET /api/desktop/persona/profile
```json
{"ok":true,"data":{"owner_user_id":10001,"scope":"私聊","learning_enabled":true,"updated_at":1755000000,
  "style":{"concise":1,"warm":2,"teasing":0,"proactive":0,"catchphrase":-1,"direct":1},
  "nickname":"小塔菲",
  "memories":[{"id":2,"kind":"note","content":"主人喜欢简洁回复","created_at":2,"updated_at":2}]}}
```

### POST /api/desktop/persona/style
`请求 {"action":"learning_on|learning_off|undo|reset"}` →
`{"ok":true,"data":{"action":"undo","undone":true,"learning_enabled":true,"style":{…}}}`
无可撤销→409 ConflictError。

### POST /api/desktop/persona/memory
`请求 {"content":"主人偏好简短回复","kind":"note|nickname"}` → 记忆对象；
容量满（20 条）→ 429 CapacityError；空/超长→400。

### DELETE /api/desktop/persona/memory?index=3
序号从 1 计（nickname 在前）；不存在→404。`{"ok":true,"data":{"deleted":"…","index":3}}`

## Analytics

### GET /api/desktop/analytics/usage?days=30 （7..90）
按日分组使用固定 UTC+8 偏移（与市场时区 Asia/Shanghai 一致）：
```json
{"ok":true,"data":{"days":30,
  "daily":[{"day":"2026-08-21","feature":"desktop.chat","calls":5}],
  "top_users":[{"user_id":10001,"calls":9}], "total":14}}
```

### GET /api/desktop/analytics/alerts?days=30
`{"ok":true,"data":{"days":30,"by_status":[{"status":"delivered","count":3}],
  "delivered_daily":[{"day":"2026-08-21","count":2}],"failed_daily":[{"day":"2026-08-22","count":1}]}}`

## System

### GET /api/desktop/system/summary
`{"ok":true,"data":{"health":{…collect_health_snapshot…},"scheduler":{…config+version+updated_at},
  "version":"0.1.0","uptime_seconds":7200,"desktop_owner_configured":true}}`

---

## 桌面前端实际使用的 admin 端点（复用清单）

`/api/admin/status` `/logs` `/events` `/alerts`(GET) `/alerts/{id}`(DELETE)
`/watchlist/{user}/{index}`(DELETE) `/permissions*` `/usage` `/learning*`
`/scheduler`(GET/PUT) `/broadcast` — 形状以 admin_api.py 与其测试为准。

---

## V0.2 新增端点（批次 A/B/C）

### 批次 A：K 线兜底

- `GET /market/kline?name=&style=&fallback=auto|off`
  - 上游成功 → `source:"upstream"`；上游失败且本地快照 ≥14 个采样日（特殊款不支持）→
    `source:"local_snapshot"` + `note:"本地快照聚合K线（口径：每小时在售价，仅覆盖采样池品种）"`。
  - 本地聚合实现：直读 `quote_snapshots`（market-history 库，只读 SQL）——任务单允许的两个方案之一；
    缺表/锁定视为"无本地数据"（fail-closed，不造线）。

### 批次 B：用户中心（全部只读聚合 + 隐私红线）

- `GET /users?q=<纯数字QQ>&limit=` → 活跃用户（近30天调用/功能分布）。
- `GET /users/{qq}/overview` → 360°：7/30 天调用、Top 命令、活跃群、预警三态计数、自选数、
  人格学习/风格/记忆数、`moderation_trail`（涉该用户的管控留痕）。
- `GET /users/{qq}/timeline?days=&cursor=&limit=` → 使用流水（id 游标降序）。
- `GET /users/{qq}/chat-log?scope=group|private&days=&confirm_view_private=&limit=`
  - **隐私红线**：`scope=private` 且 qq≠owner 时，无 `confirm_view_private=1` → **403 ForbiddenError**；
    放行时响应带 `privacy_notice` 警示。owner 查自己无需确认。
  - 群聊按 `session_key LIKE 'g\_%\_<qq>'`（转义匹配）。
- 数据管理权（POST，二次确认由前端承担）：`/users/{qq}/clear-session`（按 session_guard 逐会话删）、
  `/clear-memory`、`/cancel-alerts`（仅 pending/claimed，delivered/failed 留史）、`/clear-watchlist`。

### 批次 C：群务管控（OneBot 实权；bot 未连接→503；权限不足→中文提示）

- `GET /moderation/groups` → `get_group_list`；`GET /moderation/groups/{gid}/members` → 前 200 人。
- `POST /moderation/mute` `{group_id,user_id,minutes}`（0=解除；duration=分钟×60）。
- `POST /moderation/kick` `{group_id,user_id,reject_add_request}`。
- `POST /moderation/recall` `{message_id,user_id?}`（user_id 仅用于审计留痕）。
- 全部写审计 `feature=desktop.moderation`（input 含 `user=<qq>`，供 overview 留痕检索）。

### V0.2 已知边界（设计决策）

- 撤回消息的 UI 是"填消息 ID"而非聊天流水内嵌按钮：`session_messages` 不存 OneBot
  `message_id`，且 A2 禁止 schema 变更——端点已备，等未来可选迁移再内嵌。
