"""Host-header guard for the listener that serves OneBot ingress and the APIs.

The OneBot V11 adapter verifies an HTTP signature only when ``ONEBOT_V11_SECRET``
is configured, and ``.env.example`` ships it blank, so a default install accepts
unsigned events from anything that can reach the port.  ``src.core.config`` keeps
that listener on loopback unless strong credentials and confirmed TLS
termination are both present, but uvicorn does not check ``Host``, so a page in
the operator's own browser can still be pointed at ``127.0.0.1`` by DNS
rebinding and forge an event whose ``user_id`` decides privilege.

Policy: accept loopback names, literal IPs and operator-listed names; reject any
other hostname.  A literal IP cannot be rebound by a browser, so this closes the
rebinding shape without breaking remote access by address.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from ipaddress import ip_address
from typing import Any

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

__all__ = [
    "HostGuardMiddleware",
    "extra_hosts_from_env",
    "host_is_allowed",
]

_LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})
_WILDCARD = "*"
_ENV_KEY = "TAFFY_ALLOWED_HOSTS"


def parse_allowed_hosts(raw: str | None) -> frozenset[str]:
    """Split the comma-separated allowlist into comparable hostnames."""

    if not raw:
        return frozenset()
    names = {part.strip().casefold().rstrip(".") for part in raw.split(",")}
    return frozenset(name for name in names if name)


def extra_hosts_from_env(environ: dict[str, str] | None = None) -> frozenset[str]:
    """Read the allowlist without ever logging or echoing its contents."""

    values = os.environ if environ is None else environ
    return parse_allowed_hosts(values.get(_ENV_KEY))


def _hostname(header: str) -> str:
    if header.startswith("["):  # bracketed IPv6, optionally followed by :port
        return header.split("]", 1)[0].lower() + "]"
    return header.split(":", 1)[0].lower()


def host_is_allowed(header: str | None, extra: Iterable[str] = ()) -> bool:
    """Decide one ``Host`` value against loopback names plus an allowlist."""

    if not header or not header.strip():
        return False
    allowed = frozenset(extra)
    if _WILDCARD in allowed:
        return True
    name = _hostname(header.strip()).rstrip(".")
    if not name:
        # A value such as ":8080" carries no hostname at all; treating an
        # absent name as acceptable would hand back the bypass this guards.
        return False
    if name in _LOOPBACK_NAMES or name in allowed:
        return True
    literal = name.strip("[]")
    try:
        ip_address(literal)
    except ValueError:
        return False
    return True


class HostGuardMiddleware:
    """Pure ASGI guard so both ``http`` and ``websocket`` scopes are covered."""

    def __init__(self, app: ASGIApp, extra_hosts: Iterable[str] = ()) -> None:
        self.app = app
        self.extra_hosts = frozenset(extra_hosts)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope.get("type")
        if kind not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers: dict[bytes, bytes] = dict(scope.get("headers", []))
        raw = headers.get(b"host")
        header = raw.decode("latin-1") if raw is not None else None
        if host_is_allowed(header, self.extra_hosts):
            await self.app(scope, receive, send)
            return
        if kind == "websocket":
            await _reject_websocket(send)
            return
        await PlainTextResponse("Invalid Host header", status_code=400)(scope, receive, send)


async def _reject_websocket(send: Send) -> None:
    message: Message = {"type": "websocket.close", "code": 1008, "reason": "Invalid Host"}
    await send(message)


def describe_guard(extra_hosts: Iterable[str]) -> str:
    """One-line operator hint; never includes credential material."""

    names: frozenset[str] = frozenset(extra_hosts)
    if not names:
        return f"loopback names and literal IPs only (set {_ENV_KEY} for DNS names)"
    if _WILDCARD in names:
        return f"{_ENV_KEY}=* disabled the Host check; anyone reaching the port may post"
    listed: list[str] = sorted(names)
    return f"loopback names, literal IPs and {', '.join(listed)}"


def guard_summary(extra_hosts: Iterable[str]) -> dict[str, Any]:
    """Structured view used by tests and startup logging."""

    names: frozenset[str] = frozenset(extra_hosts)
    return {
        "wildcard": _WILDCARD in names,
        "configured": sorted(names),
        "text": describe_guard(names),
    }
