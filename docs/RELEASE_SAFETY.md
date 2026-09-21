# 发布安全说明 / Release safety

桌面远程连接必须使用 HTTPS；仅回环 IP（例如 `http://127.0.0.1:18080`）允许 HTTP，用于本机服务或 SSH 隧道。前端不提供读取已保存管理员令牌的接口。

桌面令牌按目标服务器地址分别保存。升级后旧版未绑定地址的令牌不会自动复用，请重新验证并保存；旧凭据可在操作系统凭据管理器中删除。更换服务器时需要为新地址单独配置令牌。

本开源版不附带部署凭据、登录状态、运行数据库、聊天记录或私人服务器配置。
发布时仅使用 Git 跟踪的文件，或使用本次提供的源码 ZIP；不要上传原项目、虚拟环境、备份及运行后生成的文件。

桌面 SSH 隧道默认禁用。只有同时设置 `TAFFY_SSH_HOST`、`TAFFY_SSH_USER`、`TAFFY_SSH_KEY` 才启用；私钥必须留在仓库外。
首次使用前请通过独立可信渠道核验服务器公钥，并写入自己的 SSH known_hosts。隧道仅绑定回环地址，退出仅终止自己创建的 SSH 进程。
手动工具 `desktop/scripts/taffy-tunnel.ps1` 改为前台运行，必须显式传入 `-RemoteHost`、`-RemoteUser` 和 `-KeyFile`；Ctrl+C 停止。
历史更新日志关于默认自动隧道或接管其他 SSH 进程的描述不适用于此发布版。

重新部署时从 `.env.example` 创建自己的 `.env`，生成新的独立令牌。不要把管理端口开放到公网。
监听器默认只接受回环名称与 IP 字面量作为请求的 `Host`，以阻断浏览器被 DNS 重绑定后访问本机端口的路径。通过域名反向代理访问时必须设置 `TAFFY_ALLOWED_HOSTS=bot.example.com`（可逗号分隔），否则请求返回 400；取值 `*` 会完全关闭该校验，只有在你确认不存在能让浏览器指向本机的域名时才可使用。留空 `ONEBOT_V11_SECRET` / `ONEBOT_V11_ACCESS_TOKEN` 时本机进程仍可无签名上报事件，因此不要把端口暴露给其他本机用户。
业务审计可能存储原始消息及用户标识，详见 [隐私说明](PRIVACY.md)。

This source release excludes deployment credentials and runtime data. SSH is opt-in, requires explicit host/user/key configuration, verifies known host keys, and binds only to loopback. Do not publish private keys, environment files, databases, logs, or backups. Static scanning and tests reduce risk but are not a guarantee that all vulnerabilities have been eliminated.
