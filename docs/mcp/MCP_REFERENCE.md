# 塔菲 MCP 封装参考实现（零依赖版）

> 把塔菲的行情能力暴露给外部 AI 的 **MCP 工具定义**（纯 JSON schema）：
> `docs/mcp/taffy_market_tools.json`。
> 本文是**参考实现**——MCP 协议（stdio + JSON-RPC 2.0）的最小骨架，纯 Python 标准库，
> 不引任何 MCP SDK。外部 AI 可据此把塔菲端点包装成工具。

## 为什么零依赖

塔菲铁律：不引入新 pip 依赖。MCP 的"服务器"本质是 **stdio 上的 JSON-RPC 2.0 消息帧**——
`initialize` / `tools/list` / `tools/call` 三个方法即可跑通最简客户端。标准库 `json` +
`sys.stdin/stdout` 足够，协议细节以官方 spec 为准（本文件为教学骨架，非生产服务器）。

## 协议骨架（stdlib-only）

```python
"""Minimal MCP server skeleton over stdio (stdlib only).

Reference implementation for wrapping Taffy's market endpoints as MCP tools.
This is a teaching skeleton — a real deployment should live behind the
Bearer-protected desktop API and reuse the shared HTTP client.
"""

from __future__ import annotations

import json
import sys

TOOLS: list[dict] = [...]  # load from docs/mcp/taffy_market_tools.json


def _read_message() -> dict | None:
    line = sys.stdin.readline()
    if not line:
        return None
    return json.loads(line)


def _write_message(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def _handle(request: dict) -> dict:
    method = request.get("method")
    params = request.get("params") or {}
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request.get("id"),
            "result": {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "taffy-market-mcp", "version": "0.1.0"},
            },
        }
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request.get("id"), "result": {"tools": TOOLS}}
    if method == "tools/call":
        name = (params.get("name") or "").strip()
        arguments = params.get("arguments") or {}
        tool = next((t for t in TOOLS if t["name"] == name), None)
        if tool is None:
            return {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "result": {"content": [{"type": "text", "text": f"工具 {name} 不存在。"}]},
                "isError": True,
            }
        # Forward to the Bearer-protected desktop endpoint.  In production
        # this reuses src/services/http_client (trust_env=False + proxy
        # probe) and validates the host — never accept caller-supplied URLs.
        return {
            "jsonrpc": "2.0",
            "id": request.get("id"),
            "result": {
                "content": [{"type": "text", "text": f"[调用 {name} → {tool['endpoint']}]"}]
            },
        }
    return {
        "jsonrpc": "2.0",
        "id": request.get("id"),
        "error": {"code": -32601, "message": f"方法 {method} 不存在"},
    }


def main() -> None:
    while True:
        request = _read_message()
        if request is None:
            break
        _write_message(_handle(request))


if __name__ == "__main__":
    main()
```

## 接入塔菲的要点

1. **认证**：所有端点 Bearer（`ADMIN_API_TOKEN`），请求头 `Authorization: Bearer <token>`；
   token 只从环境变量读，绝不写进代码/文档。
2. **fail-closed 契约**：端点返回 `available:false` 或错误时，工具必须如实转述
   （"暂不可用/积累中"），**禁止让下游模型补写数字**——这是塔菲铁律的延伸。
3. **主机校验**：转发请求前校验目标 host，拒绝 localhost/环回/私有地址（塔菲
   http_client 既有纪律）。
4. **端点来源**：工具清单与 `src/plugins/desktop_api.py` 的薄门面一一对应，
   数字只来自本地已验证数据源（SteamDT 校验链 + CSQAQ + 本地日线）。

## 部署形态

- 本机：`uv run --frozen python -m <module>` 经 stdio 挂进 Claude/其他 MCP 客户端配置
  （`"command": "uv", "args": ["run", "--frozen", "python", "-m", "taffy_mcp"]`）。
- 云端：同一模块挂 systemd；端点仍只绑 127.0.0.1，MCP 客户端经 SSH 隧道访问。
- 注意：生产服务器形态**尚未实现**（本文件是定义+骨架）；需要时按上述骨架补全
  真实 HTTP 转发（复用 `src/services/http_client` 的代理探测与熔断）。
