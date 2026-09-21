# 塔菲桌面工作台 Taffy Desktop Studio

一个独立的 Windows 桌面软件：把塔菲的全部能力（聊天人格 / 市场行情 / 预警 / 库存监控 /
资讯 / 学习印象 / 运维管理）装进一个可高度个性化的工作台。桌面端与 QQ 端**共享同一份
人格记忆与风格**（同一 owner 私聊作用域）——在桌面调教的塔菲，QQ 里也是调教后的塔菲。

## 架构一览

```
TaffyDesktopStudio.exe（pywebview + WebView2）
├── 前端 SPA（vendored Vue3 + ECharts，零构建、零外部资源）
│     仪表盘/聊天/市场/预警/库存/资讯/人格印象/数据/运维/设置 十大视图
│     场景切换 · Ctrl+K 命令面板 · 通知规则 · 键盘流 · 主题布局记忆
├── 桌面壳 taffy_desktop/
│     supervisor 进程看护 │ tray 托盘 │ EventPoller+notify 通知中心 │ DPAPI 凭据
└── 全部 HTTP → 127.0.0.1:8080 → bot 侧 src/plugins/desktop_api.py（薄门面）
```

三条铁律：**A1 薄门面**（desktop_api 只做 参数校验→调现有 service→JSON 化）、
**A2 零 schema 变更**（个性化数据存 `%APPDATA%\TaffyDesktop\settings.json`）、
**A3 零重复实现**（能复用 admin 端点的一律复用）。

## 安装（开发模式）

要求：Windows 10/11（WebView2 预装）+ [uv](https://docs.astral.sh/uv/) + bot 仓库同机。

```powershell
cd desktop
uv sync                                   # 安装壳依赖
powershell -ExecutionPolicy Bypass -File scripts/vendor.ps1   # 下载 Vue/ECharts 并打印 sha256
```

bot 侧一次性配置（本地 `.env`，运行时配置不提交）：

```ini
TAFFY_DESKTOP_API_ENABLED=true    # fail-closed 总开关
# DESKTOP_OWNER_QQ=主人QQ号       # 留空则回退唯一的 SUPERUSERS；有多个时必须显式指定
ADMIN_API_TOKEN=<secrets.token_urlsafe(32)>   # 桌面端与 admin API 共用
```

改完重启塔菲：`uv run --frozen python bot.py`（仓库根）。

## 启动

```powershell
cd desktop
uv run python -m taffy_desktop.app            # 正常启动；关窗行为由设置决定（见下）
uv run python -m taffy_desktop.app --minimized
uv run python -m taffy_desktop.app --self-test # 七步自检后退出
```

首次启动：输入 `ADMIN_API_TOKEN` → 校验通过后存入 Windows 凭据库（DPAPI）。
之后令牌只留在 Python 侧，绝不进页面 DOM / localStorage。

## 系统托盘

- 真托盘图标（纯 ctypes `Shell_NotifyIconW`，零额外依赖）：状态四色映射
  （运行绿 / 启动橙 / 停止灰 / 端口被占红），鼠标悬停显示当前状态。
- 左键单击或双击：唤起主窗口。
- 右键菜单：打开工作台 / 启动塔菲 / 停止塔菲 / 重启塔菲 / 退出桌面工作台。
- **关窗行为**：设置键 `minimized_to_tray_on_close` 默认 **false**（关窗即退出，
  保持历史行为）；在设置页开启后，关窗改为隐藏进托盘（进程保活，托盘可唤起）。
- "退出桌面工作台"走优雅退出链（停看护/关隧道/清锁），不停 bot。

## 打包

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build.ps1
# 产物 dist\TaffyDesktopStudio.exe（onefile + windowed + 图标）
```

## 安全边界

- 桌面软件零监听端口；请求仅指向 127.0.0.1:8080。
- 令牌仅存 DPAPI；日志/异常/DOM 不出现明文。
- 所有动态内容经 esc() 后入 DOM；聊天消息按纯文本渲染。
- 写操作二次确认；删除类操作不可无确认批量执行。
- desktop API 默认关闭（fail-closed），关闭时全部 404。

## 已知边界

- 桌面聊天与 QQ 共享 LLM 每分钟额度（chat 管线自带全局限流，勿绕过）。
- K 线数据经 fresh_kline_points 校验后才展示，坏数据宁拒勿传。
- 关闭工作台不会停止塔菲本体（DETACHED 启动 + F7 语义）。
