from __future__ import annotations

import sqlite3
import time
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import nonebot
import pytest
from nonebot.exception import FinishedException

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(_env_file=())

from src.plugins.chat import proactive_commands
from src.services import proactive
from src.services.market_history import MarketHistoryStore
from src.state.runtime import database, proactive_deliveries, watchlists

_USER = 5101
_ITEM = "AK-47 | Redline (Field-Tested)"


@pytest.fixture(autouse=True)
async def prepare(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TAFFY_PROACTIVE_ENABLED", "true")
    monkeypatch.setenv("TAFFY_PROACTIVE_DAILY_CAP", "2")
    await proactive_deliveries.initialize()
    await watchlists.initialize()
    yield
    await database.write("DELETE FROM proactive_deliveries")
    await database.write("DELETE FROM watchlist_items")


def _bot() -> SimpleNamespace:
    return SimpleNamespace(send_private_msg=AsyncMock(return_value={"message_id": 1}))


def _history_stub(snapshot: dict[str, float] | None) -> SimpleNamespace:
    return SimpleNamespace(item_snapshot=AsyncMock(return_value=snapshot))


def _index_stub() -> SimpleNamespace:
    return SimpleNamespace(
        load_closes=AsyncMock(return_value=((date(2026, 8, 15), 100.0), (date(2026, 8, 16), 101.5)))
    )


def _snapshot(price: float = 123.45, change: float = -2.1) -> dict[str, float]:
    return {
        "latest_price": price,
        "data_time": float(int(time.time())),
        "week_average": price,
        "week_change": change,
        "samples": 7.0,
    }


async def _optin(user_id: int = _USER, enabled: bool = True) -> None:
    await proactive_deliveries.set_optin(user_id, enabled)


# --- morning digest ----------------------------------------------------------


@pytest.mark.asyncio
async def test_morning_push_disabled_master_switch_skips_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TAFFY_PROACTIVE_ENABLED", "false")
    await _optin()
    await watchlists.add(_USER, _ITEM)
    bot = _bot()
    with (
        patch.object(proactive, "market_history", _history_stub(_snapshot())),
        patch.object(proactive, "index_history_store", _index_stub()),
    ):
        assert await proactive.morning_push_once(bot) == 0
    bot.send_private_msg.assert_not_awaited()
    assert (
        await proactive_deliveries.count_delivered_since(_USER, since=int(time.time()) - 86400) == 0
    )


@pytest.mark.asyncio
async def test_morning_push_delivers_watchlist_and_overview() -> None:
    await _optin()
    await watchlists.add(_USER, _ITEM)
    bot = _bot()
    with (
        patch.object(proactive, "market_history", _history_stub(_snapshot())),
        patch.object(proactive, "index_history_store", _index_stub()),
    ):
        assert await proactive.morning_push_once(bot) == 1
    bot.send_private_msg.assert_awaited_once()
    call = bot.send_private_msg.await_args
    assert call.kwargs["user_id"] == _USER
    text = str(call.kwargs["message"])
    assert "早盘提醒" in text and "大盘" in text and _ITEM in text
    assert (
        await proactive_deliveries.count_delivered_since(_USER, since=int(time.time()) - 86400) == 1
    )


@pytest.mark.asyncio
async def test_morning_push_skips_when_watchlist_empty_with_reason() -> None:
    await _optin()
    bot = _bot()
    with (
        patch.object(proactive, "market_history", _history_stub(_snapshot())),
        patch.object(proactive, "index_history_store", _index_stub()),
    ):
        assert await proactive.morning_push_once(bot) == 0
    bot.send_private_msg.assert_not_awaited()
    rows, total = await proactive_deliveries.admin_list(user_id=_USER, kind="morning")
    assert total == 1 and rows[0].status == "skipped" and "自选为空" in rows[0].detail


@pytest.mark.asyncio
async def test_morning_push_skips_when_snapshots_missing_with_reason() -> None:
    await _optin()
    await watchlists.add(_USER, _ITEM)
    bot = _bot()
    with (
        patch.object(proactive, "market_history", _history_stub(None)),
        patch.object(proactive, "index_history_store", _index_stub()),
    ):
        assert await proactive.morning_push_once(bot) == 0
    bot.send_private_msg.assert_not_awaited()
    rows, _ = await proactive_deliveries.admin_list(user_id=_USER, kind="morning")
    assert rows[0].status == "skipped" and "快照" in rows[0].detail


@pytest.mark.asyncio
async def test_morning_push_respects_daily_cap() -> None:
    await _optin()
    await watchlists.add(_USER, _ITEM)
    now = int(time.time())
    day_start = now - 60
    await proactive_deliveries.record(_USER, "dip", "warm-1", "delivered", now=day_start)
    await proactive_deliveries.record(_USER, "dip", "warm-2", "delivered", now=day_start)
    bot = _bot()
    with (
        patch.object(proactive, "market_history", _history_stub(_snapshot())),
        patch.object(proactive, "index_history_store", _index_stub()),
    ):
        assert await proactive.morning_push_once(bot, now=now) == 0
    bot.send_private_msg.assert_not_awaited()


@pytest.mark.asyncio
async def test_morning_push_deduplicates_within_the_day() -> None:
    await _optin()
    await watchlists.add(_USER, _ITEM)
    bot = _bot()
    now = int(time.time())
    with (
        patch.object(proactive, "market_history", _history_stub(_snapshot())),
        patch.object(proactive, "index_history_store", _index_stub()),
    ):
        assert await proactive.morning_push_once(bot, now=now) == 1
        assert await proactive.morning_push_once(bot, now=now + 300) == 0
    assert bot.send_private_msg.await_count == 1


@pytest.mark.asyncio
async def test_morning_push_never_targets_opted_out_user() -> None:
    await _optin(enabled=False)
    await watchlists.add(_USER, _ITEM)
    bot = _bot()
    with (
        patch.object(proactive, "market_history", _history_stub(_snapshot())),
        patch.object(proactive, "index_history_store", _index_stub()),
    ):
        assert await proactive.morning_push_once(bot) == 0
    bot.send_private_msg.assert_not_awaited()


@pytest.mark.asyncio
async def test_morning_ledger_failure_does_not_abort_remaining_users() -> None:
    """A5 regression: one ledger write error must not cancel later sends."""

    await _optin(5301)
    await _optin(5302)
    await watchlists.add(5301, _ITEM)
    await watchlists.add(5302, _ITEM)
    bot = _bot()
    original_record = proactive_deliveries.record
    injected = {"done": False}

    async def flaky(user_id, kind, dedupe_key, status, **kwargs):  # type: ignore[no-untyped-def]
        if status == "delivered" and user_id == 5301 and not injected["done"]:
            injected["done"] = True
            raise RuntimeError("ledger db jitter")
        return await original_record(user_id, kind, dedupe_key, status, **kwargs)

    with (
        patch.object(proactive, "market_history", _history_stub(_snapshot())),
        patch.object(proactive, "index_history_store", _index_stub()),
        patch.object(proactive_deliveries, "record", flaky),
    ):
        assert await proactive.morning_push_once(bot) == 2
    assert bot.send_private_msg.await_count == 2


@pytest.mark.asyncio
async def test_failed_delivery_does_not_occupy_dedupe_slot() -> None:
    """A6 regression: only delivered rows suppress the day's push."""

    await proactive_deliveries.record(_USER, "morning", "20260822", "failed")
    assert not await proactive_deliveries.has_delivery(_USER, "morning", "20260822")

    await proactive_deliveries.record(_USER, "morning", "20260822", "delivered")
    assert await proactive_deliveries.has_delivery(_USER, "morning", "20260822")


# --- dip notices -------------------------------------------------------------


def _seed_history_sync(
    path: Path,
    *,
    now: int,
    latest_price: float,
    prior_min: float,
    prior_buckets: int = 30,
    stale: bool = False,
) -> None:
    latest_bucket = now // 3600 * 3600 - (7200 if stale else 0)
    rows = [(latest_bucket, _ITEM, latest_price)]
    for index in range(prior_buckets):
        rows.append(
            (
                latest_bucket - 3600 * (index + 1),
                _ITEM,
                prior_min + index * 0.5,
            )
        )
    connection = sqlite3.connect(path)
    try:
        connection.executemany(
            "INSERT INTO quote_snapshots "
            "(bucket_at, market_name, sell_price, bid_price, sell_count, bid_count, data_time) "
            "VALUES (?, ?, ?, NULL, 1, 1, ?)",
            [(bucket, name, price, bucket) for bucket, name, price in rows],
        )
        connection.commit()
    finally:
        connection.close()


async def _seed_history(
    path: Path,
    *,
    now: int,
    latest_price: float,
    prior_min: float,
    prior_buckets: int = 30,
    stale: bool = False,
) -> MarketHistoryStore:
    store = MarketHistoryStore(path=path, wall_clock=lambda: float(now))
    await store.initialize()
    _seed_history_sync(
        path,
        now=now,
        latest_price=latest_price,
        prior_min=prior_min,
        prior_buckets=prior_buckets,
        stale=stale,
    )
    return store


@pytest.mark.asyncio
async def test_item_dip_requires_fresh_sample_and_enough_history(tmp_path: Path) -> None:
    now = int(time.time())
    fresh = await _seed_history(tmp_path / "a.db", now=now, latest_price=80.0, prior_min=100.0)
    assert await fresh.item_dip(_ITEM) == (80.0, 100.0, 30)
    stale = await _seed_history(
        tmp_path / "b.db", now=now, latest_price=80.0, prior_min=100.0, stale=True
    )
    assert await stale.item_dip(_ITEM) is None
    thin = await _seed_history(
        tmp_path / "c.db", now=now, latest_price=80.0, prior_min=100.0, prior_buckets=5
    )
    assert await thin.item_dip(_ITEM) is None


@pytest.mark.asyncio
async def test_dip_scan_triggers_once_and_deduplicates(tmp_path: Path) -> None:
    now = int(time.time())
    store = await _seed_history(tmp_path / "dip.db", now=now, latest_price=80.0, prior_min=100.0)
    await _optin()
    await watchlists.add(_USER, _ITEM)
    bot = _bot()
    with patch.object(proactive, "market_history", store):
        assert await proactive.dip_scan_once(bot, now=now) == 1
        assert await proactive.dip_scan_once(bot, now=now + 300) == 0
    assert bot.send_private_msg.await_count == 1
    text = str(bot.send_private_msg.await_args.kwargs["message"])
    assert "新低" in text and "80.00" in text and "100.00" in text


@pytest.mark.asyncio
async def test_dip_scan_skips_shallow_drops(tmp_path: Path) -> None:
    now = int(time.time())
    store = await _seed_history(
        tmp_path / "shallow.db", now=now, latest_price=96.0, prior_min=100.0
    )
    await _optin()
    await watchlists.add(_USER, _ITEM)
    bot = _bot()
    with patch.object(proactive, "market_history", store):
        assert await proactive.dip_scan_once(bot, now=now) == 0
    bot.send_private_msg.assert_not_awaited()


@pytest.mark.asyncio
async def test_dip_scan_respects_daily_cap_across_kinds(tmp_path: Path) -> None:
    now = int(time.time())
    store = await _seed_history(tmp_path / "cap.db", now=now, latest_price=80.0, prior_min=100.0)
    await _optin()
    await watchlists.add(_USER, _ITEM)
    await proactive_deliveries.record(_USER, "morning", "warm-a", "delivered", now=now - 60)
    await proactive_deliveries.record(_USER, "dip", "warm-b", "delivered", now=now - 60)
    bot = _bot()
    with patch.object(proactive, "market_history", store):
        assert await proactive.dip_scan_once(bot, now=now) == 0
    bot.send_private_msg.assert_not_awaited()


@pytest.mark.asyncio
async def test_dip_scan_master_switch_off_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TAFFY_PROACTIVE_ENABLED", "false")
    now = int(time.time())
    store = await _seed_history(tmp_path / "off.db", now=now, latest_price=80.0, prior_min=100.0)
    await _optin()
    await watchlists.add(_USER, _ITEM)
    bot = _bot()
    with patch.object(proactive, "market_history", store):
        assert await proactive.dip_scan_once(bot, now=now) == 0
    bot.send_private_msg.assert_not_awaited()


@pytest.mark.asyncio
async def test_dip_send_failure_records_ledger_and_allows_retry(tmp_path: Path) -> None:
    """D12/A6 regression: failed dip sends occupy a failed row, not a dedupe slot."""

    now = int(time.time())
    store = await _seed_history(
        tmp_path / "dipfail.db", now=now, latest_price=80.0, prior_min=100.0
    )
    await _optin()
    await watchlists.add(_USER, _ITEM)
    broken_bot = SimpleNamespace(send_private_msg=AsyncMock(side_effect=RuntimeError("net down")))
    with patch.object(proactive, "market_history", store):
        assert await proactive.dip_scan_once(broken_bot, now=now) == 0

    rows, total = await proactive_deliveries.admin_list(user_id=_USER, kind="dip")
    assert total == 1 and rows[0].status == "failed"

    # The failed row must not suppress the next attempt in the same day.
    healthy_bot = _bot()
    with patch.object(proactive, "market_history", store):
        assert await proactive.dip_scan_once(healthy_bot, now=now + 300) == 1
    healthy_bot.send_private_msg.assert_awaited_once()


# --- opt-in commands ---------------------------------------------------------


def _event(user_id: int, text: str) -> SimpleNamespace:
    return SimpleNamespace(user_id=user_id, get_plaintext=lambda text=text: text)


async def _run(text: str, *, user_id: int = 5201) -> str:
    finish = AsyncMock(side_effect=FinishedException())
    with (
        patch.object(proactive_commands._RATE_LIMITER, "allow", AsyncMock(return_value=True)),
        patch.object(proactive_commands.proactive_cmd, "finish", finish),
        pytest.raises(FinishedException),
    ):
        await proactive_commands.handle_proactive(object(), _event(user_id, text))  # type: ignore[arg-type]
    return str(finish.await_args.args[0])


@pytest.mark.asyncio
async def test_optin_command_records_switch_and_mentions_disabled_master(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TAFFY_PROACTIVE_ENABLED", "false")
    reply = await _run("早盘提醒", user_id=5201)
    assert "已开启早盘提醒" in reply
    assert "未启用主动推送" in reply
    assert await proactive_deliveries.is_optedin(5201)


@pytest.mark.asyncio
async def test_optin_reply_omits_notice_when_master_enabled() -> None:
    reply = await _run("早盘提醒", user_id=5203)
    assert "已开启早盘提醒" in reply
    assert "未启用主动推送" not in reply


@pytest.mark.asyncio
async def test_optout_command_clears_switch() -> None:
    await proactive_deliveries.set_optin(5204, True)
    reply = await _run("关闭早盘提醒", user_id=5204)
    assert "已关闭早盘提醒" in reply
    assert not await proactive_deliveries.is_optedin(5204)


# --- scheduler gate (simulated 09:00 on a weekday) ---------------------------


def _jobs_stub():
    from src.state.job_repository import JobClaim

    async def claim(job_name, run_key, *, lease_seconds, now=None):
        return JobClaim(
            run_key=run_key,
            job_name=job_name,
            lease_token="token",
            lease_expires_at=int(lease_seconds),
            attempts=1,
        )

    async def mark_completed(claim, **_kwargs):
        return True

    return SimpleNamespace(claim=claim, mark_completed=mark_completed)


async def _run_tick(
    *,
    hour: int,
    weekday: int,
    enabled: bool = True,
) -> tuple[AsyncMock, AsyncMock]:
    from datetime import datetime, timedelta, timezone

    from src.plugins import scheduler

    morning = AsyncMock(return_value=1)
    dip = AsyncMock(return_value=0)
    shanghai = timezone(timedelta(hours=8), "Asia/Shanghai")
    fixed = datetime(2026, 8, 17 + weekday, hour, 0, 30, tzinfo=shanghai)
    config = SimpleNamespace(timezone="Asia/Shanghai")
    with (
        patch.object(scheduler, "load_cfg", AsyncMock(return_value=config)),
        patch.object(scheduler, "_in_configured_timezone", lambda now, tz: fixed),
        patch.object(scheduler, "_bot_instance", object()),
        patch.object(scheduler, "jobs", _jobs_stub()),
        patch.object(proactive, "proactive_enabled", return_value=enabled),
        patch.object(proactive, "morning_push_once", morning),
        patch.object(proactive, "dip_scan_once", dip),
    ):
        await scheduler._proactive_tick()
    return morning, dip


@pytest.mark.asyncio
async def test_proactive_tick_fires_morning_on_weekday_nine() -> None:
    morning, dip = await _run_tick(hour=9, weekday=0)
    morning.assert_awaited_once()
    dip.assert_awaited_once()


@pytest.mark.asyncio
async def test_proactive_tick_skips_morning_outside_hour_or_weekend() -> None:
    morning, dip = await _run_tick(hour=10, weekday=0)
    assert morning.await_count == 0
    assert dip.await_count == 1
    morning, dip = await _run_tick(hour=9, weekday=5)
    assert morning.await_count == 0
    assert dip.await_count == 1


@pytest.mark.asyncio
async def test_proactive_tick_master_switch_off_skips_all() -> None:
    morning, dip = await _run_tick(hour=9, weekday=0, enabled=False)
    assert morning.await_count == 0
    assert dip.await_count == 0
