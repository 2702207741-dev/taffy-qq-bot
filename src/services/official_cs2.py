"""Deterministic CS2 updates and match data from structured providers."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import unescape
from typing import Any
from urllib.parse import urlsplit

import httpx

from .config import env_secret
from .http_client import lease_http_client, request_json
from .resilience import CircuitOpenError, get_circuit_breaker
from .tz import configured_timezone
from .verified import (
    DataProvenance,
    UnavailableReason,
    VerifiedResult,
    payload_sha256,
    utc_now,
)

_MARKET_TZ = configured_timezone()

logger = logging.getLogger("taffy.services.official_cs2")

_VALVE_ENDPOINT = "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
_PANDASCORE_BASE = "https://api.pandascore.co/csgo"
_VALVE_CACHE_SECONDS = 300.0
_UPCOMING_CACHE_SECONDS = 120.0
_PAST_CACHE_SECONDS = 300.0
_MAX_CLOCK_SKEW = timedelta(minutes=5)
_MAX_UPDATE_AGE = timedelta(days=90)
_MAX_MATCH_AGE = timedelta(days=14)
_SAFE_UPDATE_HOSTS = frozenset(
    {"store.steampowered.com", "steamcommunity.com", "www.counter-strike.net"}
)
_TAG_RE = re.compile(r"<[^>]+>|\[(?:/?)[a-z][^\]]*\]", re.IGNORECASE)
_SPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class ValveUpdate:
    title: str
    url: str
    published_at: datetime
    summary: str


@dataclass(frozen=True, slots=True)
class EsportsMatch:
    match_id: int
    name: str
    status: str
    begins_at: datetime
    league: str
    team_a: str
    team_b: str
    score_a: int | None
    score_b: int | None


@dataclass(frozen=True, slots=True)
class OfficialCS2Report:
    updates: VerifiedResult[tuple[ValveUpdate, ...]] | None = None
    upcoming: VerifiedResult[tuple[EsportsMatch, ...]] | None = None
    completed: VerifiedResult[tuple[EsportsMatch, ...]] | None = None


JSONRequester = Callable[..., Awaitable[dict[str, Any]]]
ListRequester = Callable[[str, Mapping[str, str], Mapping[str, str | int]], Awaitable[object]]


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or len(value) > 80:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _clean_text(value: object, maximum: int) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = unescape(_TAG_RE.sub(" ", value))
    cleaned = _SPACE_RE.sub(" ", cleaned).strip()
    return cleaned[:maximum]


def _safe_update_url(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 2048:
        return None
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _SAFE_UPDATE_HOSTS
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return value.strip()


def parse_valve_updates(
    payload: Mapping[str, object],
    *,
    fetched_at: datetime,
) -> tuple[ValveUpdate, ...]:
    appnews = payload.get("appnews")
    if not isinstance(appnews, Mapping) or appnews.get("appid") != 730:
        raise ValueError("Valve response does not belong to CS2")
    raw_items = appnews.get("newsitems")
    if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes)):
        raise ValueError("Valve news items are missing")
    updates: list[ValveUpdate] = []
    seen: set[str] = set()
    for raw in raw_items:
        if not isinstance(raw, Mapping):
            continue
        title = _clean_text(raw.get("title"), 180)
        url = _safe_update_url(raw.get("url"))
        timestamp = raw.get("date")
        if isinstance(timestamp, bool) or not isinstance(timestamp, int | float):
            continue
        try:
            published = datetime.fromtimestamp(float(timestamp), timezone.utc)
        except (OverflowError, OSError, ValueError):
            continue
        if (
            not title
            or url is None
            or published > fetched_at + _MAX_CLOCK_SKEW
            or fetched_at - published > _MAX_UPDATE_AGE
            or url in seen
        ):
            continue
        summary = _clean_text(raw.get("contents"), 240)
        updates.append(ValveUpdate(title, url, published, summary))
        seen.add(url)
    updates.sort(key=lambda item: item.published_at, reverse=True)
    if not updates:
        raise ValueError("no current, valid Valve update was returned")
    return tuple(updates[:5])


def _team_name(raw: object) -> tuple[int, str] | None:
    if not isinstance(raw, Mapping):
        return None
    opponent = raw.get("opponent")
    if not isinstance(opponent, Mapping):
        return None
    identifier = opponent.get("id")
    name = _clean_text(opponent.get("name"), 80)
    if (
        isinstance(identifier, bool)
        or not isinstance(identifier, int)
        or identifier <= 0
        or not name
    ):
        return None
    return identifier, name


def parse_pandascore_matches(
    payload: object,
    *,
    fetched_at: datetime,
    completed: bool,
) -> tuple[EsportsMatch, ...]:
    if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes)):
        raise ValueError("PandaScore match response is not a list")
    matches: list[EsportsMatch] = []
    seen: set[int] = set()
    accepted_statuses = {"finished"} if completed else {"not_started", "running"}
    for raw in payload:
        if not isinstance(raw, Mapping):
            continue
        identifier = raw.get("id")
        status = raw.get("status")
        begins_at = _parse_datetime(raw.get("begin_at") or raw.get("scheduled_at"))
        opponents = raw.get("opponents")
        if (
            isinstance(identifier, bool)
            or not isinstance(identifier, int)
            or identifier <= 0
            or identifier in seen
            or status not in accepted_statuses
            or begins_at is None
            or not isinstance(opponents, Sequence)
            or isinstance(opponents, (str, bytes))
            or len(opponents) != 2
        ):
            continue
        first, second = _team_name(opponents[0]), _team_name(opponents[1])
        if first is None or second is None or first[0] == second[0]:
            continue
        if completed and (
            begins_at > fetched_at + _MAX_CLOCK_SKEW or fetched_at - begins_at > _MAX_MATCH_AGE
        ):
            continue
        if not completed and begins_at < fetched_at - timedelta(hours=12):
            continue
        scores: dict[int, int] = {}
        raw_results = raw.get("results")
        if isinstance(raw_results, Sequence) and not isinstance(raw_results, (str, bytes)):
            for result in raw_results:
                if not isinstance(result, Mapping):
                    continue
                team_id, score = result.get("team_id"), result.get("score")
                if (
                    isinstance(team_id, int)
                    and not isinstance(team_id, bool)
                    and isinstance(score, int)
                    and not isinstance(score, bool)
                    and score >= 0
                ):
                    scores[team_id] = score
        score_a, score_b = scores.get(first[0]), scores.get(second[0])
        if completed and (score_a is None or score_b is None):
            continue
        league_raw = raw.get("league")
        league = _clean_text(
            league_raw.get("name") if isinstance(league_raw, Mapping) else None,
            80,
        )
        name = _clean_text(raw.get("name"), 120)
        if not league or not name:
            continue
        matches.append(
            EsportsMatch(
                identifier,
                name,
                str(status),
                begins_at,
                league,
                first[1],
                second[1],
                score_a,
                score_b,
            )
        )
        seen.add(identifier)
    matches.sort(key=lambda item: item.begins_at, reverse=completed)
    if not matches:
        raise ValueError("no complete PandaScore match was returned")
    return tuple(matches[:5])


async def _default_list_request(
    url: str,
    headers: Mapping[str, str],
    params: Mapping[str, str | int],
) -> object:
    async with asyncio.timeout(6.0):
        for attempt in range(2):
            try:
                async with lease_http_client() as client:
                    response = await client.get(
                        url,
                        headers={str(key): str(value) for key, value in headers.items()},
                        params=dict(params),
                        timeout=httpx.Timeout(connect=3.0, read=5.0, write=3.0, pool=3.0),
                    )
                    response.raise_for_status()
                    if len(response.content) > 1_500_000:
                        raise ValueError("PandaScore response is too large")
                    return response.json()
            except httpx.TransportError:
                if attempt > 0:
                    raise
                await asyncio.sleep(0.1)
    raise RuntimeError("PandaScore request ended unexpectedly")


class OfficialCS2Service:
    def __init__(
        self,
        *,
        valve_requester: JSONRequester = request_json,
        match_requester: ListRequester = _default_list_request,
        clock: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        pandascore_token: str | None = None,
    ) -> None:
        self._valve_requester = valve_requester
        self._match_requester = match_requester
        self._clock = clock
        self._monotonic = monotonic
        self._token_override = pandascore_token
        self._cache: dict[str, tuple[float, VerifiedResult[Any]]] = {}
        self._inflight: dict[str, asyncio.Task[VerifiedResult[Any]]] = {}
        self._lock = asyncio.Lock()
        self._valve_bulkhead = asyncio.Semaphore(2)
        self._match_bulkhead = asyncio.Semaphore(2)

    async def _cached_singleflight(
        self,
        key: str,
        ttl: float,
        loader: Callable[[], Awaitable[VerifiedResult[Any]]],
    ) -> VerifiedResult[Any]:
        now = self._monotonic()
        cached = self._cache.get(key)
        if cached is not None and now - cached[0] < ttl:
            return cached[1]
        async with self._lock:
            cached = self._cache.get(key)
            if cached is not None and now - cached[0] < ttl:
                return cached[1]
            task = self._inflight.get(key)
            if task is None:

                async def run_loader() -> VerifiedResult[Any]:
                    return await loader()

                task = asyncio.create_task(run_loader(), name=f"official-cs2:{key}")
                self._inflight[key] = task
        try:
            result = await asyncio.shield(task)
        finally:
            if task.done():
                async with self._lock:
                    if self._inflight.get(key) is task:
                        self._inflight.pop(key, None)
        if result.is_available:
            self._cache[key] = (self._monotonic(), result)
        return result

    async def valve_updates(self) -> VerifiedResult[tuple[ValveUpdate, ...]]:
        async def load() -> VerifiedResult[tuple[ValveUpdate, ...]]:
            breaker = get_circuit_breaker("valve-news")
            try:
                await breaker.acquire()
            except CircuitOpenError:
                return VerifiedResult.unavailable(UnavailableReason.CIRCUIT_OPEN)
            fetched_at = self._clock()
            try:
                async with self._valve_bulkhead:
                    payload = await self._valve_requester(
                        "GET",
                        _VALVE_ENDPOINT,
                        params={"appid": 730, "count": 5, "maxlength": 800, "format": "json"},
                        # The public command has a two-second SLO. A failed
                        # first-party endpoint must fail closed quickly; the
                        # circuit breaker handles subsequent probes.
                        attempts=1,
                        timeout=httpx.Timeout(connect=1.2, read=1.5, write=1.0, pool=1.0),
                        total_timeout=2.0,
                    )
                updates = parse_valve_updates(payload, fetched_at=fetched_at)
            except (TimeoutError, RuntimeError, httpx.TransportError, httpx.HTTPStatusError) as exc:
                await breaker.fail()
                logger.warning("Valve news unavailable error=%s", type(exc).__name__)
                return VerifiedResult.unavailable(UnavailableReason.UPSTREAM_ERROR)
            except (TypeError, ValueError) as exc:
                await breaker.cancel_probe()
                logger.warning("Valve news validation failed error=%s", type(exc).__name__)
                return VerifiedResult.unavailable(UnavailableReason.VALIDATION_FAILED)
            await breaker.succeed()
            provenance = DataProvenance(
                "Valve",
                "ISteamNews/GetNewsForApp/v2",
                updates[0].published_at,
                fetched_at,
                payload_sha256(payload),
            )
            return VerifiedResult.available(updates, provenance)

        return await self._cached_singleflight("valve", _VALVE_CACHE_SECONDS, load)

    async def matches(
        self,
        *,
        completed: bool,
    ) -> VerifiedResult[tuple[EsportsMatch, ...]]:
        token = (
            self._token_override
            if self._token_override is not None
            else env_secret("PANDASCORE_TOKEN")
        )
        if not token:
            return VerifiedResult.unavailable(UnavailableReason.NOT_CONFIGURED)
        key = "matches:past" if completed else "matches:upcoming"
        ttl = _PAST_CACHE_SECONDS if completed else _UPCOMING_CACHE_SECONDS

        async def load() -> VerifiedResult[tuple[EsportsMatch, ...]]:
            breaker = get_circuit_breaker("pandascore-matches")
            try:
                await breaker.acquire()
            except CircuitOpenError:
                return VerifiedResult.unavailable(UnavailableReason.CIRCUIT_OPEN)
            fetched_at = self._clock()
            endpoint = "past" if completed else "upcoming"
            params: dict[str, str | int] = {
                "page[size]": 5,
                "sort": "-begin_at" if completed else "begin_at",
            }
            try:
                async with self._match_bulkhead:
                    payload = await self._match_requester(
                        f"{_PANDASCORE_BASE}/matches/{endpoint}",
                        {"Authorization": f"Bearer {token}", "Accept": "application/json"},
                        params,
                    )
                matches = parse_pandascore_matches(
                    payload,
                    fetched_at=fetched_at,
                    completed=completed,
                )
            except httpx.HTTPStatusError as exc:
                await breaker.fail()
                reason = (
                    UnavailableReason.RATE_LIMITED
                    if exc.response.status_code == 429
                    else UnavailableReason.UPSTREAM_ERROR
                )
                return VerifiedResult.unavailable(reason)
            except (TimeoutError, RuntimeError, httpx.TransportError) as exc:
                await breaker.fail()
                logger.warning("PandaScore unavailable error=%s", type(exc).__name__)
                return VerifiedResult.unavailable(UnavailableReason.UPSTREAM_ERROR)
            except (TypeError, ValueError) as exc:
                await breaker.cancel_probe()
                logger.warning("PandaScore validation failed error=%s", type(exc).__name__)
                return VerifiedResult.unavailable(UnavailableReason.VALIDATION_FAILED)
            await breaker.succeed()
            provenance = DataProvenance(
                "PandaScore",
                f"/csgo/matches/{endpoint}",
                matches[0].begins_at,
                fetched_at,
                payload_sha256(payload),
            )
            return VerifiedResult.available(matches, provenance)

        return await self._cached_singleflight(key, ttl, load)

    async def report(
        self,
        *,
        include_updates: bool,
        include_matches: bool,
    ) -> OfficialCS2Report:
        tasks: list[asyncio.Task[VerifiedResult[Any]]] = []
        if include_updates:
            tasks.append(asyncio.create_task(self.valve_updates()))
        if include_matches:
            tasks.extend(
                (
                    asyncio.create_task(self.matches(completed=False)),
                    asyncio.create_task(self.matches(completed=True)),
                )
            )
        try:
            results = await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

        index = 0
        updates = results[index] if include_updates else None
        index += int(include_updates)
        upcoming = results[index] if include_matches else None
        completed = results[index + 1] if include_matches else None
        return OfficialCS2Report(updates=updates, upcoming=upcoming, completed=completed)


def _local_time(value: datetime) -> str:
    return value.astimezone(_MARKET_TZ).strftime("%m-%d %H:%M")


def format_official_report(report: OfficialCS2Report, *, asks_benefit: bool = False) -> str:
    sections: list[str] = []
    if report.updates is not None:
        lines = ["🎮 【官方更新】"]
        if report.updates.is_available:
            assert report.updates.value is not None
            for index, update in enumerate(report.updates.value[:3], 1):
                lines.append(f"{index}. {_local_time(update.published_at)}｜{update.title}")
                if update.summary:
                    lines.append(f"   {update.summary}")
                lines.append(f"   {update.url}")
        else:
            lines.append("⚠️ 官方更新暂时不可用，本次不使用网页搜索或模型补写。")
        sections.append("\n".join(lines))

    if report.upcoming is not None or report.completed is not None:
        lines = ["🏆 【赛事赛程与赛果】"]
        if (
            report.upcoming is not None
            and report.upcoming.reason is UnavailableReason.NOT_CONFIGURED
        ):
            lines.append("⚠️ 赛事接口尚未配置；当前不回退到聊天模型猜测。")
        else:
            rendered = 0
            if report.upcoming is not None and report.upcoming.is_available:
                assert report.upcoming.value is not None
                for match in report.upcoming.value[:3]:
                    lines.append(
                        f"赛程 {_local_time(match.begins_at)}｜{match.league}｜{match.team_a} vs {match.team_b}"
                    )
                    rendered += 1
            if report.completed is not None and report.completed.is_available:
                assert report.completed.value is not None
                for match in report.completed.value[:3]:
                    lines.append(
                        f"赛果 {_local_time(match.begins_at)}｜{match.league}｜{match.team_a} "
                        f"{match.score_a}:{match.score_b} {match.team_b}"
                    )
                    rendered += 1
            if not rendered:
                lines.append("⚠️ 当前没有通过队伍、时间、状态和比分完整校验的赛事数据。")
        sections.append("\n".join(lines))

    if asks_benefit:
        sections.append(
            "📌 【可能影响】\n"
            "更新或赛事消息本身不等于饰品价格利好；是否形成正面影响，仍要结合可核验的大盘、成交和具体品种观察。"
        )
    return "\n───────────────\n".join(sections)


official_cs2 = OfficialCS2Service()


__all__ = [
    "EsportsMatch",
    "OfficialCS2Report",
    "OfficialCS2Service",
    "ValveUpdate",
    "format_official_report",
    "official_cs2",
    "parse_pandascore_matches",
    "parse_valve_updates",
]
