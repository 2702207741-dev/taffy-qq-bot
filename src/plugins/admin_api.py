"""管理 API — 运行时可见性 + 数据访问（Bearer 令牌保护）。"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import re
import secrets
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, cast

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from nonebot import get_app, get_driver

from src.core.config import get_admin_api_token
from src.core.health import collect_health_snapshot, public_readiness
from src.core.ingress_guard import (
    HostGuardMiddleware,
    describe_guard,
    extra_hosts_from_env,
)
from src.core.rate_limit import SlidingWindowLimiter
from src.services import panel_stats
from src.services.authority import FEATURES
from src.services.index_history import index_history_store
from src.services.market import get_broad_summary
from src.services.market_history import (
    _MIN_COLUMN_ITEMS,
    _MIN_COVERAGE_RATIO,
    MarketHistoryStore,
    market_history,
)
from src.services.market_prediction import historical_scenario_probs
from src.services.market_temperature import TemperatureUnavailable, get_market_temperature
from src.state.alert_repository import AlertStatus
from src.state.broadcast_repository import DeliveryStatus
from src.state.permission_repository import PermissionOverride
from src.state.runtime import alerts as alert_repository
from src.state.runtime import (
    authority,
    broadcasts,
    group_pulse_log,
    learning_insights,
    permissions,
    schedules,
    usage_log,
    watchlists,
)
from src.state.schedule_repository import ScheduleVersionConflictError
from src.state.session_repository import SessionMessage, SessionSummary
from src.state.usage_log_repository import UsageRecord

if TYPE_CHECKING:
    from .session_store import SessionStore

_ROOT = Path(__file__).resolve().parent.parent.parent


def _log_file() -> Path:
    """Resolve the sanitized sink file so panel log views match real output."""

    from src.core.logging_privacy import resolve_log_directory

    return resolve_log_directory(_ROOT) / "taffy.log"


_log = logging.getLogger("taffy.admin")

_DUMMY_TOKEN = "0" * 32
_bearer = HTTPBearer(auto_error=False)
_HEALTH_CACHE_TTL_SECONDS = 2.0
_EXPORT_ROW_LIMIT = 10_000
_AUTH_FAILURE_LIMIT = 10
_AUTH_FAILURE_WINDOW_SECONDS = 300
_TAIL_BLOCK_BYTES = 262_144
#: Concurrent sends per wave for the manual admin broadcast (mirrors the
#: scheduler's default wave size).
_BROADCAST_SEND_CONCURRENCY = 5
_BROADCAST_TOTAL_DEADLINE_SECONDS = 300.0


def _broadcast_total_deadline_seconds() -> float:
    """Overall wall-clock budget for one manual broadcast, read at call time.

    Deliberately **not** a route parameter: ``admin_broadcast`` is a FastAPI route,
    and a scalar parameter with a default that is not marked ``Query(...)`` is
    published as a query-string knob — i.e. it **exposes the knob to anyone who can
    read the API catalogue** (the anonymously reachable ``/openapi.json``).  Calling
    it still needs the Bearer token, so this is a documentation/discovery surface
    rather than a bypass; but a published admin-only timeout switch is still the
    wrong shape.  Keeping the route signature untouched and reading the module
    constant here (rather than binding it into a local once at import) is what lets
    tests inject a short budget without adding any public surface.
    """

    return _BROADCAST_TOTAL_DEADLINE_SECONDS


_auth_failure_limiter = SlidingWindowLimiter(_AUTH_FAILURE_LIMIT, _AUTH_FAILURE_WINDOW_SECONDS)
_log.warning(
    "Admin API auth-failure throttling counts per TCP peer in-process; run a "
    "single worker and keep the panel off shared reverse proxies (see "
    "docs/RELEASE_SAFETY.md)"
)
_health_cache_lock = asyncio.Lock()
_health_cache_expires_at = 0.0
_health_cache_value: dict[str, Any] | None = None


async def require_admin_api_token(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(_bearer),
    ],
    request: Request,
) -> None:
    """Fail closed unless a strong ADMIN_API_TOKEN matches in constant time.

    Failed attempts are counted per real TCP peer (``request.client``, which
    ignores spoofable ``X-Forwarded-For`` headers); a peer that exceeds the
    threshold is throttled with 429 instead of being allowed to keep guessing.
    """

    expected = get_admin_api_token(get_driver().config)
    supplied = credentials.credentials if credentials is not None else ""
    # compare_digest rejects non-ASCII str (headers decode as latin-1 so
    # arbitrary bytes can arrive); comparing UTF-8 bytes turns any payload
    # into a plain mismatch that still counts toward the failure limiter.
    token_matches = secrets.compare_digest(
        supplied.encode("utf-8"), (expected or _DUMMY_TOKEN).encode("utf-8")
    )

    if expected is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin API is disabled",
        )
    if credentials is None or credentials.scheme.casefold() != "bearer" or not token_matches:
        peer = request.client.host if request.client is not None else "unknown"
        if not await _auth_failure_limiter.allow(peer):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many failed attempts; try again later",
                headers={"Retry-After": str(_AUTH_FAILURE_WINDOW_SECONDS)},
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )


def prevent_response_cache(response: Response) -> None:
    """Set defense-in-depth cache headers on router-generated responses."""

    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _session_store() -> SessionStore:
    """Resolve the singleton lazily to avoid plugin import-order coupling."""

    from .session_store import store

    return store


async def _session_summaries(*, limit: int, offset: int) -> list[SessionSummary]:
    """Read bounded metadata without loading complete conversation histories."""

    async with _storage_unavailable(
        "Admin session summary query", "Session storage is unavailable"
    ):
        return await _session_store().list_summaries(limit=limit, offset=offset)


async def _session_messages(key: str) -> list[SessionMessage]:
    try:
        return await _session_store().get(key)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid session key") from exc
    except Exception as exc:
        _log.error("Admin session query failed error=%s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Session storage is unavailable",
        ) from exc


async def _delete_session(key: str) -> bool:
    """Fence deletion against an in-flight LLM turn and commit it atomically."""

    store = _session_store()
    try:
        async with store.session_guard(key, 15, wait_timeout=10):
            return bool(await store.delete(key))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid session key") from exc
    except TimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Session is currently busy",
        ) from exc
    except Exception as exc:
        _log.error("Admin session deletion failed error=%s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Session storage is unavailable",
        ) from exc


def _tail(path: Path, n: int) -> str:
    """读文件末尾 N 行（按块回退定位，避免把整个日志读进内存）"""

    if not path.exists():
        return "(file not found)"
    try:
        with open(path, "rb") as file:
            file.seek(0, 2)  # SEEK_END
            position = file.tell()
            buffer = b""
            while position > 0:
                step = min(_TAIL_BLOCK_BYTES, position)
                position -= step
                file.seek(position)
                buffer = file.read(step) + buffer
                if buffer.count(b"\n") > n:
                    break
            text = buffer.decode("utf-8", errors="replace")
        # Trim the leading partial line unless the read reached file start.
        if position > 0:
            first_complete = text.find("\n")
            if first_complete != -1:
                text = text[first_complete + 1 :]
        return "".join(deque(text.splitlines(keepends=True), maxlen=n))
    except OSError:
        _log.exception("Unable to tail admin log file %s", path)
        return "(log unavailable)"


@asynccontextmanager
async def _storage_unavailable(log_label: str, detail: str) -> AsyncIterator[None]:
    """Translate one storage failure into the shared 503 admin response.

    ``HTTPException`` raised inside the block is a deliberate business
    outcome (not found / conflict) and passes through untouched.
    """

    try:
        yield
    except HTTPException:
        raise
    except Exception as exc:
        _log.error("%s failed error=%s", log_label, type(exc).__name__)
        raise HTTPException(503, detail) from exc


def _file_size(path: Path) -> int:
    """Return a file size without making a failed diagnostics call fatal."""

    try:
        return path.stat().st_size
    except (FileNotFoundError, OSError):
        return 0


async def _health_snapshot() -> dict[str, Any]:
    """Collect the single readiness snapshot shared by health and admin APIs."""

    global _health_cache_expires_at, _health_cache_value
    now = time.monotonic()
    if _health_cache_value is not None and now < _health_cache_expires_at:
        return _health_cache_value
    async with _health_cache_lock:
        now = time.monotonic()
        if _health_cache_value is not None and now < _health_cache_expires_at:
            return _health_cache_value
        try:
            async with asyncio.timeout(3.0):
                snapshot = await collect_health_snapshot(config=get_driver().config, root=_ROOT)
        except TimeoutError:
            _log.error("Health snapshot exceeded its total deadline")
            snapshot = {
                "status": "not_ready",
                "ready": False,
                "uptime_seconds": 0,
                "checks": {"health_probe": {"status": "fail"}},
            }
        _health_cache_value = snapshot
        _health_cache_expires_at = time.monotonic() + _HEALTH_CACHE_TTL_SECONDS
        return snapshot


# These endpoints are deliberately outside the authenticated admin router.
# Liveness is anonymous for process supervisors.  Readiness is also anonymous
# by default, but its response is reduced to one aggregate status and cannot
# expose credentials, paths, bot IDs, scheduler errors, or user data.  Detailed
# health information is available only through Bearer-protected /api/admin/status.
health_router = APIRouter(prefix="/health")


@health_router.get("/live")
async def health_live(response: Response) -> dict[str, str]:
    """Report process liveness without touching external or local resources."""

    prevent_response_cache(response)
    return {"status": "alive"}


@health_router.get("/ready")
async def health_ready() -> JSONResponse:
    """Return only aggregate readiness; HTTP 503 asks a supervisor to hold traffic."""

    snapshot = await _health_snapshot()
    return JSONResponse(
        status_code=200 if snapshot.get("ready") is True else 503,
        content=public_readiness(snapshot),
        headers={"Cache-Control": "no-store"},
    )


router = APIRouter(
    prefix="/api/admin",
    dependencies=[Depends(prevent_response_cache), Depends(require_admin_api_token)],
)


# ─── Status ─────────────────────────────────────────────


@router.get("/status", response_model=None)
async def admin_status() -> dict[str, Any]:
    health, log_size, log_tail = await asyncio.gather(
        _health_snapshot(),
        asyncio.to_thread(_file_size, _log_file()),
        asyncio.to_thread(_tail, _log_file(), 30),
    )
    uptime = int(health["uptime_seconds"])
    return {
        "uptime_seconds": uptime,
        "uptime_display": f"{uptime // 3600}h {(uptime % 3600) // 60}m {uptime % 60}s",
        "health": health,
        "bot_log_size_bytes": log_size,
        "bot_log_tail_30": log_tail,
    }


# ─── Logs ───────────────────────────────────────────────


@router.get("/logs", response_model=None)
async def admin_logs(lines: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    return {"lines": lines, "content": await asyncio.to_thread(_tail, _log_file(), lines)}


@router.get("/logs/raw", response_class=PlainTextResponse, response_model=None)
async def admin_logs_raw(lines: int = Query(50, ge=1, le=500)) -> str:
    return await asyncio.to_thread(_tail, _log_file(), lines)


# ─── Sessions ───────────────────────────────────────────


@router.get("/sessions", response_model=None)
async def admin_sessions_list(
    limit: int = Query(100, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    summaries = await _session_summaries(limit=limit, offset=offset)
    return {"sessions": summaries, "count": len(summaries), "offset": offset}


@router.get("/sessions/{key}", response_model=None)
async def admin_session_get(key: str) -> dict[str, Any]:
    messages = await _session_messages(key)
    if not messages:
        raise HTTPException(404, f"session {key} not found")
    return {"key": key, "messages": messages}


@router.delete("/sessions/{key}", response_model=None)
async def admin_session_delete(key: str) -> dict[str, str]:
    if not await _delete_session(key):
        raise HTTPException(404, f"session {key} not found")
    return {"deleted": key}


# ─── Alerts ─────────────────────────────────────────────


@router.get("/alerts", response_model=None)
async def admin_alerts(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    owner: int | None = Query(None, ge=1),
    alert_status: str | None = Query(None, alias="status"),
) -> dict[str, Any]:
    if alert_status not in {None, "pending", "claimed", "delivered", "failed"}:
        raise HTTPException(status_code=400, detail="Invalid alert status")
    validated_status = cast(AlertStatus | None, alert_status)
    async with _storage_unavailable("Admin alert query", "Alert storage is unavailable"):
        page = await alert_repository.admin_page(
            limit=limit,
            offset=offset,
            owner_user_id=owner,
            status=validated_status,
        )

    # Lease tokens are internal fencing credentials and must never leave the
    # process, even through the authenticated administration API.
    items = [
        {
            "id": item.id,
            "owner_user_id": item.owner_user_id,
            "group_id": item.group_id,
            "market_name": item.market_name,
            "target_price_cents": item.target_price_cents,
            "direction": item.direction,
            "status": item.status,
            "attempts": item.attempts,
            "next_retry_at": item.next_retry_at,
            "last_error": item.last_error,
            "last_observed_price_cents": item.last_observed_price_cents,
            "created_at": item.created_at,
            "updated_at": item.updated_at,
            "delivered_at": item.delivered_at,
            "failed_at": item.failed_at,
            "message_id": item.message_id,
        }
        for item in page.items
    ]
    return {
        "alerts": items,
        "count": len(items),
        "total": page.total,
        "offset": page.offset,
    }


@router.post("/alerts/{alert_id}/retry", response_model=None)
async def admin_alert_retry(alert_id: str) -> dict[str, str]:
    try:
        result = await alert_repository.retry_failed(alert_id)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid alert id") from exc
    except Exception as exc:
        _log.error("Admin alert retry failed error=%s", type(exc).__name__)
        raise HTTPException(503, "Alert storage is unavailable") from exc
    if not result.applied:
        raise HTTPException(409, "Alert is not in the failed state")
    return {"retried": alert_id}


# ─── Watchlist ──────────────────────────────────────────


@router.get("/watchlist", response_model=None)
async def admin_watchlist(user: int | None = Query(None, ge=1)) -> dict[str, Any]:
    try:
        if user is not None:
            items = await watchlists.list_owner(user)
            return {
                "user": user,
                "items": [item.display_name for item in items],
                "count": len(items),
            }
        summary = await watchlists.admin_summary()
    except Exception as exc:
        _log.error("Admin watchlist query failed error=%s", type(exc).__name__)
        raise HTTPException(503, "Watchlist storage is unavailable") from exc
    return {
        "watchlists": {owner: {"count": count} for owner, count in summary.items()},
        "count": len(summary),
    }


# ─── Feature permissions ────────────────────────────────


@router.get("/permissions/features", response_model=None)
async def admin_permission_features() -> dict[str, Any]:
    return {"features": [{"name": name, "label": label} for name, label in FEATURES.items()]}


@router.get("/permissions", response_model=None)
async def admin_permissions(
    user: int | None = Query(None, ge=1),
    feature: str | None = Query(None, min_length=1, max_length=64),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    if feature is not None and feature not in FEATURES:
        raise HTTPException(status_code=400, detail="Unknown feature")
    async with _storage_unavailable("Admin permission query", "Permission storage is unavailable"):
        overrides = await permissions.list(
            user_id=user,
            feature=feature,
            limit=limit,
            offset=offset,
        )
        total = await permissions.count(user_id=user, feature=feature)
    return {
        "permissions": [
            {
                "user_id": item.user_id,
                "feature": item.feature,
                "allowed": item.allowed,
                "updated_at": item.updated_at,
            }
            for item in overrides
        ],
        "count": len(overrides),
        "total": total,
        "offset": offset,
    }


@router.put("/permissions/{user_id}/{feature}", response_model=None)
async def admin_permission_set(
    user_id: int,
    feature: str,
    allowed: bool = Body(..., embed=True),
) -> dict[str, Any]:
    if feature not in FEATURES:
        raise HTTPException(status_code=400, detail="Unknown feature")
    async with _storage_unavailable("Admin permission set", "Permission storage is unavailable"):
        await authority.set(user_id, feature, allowed)
    return {"user_id": user_id, "feature": feature, "allowed": allowed}


@router.delete("/permissions/{user_id}/{feature}", response_model=None)
async def admin_permission_clear(user_id: int, feature: str) -> dict[str, Any]:
    if feature not in FEATURES:
        raise HTTPException(status_code=400, detail="Unknown feature")
    async with _storage_unavailable("Admin permission clear", "Permission storage is unavailable"):
        removed = await authority.clear(user_id, feature)
    if not removed:
        raise HTTPException(404, "Permission override not found")
    return {"user_id": user_id, "feature": feature, "default_allowed": True}


# ─── Usage audit ────────────────────────────────────────


@router.get("/usage", response_model=None)
async def admin_usage(
    user: int | None = Query(None, ge=1),
    feature: str | None = Query(None, min_length=1, max_length=64),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    if feature is not None and feature not in FEATURES:
        raise HTTPException(status_code=400, detail="Unknown feature")
    async with _storage_unavailable("Admin usage query", "Usage storage is unavailable"):
        records = await usage_log.list(
            user_id=user,
            feature=feature,
            limit=limit,
            offset=offset,
        )
        total = await usage_log.count(user_id=user, feature=feature)
    return {
        "usage": [
            {
                "id": item.id,
                "user_id": item.user_id,
                "group_id": item.group_id,
                "feature": item.feature,
                "command": item.command,
                "input_text": item.input_text,
                "output_text": item.output_text,
                "created_at": item.created_at,
            }
            for item in records
        ],
        "count": len(records),
        "total": total,
        "offset": offset,
    }


# ─── Learning insights ─────────────────────────────────


@router.get("/learning", response_model=None)
async def admin_learning(
    user: int | None = Query(None, ge=1),
    status: str | None = Query(None, pattern="^(pending|accepted|rejected|archived)$"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    async with _storage_unavailable("Admin learning query", "Learning storage is unavailable"):
        insights, total = await learning_insights.admin_list(
            user_id=user,
            status=status,
            limit=limit,
            offset=offset,
        )
    return {
        "insights": [
            {
                "id": item.id,
                "user_id": item.user_id,
                "scope_group_id": item.scope_group_id,
                "kind": item.kind,
                "content": item.content,
                "evidence": item.evidence,
                "confidence": item.confidence,
                "status": item.status,
                "created_at": item.created_at,
                "accepted_at": item.accepted_at,
                "updated_at": item.updated_at,
                "weight": item.weight,
            }
            for item in insights
        ],
        "count": len(insights),
        "total": total,
        "offset": offset,
    }


@router.post("/learning/{insight_id}/accept", response_model=None)
async def admin_learning_accept(insight_id: int) -> dict[str, str]:
    async with _storage_unavailable("Admin learning accept", "Learning storage is unavailable"):
        rows = await learning_insights.database.read(
            "SELECT user_id, scope_group_id FROM learning_insights WHERE id = ?",
            (insight_id,),
        )
        if not rows:
            raise HTTPException(404, "Insight not found")
        applied = await learning_insights.confirm(
            insight_id,
            int(rows[0]["user_id"]),
            int(rows[0]["scope_group_id"]),
        )
    if not applied:
        raise HTTPException(409, "Insight is not in the pending state")
    return {"accepted": str(insight_id)}


@router.post("/learning/{insight_id}/reject", response_model=None)
async def admin_learning_reject(insight_id: int) -> dict[str, str]:
    async with _storage_unavailable("Admin learning reject", "Learning storage is unavailable"):
        rows = await learning_insights.database.read(
            "SELECT user_id, scope_group_id FROM learning_insights WHERE id = ?",
            (insight_id,),
        )
        if not rows:
            raise HTTPException(404, "Insight not found")
        applied = await learning_insights.reject(
            insight_id,
            int(rows[0]["user_id"]),
            int(rows[0]["scope_group_id"]),
        )
    if not applied:
        raise HTTPException(409, "Insight is not in the pending state")
    return {"rejected": str(insight_id)}


@router.delete("/learning/{insight_id}", response_model=None)
async def admin_learning_delete(insight_id: int) -> dict[str, str]:
    async with _storage_unavailable("Admin learning delete", "Learning storage is unavailable"):
        rows = await learning_insights.database.read(
            "SELECT user_id, scope_group_id, status FROM learning_insights WHERE id = ?",
            (insight_id,),
        )
        if not rows:
            raise HTTPException(404, "Insight not found")
        if str(rows[0]["status"]) != "accepted":
            raise HTTPException(409, "Only accepted insights may be deleted")
        applied = await learning_insights.delete(
            insight_id,
            int(rows[0]["user_id"]),
            int(rows[0]["scope_group_id"]),
        )
    if not applied:
        raise HTTPException(409, "Insight state changed; refresh and retry")
    return {"deleted": str(insight_id)}


@router.get("/learning/stats", response_model=None)
async def admin_learning_stats() -> dict[str, int]:
    async with _storage_unavailable("Admin learning stats", "Learning storage is unavailable"):
        return await learning_insights.stats()


# ─── CSV export ─────────────────────────────────────────


def _csv_neutralise_line(line: str) -> str:
    """Prefix one spreadsheet-unsafe line so it cannot evaluate as a formula."""

    if line.startswith(("=", "+", "-", "@")):
        return f"'{line}"
    return line


def _csv_cell(value: object) -> str:
    """Neutralise spreadsheet formula injection on every line of one cell.

    Multi-line cells are dangerous because a payload like ``ok\n=HYPERLINK(...)``
    only shows its formula character on the second row; each embedded line is
    therefore neutralised independently before ``csv.writer`` quotes the cell.
    """

    text = "" if value is None else str(value)
    if "\n" not in text:
        return _csv_neutralise_line(text)
    return "\n".join(_csv_neutralise_line(line) for line in text.split("\n"))


def _csv_response(rows: Iterable[Sequence[object]], filename: str) -> Response:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    for row in rows:
        writer.writerow([_csv_cell(cell) for cell in row])
    payload = buffer.getvalue().encode("utf-8-sig")
    return Response(
        content=payload,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/usage/export", response_model=None)
async def admin_usage_export(
    user: int | None = Query(None, ge=1),
    feature: str | None = Query(None, min_length=1, max_length=64),
    response_format: str = Query("csv", alias="format", pattern="^csv$"),
) -> Response:
    del response_format
    if feature is not None and feature not in FEATURES:
        raise HTTPException(status_code=400, detail="Unknown feature")
    async with _storage_unavailable("Admin usage export", "Usage storage is unavailable"):
        total = await usage_log.count(user_id=user, feature=feature)
        if total > _EXPORT_ROW_LIMIT:
            raise HTTPException(
                status_code=400,
                detail=f"Export is limited to {_EXPORT_ROW_LIMIT} rows; narrow the filters",
            )
        records: list[UsageRecord] = []
        offset = 0
        while offset < total:
            page = await usage_log.list(
                user_id=user,
                feature=feature,
                limit=1_000,
                offset=offset,
            )
            records.extend(page)
            offset += len(page)
            if not page:
                break
    rows: list[Sequence[object]] = [
        (
            "id",
            "user_id",
            "group_id",
            "feature",
            "command",
            "input_text",
            "output_text",
            "created_at",
        )
    ]
    rows.extend(
        (
            record.id,
            record.user_id,
            record.group_id,
            record.feature,
            record.command,
            record.input_text,
            record.output_text,
            record.created_at,
        )
        for record in records
    )
    return _csv_response(rows, "taffy-usage.csv")


@router.get("/permissions/export", response_model=None)
async def admin_permissions_export(
    response_format: str = Query("csv", alias="format", pattern="^csv$"),
) -> Response:
    del response_format
    async with _storage_unavailable("Admin permission export", "Permission storage is unavailable"):
        total = await permissions.count()
        overrides: list[PermissionOverride] = []
        offset = 0
        while offset < total:
            page = await permissions.list(limit=1_000, offset=offset)
            overrides.extend(page)
            offset += len(page)
            if not page:
                break
    rows: list[Sequence[object]] = [("user_id", "feature", "allowed", "updated_at")]
    rows.extend((item.user_id, item.feature, item.allowed, item.updated_at) for item in overrides)
    return _csv_response(rows, "taffy-permissions.csv")


# ─── Scheduler / durable delivery state ─────────────────────────────────────


@router.get("/scheduler", response_model=None)
async def admin_scheduler() -> dict[str, Any]:
    async with _storage_unavailable("Admin scheduler query", "Scheduler storage is unavailable"):
        config = await schedules.get()
    return {**config.as_dict(), "version": config.version, "updated_at": config.updated_at}


@router.get("/broadcast-deliveries", response_model=None)
async def admin_broadcast_deliveries(
    schedule_key: str | None = Query(None, min_length=1, max_length=200),
    delivery_status: str | None = Query(None, alias="status"),
    limit: int = Query(100, ge=1, le=200),
) -> dict[str, Any]:
    if delivery_status not in {None, "pending", "claimed", "delivered", "failed"}:
        raise HTTPException(status_code=400, detail="Invalid delivery status")
    validated_status = cast(DeliveryStatus | None, delivery_status)
    async with _storage_unavailable("Admin broadcast query", "Broadcast storage is unavailable"):
        deliveries = await broadcasts.list(
            schedule_key=schedule_key,
            status=validated_status,
            limit=limit,
        )

    return {
        "deliveries": [
            {
                "schedule_key": item.schedule_key,
                "group_id": item.group_id,
                "status": item.status,
                "attempts": item.attempts,
                "next_retry_at": item.next_retry_at,
                "lease_expires_at": item.lease_expires_at,
                "last_error": item.last_error,
                "payload_preview": (item.payload_text or "")[:200],
                "created_at": item.created_at,
                "updated_at": item.updated_at,
                "delivered_at": item.delivered_at,
                "failed_at": item.failed_at,
                "message_id": item.message_id,
            }
            for item in deliveries
        ],
        "count": len(deliveries),
    }


@router.post("/broadcast-deliveries/{schedule_key}/{group_id}/retry", response_model=None)
async def admin_broadcast_retry(schedule_key: str, group_id: int) -> dict[str, Any]:
    try:
        applied = await broadcasts.retry_failed(schedule_key, group_id)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "Invalid broadcast delivery identity") from exc
    except Exception as exc:
        _log.error("Admin broadcast retry failed error=%s", type(exc).__name__)
        raise HTTPException(503, "Broadcast storage is unavailable") from exc
    if not applied:
        raise HTTPException(409, "Broadcast delivery is not in the failed state")
    return {"retried": {"schedule_key": schedule_key, "group_id": group_id}}


@router.delete("/broadcast-deliveries/{schedule_key}/{group_id}", response_model=None)
async def admin_broadcast_discard(schedule_key: str, group_id: int) -> dict[str, Any]:
    try:
        applied = await broadcasts.discard_failed(schedule_key, group_id)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "Invalid broadcast delivery identity") from exc
    except Exception as exc:
        _log.error("Admin broadcast discard failed error=%s", type(exc).__name__)
        raise HTTPException(503, "Broadcast storage is unavailable") from exc
    if not applied:
        raise HTTPException(409, "Only failed broadcast deliveries may be discarded")
    return {"discarded": {"schedule_key": schedule_key, "group_id": group_id}}


# ─── Group pulse audit (read-only) ──────────────────────────────────────────


@router.get("/group-pulse", response_model=None)
async def admin_group_pulse(
    group_id: int | None = Query(None, ge=1),
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    """插话与开关的活动流（群号/行类型/原因/指纹哈希/时间，绝无消息原文）。

    该表同时承载两类行：开关行（opt-in 状态）与插话审计行。每行用 ``kind``
    标注类型；计数按类分开给出（``interjection_count`` / ``switch_count``），
    避免把 opt-in 开关操作算进「插话条数」（M-109）。注意 ``enabled`` 是开关行
    的字段，审计行的它是恒 0，读它之前先看 ``kind``。
    """

    try:
        rows = await group_pulse_log.recent(group_id=group_id, limit=limit)
        enabled_groups = await group_pulse_log.enabled_groups()
        counts = await group_pulse_log.counts(group_id=group_id)
    except Exception as exc:
        _log.error("Admin group-pulse query failed error=%s", type(exc).__name__)
        raise HTTPException(503, "Group pulse storage is unavailable") from exc
    return {
        "entries": [
            {
                "id": row.id,
                "group_id": row.group_id,
                "enabled": row.enabled,
                "reason": row.reason,
                "fingerprint": row.fingerprint,
                "created_at": row.created_at,
                "kind": row.kind,
            }
            for row in rows
        ],
        "enabled_groups": enabled_groups,
        **counts,
    }


# ─── Panel statistics (read-only aggregations) ───────────────────────────────


@router.get("/stats/summary", response_model=None)
async def admin_stats_summary() -> dict[str, Any]:
    health = await _health_snapshot()
    async with _storage_unavailable("Admin stats summary", "Statistics storage is unavailable"):
        summary = await panel_stats.dashboard_summary()
    summary["uptime_seconds"] = int(health.get("uptime_seconds") or 0)
    return summary


@router.get("/stats/usage-trend", response_model=None)
async def admin_stats_usage_trend(
    days: int = Query(30, ge=1, le=panel_stats.MAX_USAGE_TREND_DAYS),
) -> dict[str, Any]:
    async with _storage_unavailable("Admin usage trend", "Statistics storage is unavailable"):
        return await panel_stats.usage_trend(days)


@router.get("/stats/feature-pie", response_model=None)
async def admin_stats_feature_pie(
    days: int = Query(7, ge=1, le=30),
) -> dict[str, Any]:
    async with _storage_unavailable("Admin feature pie", "Statistics storage is unavailable"):
        return await panel_stats.feature_pie(days)


@router.get("/stats/market-trend", response_model=None)
async def admin_stats_market_trend(
    days: int = Query(30, ge=1, le=panel_stats.MAX_MARKET_TREND_DAYS),
) -> dict[str, Any]:
    async with _storage_unavailable("Admin market trend", "Statistics storage is unavailable"):
        return await panel_stats.market_trend(days)


@router.get("/stats/llm-usage", response_model=None)
async def admin_stats_llm_usage(
    days: int = Query(7, ge=1, le=90),
) -> dict[str, Any]:
    async with _storage_unavailable("Admin llm usage", "Statistics storage is unavailable"):
        return await panel_stats.llm_usage_trend(days)


# ─── Market feature status (read-only dashboard card) ────────────────────────


async def _market_temperature_status() -> dict[str, Any]:
    """One dashboard item for the broad temperature; never fabricates a number."""

    try:
        async with asyncio.timeout(10.0):
            broad = await get_broad_summary()
            result = await get_market_temperature(broad)
    except Exception as exc:
        _log.error("Market temperature status failed error=%s", type(exc).__name__)
        result = TemperatureUnavailable("大盘数据暂不可用")
    if isinstance(result, TemperatureUnavailable):
        return {
            "available": False,
            "display": "暂不可用",
            "reason": result.reason,
            "score": None,
            "state": None,
            "detail": None,
        }
    return {
        "available": True,
        "display": f"{result.score:+.0f}（{result.temperature_state}）",
        "reason": None,
        "score": result.score,
        "state": result.temperature_state,
        "detail": result.detail,
    }


async def _market_prediction_status() -> dict[str, Any]:
    """One dashboard item for the scenario-forecast statistics."""

    closes = dict(await index_history_store.load_closes())
    if not closes:
        return {
            "available": False,
            "display": "积累中",
            "reason": "大盘日线尚未积累",
            "horizon": None,
            "up_ratio": None,
            "count": None,
            "confidence": None,
        }
    stats = historical_scenario_probs(closes, max(closes))
    if not stats:
        return {
            "available": False,
            "display": "积累中",
            "reason": "同档位历史样本不足",
            "horizon": None,
            "up_ratio": None,
            "count": None,
            "confidence": None,
        }
    stat = max(stats, key=lambda item: item.horizon)
    return {
        "available": True,
        "display": f"{stat.horizon}日上涨率 {stat.up_ratio * 100:.0f}%｜置信度 {stat.confidence}",
        "reason": None,
        "horizon": stat.horizon,
        "up_ratio": stat.up_ratio,
        "count": stat.count,
        "confidence": stat.confidence,
    }


async def _market_ranking_status() -> dict[str, Any]:
    """One dashboard item for ranking-board unlock readiness."""

    status = await market_history.status()
    current_bucket = int(time.time()) // 3600 * 3600
    ready = MarketHistoryStore.ranking_ready(status, current_bucket=current_bucket)
    coverage = status.latest_count / status.sample_count if status.sample_count > 0 else 0.0
    target = _MIN_COLUMN_ITEMS
    stale = status.latest_bucket is None or current_bucket - status.latest_bucket > 3600
    base = f"三桶同样本 {status.ranking_overlap}/{target}｜覆盖 {coverage * 100:.0f}%"
    if ready:
        display = f"{base}｜已解锁"
    elif stale:
        display = f"{base}｜快照过期"
    elif coverage < _MIN_COVERAGE_RATIO:
        display = f"{base}｜覆盖未达标"
    else:
        display = f"{base}｜三桶未齐"
    return {
        "available": True,
        "ready": ready,
        "display": display,
        "hours_collected": status.hours_collected,
        "coverage": round(coverage, 4),
        "latest_count": status.latest_count,
        "ranking_overlap": status.ranking_overlap,
        "ranking_target": target,
    }


@router.get("/market-status", response_model=None)
async def admin_market_status() -> dict[str, Any]:
    """Aggregate read-only market feature readiness for the dashboard card."""

    temperature_task = asyncio.create_task(_market_temperature_status())
    try:
        async with _storage_unavailable("Admin market status", "Market storage is unavailable"):
            prediction = await _market_prediction_status()
            ranking = await _market_ranking_status()
    finally:
        temperature = await temperature_task
    return {"temperature": temperature, "prediction": prediction, "ranking": ranking}


# ─── Management operations ──────────────────────────────────────────────────

_BROADCAST_MAX_GROUPS = 50
_BROADCAST_MAX_TEXT = 2_000
_BROADCAST_SEND_TIMEOUT_SECONDS = 20.0
_SCHEDULER_MAX_TIMES = 24
_SCHEDULER_MAX_GROUPS = 200
_TIME_RE = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d\Z")
_EVENT_SUMMARY_MAX = 120


def _current_bot() -> Any:
    """Resolve the connected OneBot bot lazily (admin runs in the bot process)."""

    from . import scheduler

    return scheduler.connected_bot()


@router.post("/broadcast", response_model=None)
async def admin_broadcast_send(
    group_ids: Annotated[list[int], Body(embed=True)],
    payload_text: Annotated[str, Body(embed=True)],
) -> dict[str, Any]:
    cleaned_groups: list[int] = []
    for raw in group_ids:
        if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
            raise HTTPException(status_code=422, detail="group_ids must be positive integers")
        if raw not in cleaned_groups:
            cleaned_groups.append(raw)
    if not cleaned_groups:
        raise HTTPException(status_code=422, detail="group_ids must not be empty")
    if len(cleaned_groups) > _BROADCAST_MAX_GROUPS:
        raise HTTPException(
            status_code=422, detail=f"group_ids is limited to {_BROADCAST_MAX_GROUPS} groups"
        )
    text = payload_text.strip()
    if not text or len(text) > _BROADCAST_MAX_TEXT:
        raise HTTPException(
            status_code=422, detail=f"payload_text must contain 1 to {_BROADCAST_MAX_TEXT} bytes"
        )

    from nonebot.adapters.onebot.v11 import Message, MessageSegment

    bot = _current_bot()
    now = int(time.time())
    schedule_key = f"admin:{now}:{uuid.uuid4().hex[:8]}"
    lease_token = f"admin:{uuid.uuid4().hex}"
    async with _storage_unavailable("Admin broadcast claim", "Broadcast storage is unavailable"):
        claimed = await broadcasts.claim(
            schedule_key,
            cleaned_groups,
            lease_token=lease_token,
            lease_seconds=600,
            now=now,
        )
        frozen = await broadcasts.set_payload(
            schedule_key, lease_token=lease_token, payload_text=text
        )
    if frozen is None:
        raise HTTPException(status_code=409, detail="Broadcast claim was lost; retry")
    if bot is None:
        try:
            await broadcasts.record_claim_failures(
                schedule_key, lease_token=lease_token, error="BotUnavailable", failed_at=now
            )
        except Exception:
            _log.exception("Admin broadcast failure ledger write failed")
        raise HTTPException(status_code=503, detail="Bot is not connected")

    ordered_results: list[dict[str, Any] | None] = [None] * len(claimed)
    semaphore = asyncio.Semaphore(_BROADCAST_SEND_CONCURRENCY)

    async def deliver(index: int, delivery: Any) -> None:
        async with semaphore:
            try:
                async with asyncio.timeout(_BROADCAST_SEND_TIMEOUT_SECONDS):
                    await bot.send_group_msg(
                        group_id=delivery.group_id,
                        message=Message(MessageSegment.text(frozen)),
                    )
            except Exception as exc:
                error_type = type(exc).__name__
                _log.warning(
                    "Admin broadcast to group %s failed error=%s", delivery.group_id, error_type
                )
                try:
                    await broadcasts.record_failure(
                        schedule_key,
                        delivery.group_id,
                        lease_token=lease_token,
                        error=error_type,
                        failed_at=now,
                    )
                except Exception:
                    _log.exception("Admin broadcast failure ledger write failed")
                ordered_results[index] = {
                    "group_id": delivery.group_id,
                    "success": False,
                    "error": error_type,
                }
                return
            try:
                committed = await broadcasts.mark_delivered(
                    schedule_key, delivery.group_id, lease_token=lease_token, message_id=None
                )
            except Exception as exc:
                _log.error(
                    "Admin broadcast delivered but ledger write failed error=%s",
                    type(exc).__name__,
                )
                committed = False
            ordered_results[index] = (
                {"group_id": delivery.group_id, "success": bool(committed), "error": None}
                if committed
                else {"group_id": delivery.group_id, "success": False, "error": "LedgerWriteFailed"}
            )

    # Concurrent waves bounded by a semaphore plus one overall deadline keep
    # worst-case latency at minutes instead of the serial ~16-minute ceiling;
    # groups left unattempted by the deadline stay pending for manual retry.
    try:
        async with asyncio.timeout(_broadcast_total_deadline_seconds()):
            await asyncio.gather(
                *(deliver(index, delivery) for index, delivery in enumerate(claimed))
            )
    except TimeoutError:
        _log.warning("Admin broadcast hit its total deadline; remaining groups stay pending")
    results: list[dict[str, Any]] = [item for item in ordered_results if item is not None]
    # ``total`` is the number of groups this broadcast was supposed to reach, not
    # the number it managed to attempt: reporting ``len(results)`` made a run that
    # skipped groups look like a smaller-but-complete success.
    #
    # ``unconfirmed`` is deliberately NOT called "unattempted": it counts groups
    # with **no recorded terminal result**, which includes groups whose
    # ``send_group_msg`` was already called and then cancelled by the deadline — a
    # group holding a semaphore slot when the deadline fires has been contacted and
    # may well have received the message.  Their rows are still ``claimed`` under a
    # live lease, so a retry inside that lease is refused.  Operators must check the
    # group before re-sending.
    unconfirmed = len(claimed) - len(results)
    return {
        "schedule_key": schedule_key,
        "total": len(claimed),
        "succeeded": sum(1 for item in results if item["success"]),
        "unconfirmed": unconfirmed,
        "results": results,
    }


@router.put("/scheduler", response_model=None)
async def admin_scheduler_update(
    enabled: Annotated[bool | None, Body(embed=True)] = None,
    times: Annotated[list[str] | None, Body(embed=True)] = None,
    groups: Annotated[list[int] | None, Body(embed=True)] = None,
    expected_version: Annotated[int | None, Body(embed=True)] = None,
) -> dict[str, Any]:
    async with _storage_unavailable("Admin scheduler update", "Scheduler storage is unavailable"):
        current = await schedules.get()
        updated = current.as_dict()
        if enabled is not None:
            updated["enabled"] = bool(enabled)
        if times is not None:
            cleaned_times = sorted(dict.fromkeys(times))
            if not 1 <= len(cleaned_times) <= _SCHEDULER_MAX_TIMES:
                raise HTTPException(
                    status_code=422,
                    detail=f"times must contain 1 to {_SCHEDULER_MAX_TIMES} entries",
                )
            invalid = [value for value in cleaned_times if _TIME_RE.fullmatch(value) is None]
            if invalid:
                raise HTTPException(
                    status_code=422, detail="Invalid HH:MM value: " + ", ".join(invalid)
                )
            updated["times"] = cleaned_times
        if groups is not None:
            cleaned_groups = sorted(dict.fromkeys(groups))
            if not 1 <= len(cleaned_groups) <= _SCHEDULER_MAX_GROUPS:
                raise HTTPException(
                    status_code=422,
                    detail=f"groups must contain 1 to {_SCHEDULER_MAX_GROUPS} entries",
                )
            if any(
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
                for value in cleaned_groups
            ):
                raise HTTPException(status_code=422, detail="groups must be positive integers")
            updated["groups"] = cleaned_groups
        try:
            config = await schedules.replace(
                updated,
                expected_version=(
                    expected_version if expected_version is not None else current.version
                ),
            )
        except ScheduleVersionConflictError as exc:
            raise HTTPException(
                status_code=409, detail="Configuration was updated concurrently"
            ) from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="Invalid scheduler configuration") from exc
    return {**config.as_dict(), "version": config.version, "updated_at": config.updated_at}


@router.delete("/alerts/{alert_id}", response_model=None)
async def admin_alert_delete(alert_id: str) -> dict[str, Any]:
    async with _storage_unavailable("Admin alert delete", "Alert storage is unavailable"):
        try:
            removed = await alert_repository.admin_delete(alert_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid alert id") from exc
    if removed is None:
        raise HTTPException(404, "Alert not found")
    return {
        "deleted": {
            "id": removed.id,
            "owner_user_id": removed.owner_user_id,
            "market_name": removed.market_name,
            "direction": removed.direction,
        }
    }


@router.delete("/watchlist/{user_id}/{index}", response_model=None)
async def admin_watchlist_delete(user_id: int, index: int) -> dict[str, Any]:
    if user_id <= 0 or index < 1:
        raise HTTPException(status_code=422, detail="user_id and index must be positive")
    async with _storage_unavailable("Admin watchlist delete", "Watchlist storage is unavailable"):
        # remove_at validates the index inside its own transaction, so no
        # separate list_owner pre-check is needed (no pre-check could be
        # race-free anyway once concurrent deletions shift the indexes).
        removed = await watchlists.remove_at(user_id, index - 1)
    if removed is None:
        raise HTTPException(404, "Watchlist item not found")
    return {"deleted": {"user_id": user_id, "display_name": removed.display_name}}


def _event_summary(value: str) -> str:
    text = " ".join(value.split())
    return text[:_EVENT_SUMMARY_MAX]


@router.get("/events", response_model=None)
async def admin_events(
    since: int | None = Query(None, ge=0),
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    """Merge recent audit records, alert outcomes and job failures for polling."""

    async with _storage_unavailable("Admin events query", "Statistics storage is unavailable"):
        # A missing cursor means "give me the newest batch", not "match nothing".
        # The comparison is inclusive: timestamps have second resolution, so a
        # strict ``>`` would silently drop later events that land in an
        # already-polled second.  Each event carries a stable ``key`` and the
        # panel de-duplicates on it instead.
        cutoff = 0 if since is None else int(since)
        usage_rows = await usage_log.database.read(
            "SELECT id, user_id, feature, input_text, created_at FROM usage_log "
            "WHERE created_at >= ? ORDER BY created_at DESC, id DESC LIMIT ?",
            (cutoff, limit),
        )
        alert_rows = await usage_log.database.read(
            """
            SELECT id, market_name, owner_user_id, status, delivered_at, failed_at, updated_at
            FROM alerts
            WHERE updated_at >= ?
            ORDER BY updated_at DESC LIMIT ?
            """,
            (cutoff, limit),
        )
        job_rows = await usage_log.database.read(
            """
            SELECT run_key, job_name, last_error, updated_at FROM background_job_runs
            WHERE last_error IS NOT NULL AND updated_at >= ?
            ORDER BY updated_at DESC LIMIT ?
            """,
            (cutoff, limit),
        )
    events: list[dict[str, Any]] = []
    for row in usage_rows:
        events.append(
            {
                "kind": "usage",
                "key": f"usage:{row['id']}",
                "time": int(row["created_at"]),
                "summary": _event_summary(
                    f"用户 {row['user_id']} 使用 {row['feature']}：{row['input_text']}"
                ),
            }
        )
    for row in alert_rows:
        delivered_at = row["delivered_at"]
        failed_at = row["failed_at"]
        if delivered_at is not None and int(delivered_at) > cutoff:
            events.append(
                {
                    "kind": "alert_delivered",
                    "key": f"alert:{row['id']}:delivered",
                    "time": int(delivered_at),
                    "summary": _event_summary(
                        f"预警触发：{row['market_name']}（用户 {row['owner_user_id']}）"
                    ),
                }
            )
        elif failed_at is not None and int(failed_at) > cutoff:
            events.append(
                {
                    "kind": "alert_failed",
                    "key": f"alert:{row['id']}:failed",
                    "time": int(failed_at),
                    "summary": _event_summary(
                        f"预警失败：{row['market_name']}（用户 {row['owner_user_id']}）"
                    ),
                }
            )
    for row in job_rows:
        events.append(
            {
                "kind": "job_failed",
                "key": f"job:{row['run_key']}",
                "time": int(row["updated_at"]),
                "summary": _event_summary(f"后台任务 {row['job_name']} 失败：{row['last_error']}"),
            }
        )
    events.sort(key=lambda item: item["time"], reverse=True)
    bounded = events[:limit]
    return {
        "events": bounded,
        "count": len(bounded),
        "since": cutoff,
        "latest": max((item["time"] for item in bounded), default=cutoff),
    }


# ─── Register ───────────────────────────────────────────

app = get_app()
app.include_router(health_router)
app.include_router(router)

#: ``Host`` must be checked before the adapter's unsigned local webhook can be
#: reached, so the guard wraps the whole ASGI app rather than these routers.
_allowed_host_names = extra_hosts_from_env()
app.add_middleware(HostGuardMiddleware, extra_hosts=_allowed_host_names)
_log.info("Ingress Host guard active: %s", describe_guard(_allowed_host_names))

_PANEL_DIRECTORY = Path(__file__).resolve().parent / "admin_panel"
_PANEL_INDEX = _PANEL_DIRECTORY / "index.html"

# The panel is a zero-external-dependency SPA: lock its responses down with
# framing/sniffing/CSP headers so a stray script or frame cannot abuse it.
_PANEL_SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:",
}


@app.get("/admin-panel", include_in_schema=False)
async def admin_panel_page() -> FileResponse:
    """Serve the local management panel (static, no secrets; API is gated)."""

    return FileResponse(
        _PANEL_INDEX,
        headers={"Cache-Control": "no-store", **_PANEL_SECURITY_HEADERS},
    )


app.mount(
    "/admin-panel/",
    StaticFiles(directory=_PANEL_DIRECTORY, html=False),
    name="admin-panel-assets",
)


@app.middleware("http")
async def _prevent_sensitive_response_caching(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Prevent intermediaries and browsers from caching health/admin data."""

    response = await call_next(request)
    if request.url.path.startswith(("/api/admin", "/health", "/admin-panel")):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
    if request.url.path.startswith("/admin-panel"):
        for name, value in _PANEL_SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
    return response
