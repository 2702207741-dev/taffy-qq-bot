"""Bot HTTP 探测客户端：health/token/desktop API。

安全模型（任务单 V0.3 红线）：base_url 只来自用户显式配置（%APPDATA%
settings 或 UI），用户配置即授权；请求发自用户本机桌面客户端、无服务端
上下文，SSRF 不适用。校验上仍收紧：仅 http(s)、禁内嵌凭据、禁路径/查询、
不跟随重定向（防被诱导跳转他域），远程必须 HTTPS；HTTP 仅允许回环 IP。SSH 隧道目标
正是 127.0.0.1:18080 这类环回地址，IP 黑名单会直接打断本功能。

base_url 默认 None = 每次请求即时解析 settings（remote_base_url 或本地
默认），设置变化无需重启即生效；显式传入则固定使用（测试/mock）。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import load_settings, normalize_base_url, resolve_base_url

_log = logging.getLogger("taffy.desktop.bot_api")

DEFAULT_TIMEOUT = 4.0


class BotApiClient:
    """Small synchronous client; every call fails closed to a typed error."""

    def __init__(
        self,
        base_url: str | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_override = normalize_base_url(base_url) if base_url else None
        if base_url and self._base_override is None:
            raise ValueError("API URL requires HTTPS or literal HTTP loopback")
        self._client: httpx.Client | None = (
            httpx.Client(transport=transport, follow_redirects=False)
            if transport is not None
            else None
        )

    @property
    def base_url(self) -> str:
        """Effective base URL; re-resolved from settings on every access."""

        return self._base_override or resolve_base_url(load_settings())

    def _request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        origin: str | None = None,
    ) -> tuple[int, Any]:
        headers: dict[str, str] = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        base = normalize_base_url(origin) if origin is not None else self.base_url
        if base is None:
            raise ValueError("Invalid API origin")
        url = f"{base}{path}"
        try:
            if self._client is not None:
                response = self._client.request(method, url, headers=headers)
            else:
                response = httpx.request(
                    method, url, headers=headers, timeout=timeout, follow_redirects=False
                )
        except httpx.HTTPError as exc:
            _log.warning("Request %s %s failed error=%s", method, path, type(exc).__name__)
            raise ConnectionError(type(exc).__name__) from exc
        try:
            payload: Any = response.json()
        except ValueError:
            payload = response.text
        return response.status_code, payload

    def health_ready(self) -> dict[str, Any] | None:
        """Anonymous readiness probe (F3); None when the bot is unreachable."""

        try:
            status, payload = self._request("GET", "/health/ready")
        except ConnectionError:
            # 拒绝连接/超时等一律按"不可达"处理，调用方（自检④、get_state）
            # 依赖这里返回 None 而不是抛异常。
            return None
        if status == 200 and isinstance(payload, dict):
            return payload
        return None

    def verify_token(self, token: str, *, origin: str | None = None) -> bool:
        """A candidate token is valid iff the admin status endpoint answers 200."""

        status, _ = self._request("GET", "/api/admin/status", token=token, origin=origin)
        return status == 200

    def desktop_summary(self, token: str | None, *, origin: str | None = None) -> tuple[int, Any]:
        """Probe /api/desktop/system/summary; 404 means the switch is off."""

        return self._request("GET", "/api/desktop/system/summary", token=token, origin=origin)

    def tail_logs(self, token: str, lines: int = 30, *, origin: str | None = None) -> str | None:
        status, payload = self._request(
            "GET", f"/api/admin/logs?lines={int(lines)}", token=token, origin=origin
        )
        if status == 200 and isinstance(payload, dict):
            content = payload.get("content")
            return content if isinstance(content, str) else None
        return None
