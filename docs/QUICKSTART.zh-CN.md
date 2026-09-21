# 塔菲开源版：从下载到使用

这是自托管源码，不含 QQ 登录状态、API 额度、会员或任何密钥。你需要自己运行服务，并承担所选第三方接口的费用。建议先在测试 QQ 账号和测试群中使用。

## 1. 准备源码与环境

安装 Python 3.12 和 uv。下载源码 ZIP 并解压，进入包含 `bot.py` 的目录；不要在 ZIP 内直接运行。

```powershell
uv sync --frozen
Copy-Item .env.example .env
```

如果已有 `.env`，不要执行复制命令覆盖它。用本地编辑器修改配置，不要把密钥贴进聊天、截图或 GitHub issue。

## 2. 配置你自己的连接

- OneBot：独立安装并配置 OneBot V11 实现（例如 NapCat），登录你有权使用的 QQ 账号，设置连接地址与访问令牌，并与 `.env` 中的配置保持一致。
- 聊天模型：填写 `TAFFY_API_KEY`、`TAFFY_BASE_URL`、`TAFFY_MODEL`；`TAFFY_API_FORMAT` 必须匹配接口协议，支持 `anthropic` 或 `openai`。示例配置不是接口可用性的保证。
- 可选行情等服务：只填写你实际拥有的服务凭据。未配置时相关能力可能不可用。
- 管理 API：使用下面命令生成随机令牌，填入 `ADMIN_API_TOKEN`。保持 `HOST=127.0.0.1`，不要把管理端口直接开放到公网。

```powershell
uv run --frozen python -c "import secrets; print(secrets.token_urlsafe(32))"
```

终端会显示你刚生成的令牌，请勿录屏或复制到公开材料。

## 3. 启动与检查

```powershell
uv run --frozen python bot.py
```

保持该终端运行，在另一终端检查存活接口：

```powershell
Invoke-RestMethod http://127.0.0.1:8080/health/live
```

存活响应只说明 HTTP 服务运行，不代表 QQ 已登录、模型可用或行情接口有权限。随后在测试群中 @ 机器人发送一条简单消息，逐项确认 OneBot 连接、模型回复及所需功能。

## 4. 可选 Windows 桌面工作台

在 `.env` 设置 `TAFFY_DESKTOP_API_ENABLED=true`，明确配置 `DESKTOP_OWNER_QQ` 为你自己的账号，重启 bot。然后从源码根目录执行：

```powershell
cd desktop
uv sync --frozen
uv run --frozen python -m taffy_desktop.app
```

首次输入管理员令牌并验证。WebView2 运行时必须可用。远程连接要求 HTTPS；SSH 隧道使用回环地址。令牌按服务器地址保存，切换地址需重新配置对应凭据。详见 [桌面说明](../desktop/README.md)。

## 5. 数据、备份与排错

- `.env`、`data/`、日志和 NapCat 登录目录都属于私有运行数据，不能上传 GitHub。
- 业务审计可能保存消息原文和账号标识，请先阅读 [隐私说明](PRIVACY.md)，告知群成员并确定保留期限。
- 修改代码或升级前备份私有数据；不要用新 ZIP 覆盖运行中的部署目录。
- 无回复：先查 OneBot 连接，再查模型凭据、协议、额度与日志；不要为排错关闭认证。
- 桌面拒绝连接：确认 bot 已启动、端口一致、桌面 API 开关已开启、令牌匹配。
- 提交问题时只附脱敏的错误类型、操作步骤和版本，不附完整配置、数据库、私聊内容或登录二维码。

英文使用入口见 [README](../README.md)。本教程不承诺第三方平台持续可用，也不代表通过了真实 QQ 账号的端到端验收。
