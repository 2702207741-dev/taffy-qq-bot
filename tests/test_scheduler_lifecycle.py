"""Batch 7: scheduler task lifecycle decoupled from the QQ connection.

Contract:

* Application-level tasks (operations, maintenance) start after
  initialization even when QQ never connects, keep running across
  disconnects, are never rebuilt by reconnects, and stop on shutdown.
* QQ-connection-bound tasks (broadcast, alerts, inventory monitors,
  optional cache warmer, version notification) follow the connection:
  started on connect, cancelled and awaited on disconnect, never leaked
  across repeated connect events, and a late disconnect from an old
  connection must not cancel a newer connection's tasks.
* No-bot proactive send steps short-circuit without consuming one-shot
  deliveries; individual step failures stay isolated; cancellation
  propagates; shutdown stops and awaits all tasks before resources close.
* Existing feature switches keep their meaning.

Each test drives the real lifecycle functions and real asyncio Tasks;
loop bodies are replaced with controlled coroutines synchronised by
Events (no long sleeps, no real network/send/backup/learning).
"""

from __future__ import annotations

import ast
import asyncio
from types import SimpleNamespace

import nonebot
import pytest

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()

from src.plugins import scheduler


@pytest.fixture(autouse=True)
async def _clean_lifecycle() -> None:
    """Reset both registries between tests (real cancellation paths)."""
    original_bot = scheduler._bot_instance
    await scheduler._cancel_scheduler_tasks("test setup")
    await scheduler._cancel_application_tasks("test setup")
    scheduler._bot_instance = None
    yield
    await scheduler.shutdown_scheduler()
    scheduler._bot_instance = original_bot


async def _parked(events: dict[str, asyncio.Event] | None = None) -> None:
    """A controllable loop body that parks until released or cancelled."""
    if events is not None:
        events.setdefault("parked", asyncio.Event()).set()
    await asyncio.Event().wait()


def _patch_loops(
    monkeypatch: pytest.MonkeyPatch, *, warmer: bool = True
) -> dict[str, asyncio.Event]:
    """Replace every loop body with a parking coroutine; return events."""
    events: dict[str, asyncio.Event] = {}

    async def park(*_args: object, **_kwargs: object) -> None:
        await _parked(events)

    for name in (
        "_broadcast_loop",
        "_check_alerts_loop",
        "_check_inventory_monitors_loop",
        "_maintenance_loop",
        "_operations_loop",
        "_warm_volume_cache",
    ):
        monkeypatch.setattr(scheduler, name, park)
    monkeypatch.setattr(scheduler, "_cache_warmer_enabled", lambda: warmer)
    monkeypatch.setattr(scheduler, "_notify_version_update", park)
    return events


# ---------------------------------------------------------------------------
# 1. App start, QQ never connected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_application_tasks_start_without_any_qq_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_loops(monkeypatch)
    # No start_scheduler call at all — QQ never connected.
    await scheduler.start_application_tasks()
    await asyncio.sleep(0)

    assert set(scheduler._application_tasks) == {"maintenance", "operations"}
    assert scheduler._scheduler_tasks == {}  # no connection-bound tasks
    assert all(not task.done() for task in scheduler._application_tasks.values())

    await scheduler.stop_application_tasks()
    assert scheduler._application_tasks == {}
    assert all(task.done() for task in scheduler._application_tasks.values())


# ---------------------------------------------------------------------------
# 2. Connect then disconnect: connection tasks cancelled, app tasks survive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disconnect_cancels_connection_tasks_but_keeps_application_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_loops(monkeypatch)
    await scheduler.start_application_tasks()
    app_tasks = dict(scheduler._application_tasks)
    app_object = app_tasks["operations"]

    bot = object()
    await scheduler.start_scheduler(bot)  # type: ignore[arg-type]
    conn_tasks = dict(scheduler._scheduler_tasks)
    assert set(conn_tasks) == {
        "broadcast",
        "alerts",
        "inventory-monitors",
        "warm-cache",
        "version-notify",
    }
    await asyncio.sleep(0)

    await scheduler.stop_scheduler(bot)  # type: ignore[arg-type]
    # Connection tasks cancelled and awaited to completion.
    assert scheduler._scheduler_tasks == {}
    assert all(task.done() for task in conn_tasks.values())
    # Application tasks: same Task objects, still running.
    assert set(scheduler._application_tasks) == {"maintenance", "operations"}
    assert scheduler._application_tasks["operations"] is app_object
    assert not app_object.done()

    await scheduler.stop_application_tasks()
    assert app_object.done()


# ---------------------------------------------------------------------------
# 3. Repeated reconnects keep one loop per class and the same app tasks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repeated_reconnects_keep_single_loop_and_original_application_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_loops(monkeypatch)
    await scheduler.start_application_tasks()
    app_object = scheduler._application_tasks["operations"]

    bot_a = object()
    await scheduler.start_scheduler(bot_a)  # type: ignore[arg-type]
    first_conn = scheduler._scheduler_tasks["broadcast"]
    await scheduler.stop_scheduler(bot_a)  # type: ignore[arg-type]

    bot_b = object()
    await scheduler.start_scheduler(bot_b)  # type: ignore[arg-type]
    second_conn = scheduler._scheduler_tasks["broadcast"]
    await asyncio.sleep(0)

    # One broadcast loop exists, and it is a fresh task for the new bot.
    assert second_conn is not first_conn
    assert first_conn.done()
    assert not second_conn.done()
    # Application tasks were never rebuilt.
    assert scheduler._application_tasks["operations"] is app_object

    await scheduler.shutdown_scheduler()
    assert scheduler._application_tasks == {}
    assert scheduler._scheduler_tasks == {}
    assert app_object.done()


# ---------------------------------------------------------------------------
# 4. Repeated connect events do not leave old loops or orphan notify tasks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_connect_events_do_not_duplicate_loops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_loops(monkeypatch)
    bot = object()
    await scheduler.start_scheduler(bot)  # type: ignore[arg-type]
    first_broadcast = scheduler._scheduler_tasks["broadcast"]

    # The same bot connects again (duplicate event).
    await scheduler.start_scheduler(bot)  # type: ignore[arg-type]
    await asyncio.sleep(0)

    assert scheduler._scheduler_tasks["broadcast"] is first_broadcast
    assert len(scheduler._scheduler_tasks) == 5  # no duplicates
    assert "version-notify" in scheduler._scheduler_tasks

    await scheduler.shutdown_scheduler()
    assert scheduler._scheduler_tasks == {}


# ---------------------------------------------------------------------------
# 5. Late disconnect from an old connection must not cancel the new one
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_late_disconnect_of_old_bot_does_not_cancel_new_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_loops(monkeypatch)
    old_bot = object()
    new_bot = object()
    await scheduler.start_scheduler(old_bot)  # type: ignore[arg-type]
    await scheduler.stop_scheduler(old_bot)  # type: ignore[arg-type]
    await scheduler.start_scheduler(new_bot)  # type: ignore[arg-type]
    new_tasks = dict(scheduler._scheduler_tasks)
    await asyncio.sleep(0)

    # The old bot's disconnect arrives late.
    await scheduler.stop_scheduler(old_bot)  # type: ignore[arg-type]

    assert scheduler._scheduler_tasks == new_tasks  # untouched
    assert all(not task.done() for task in new_tasks.values())

    await scheduler.shutdown_scheduler()


# ---------------------------------------------------------------------------
# 6. No-bot proactive step short-circuits without consuming delivery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deliver_short_circuits_without_bot_and_keeps_delivery_claimable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A claimed delivery whose bot disconnects before the send step must
    not be sent and must not be marked delivered — the one-shot delivery
    stays claimable for a later connection."""
    from src.state.broadcast_repository import BroadcastDelivery

    delivery = BroadcastDelivery(
        schedule_key="12:00",
        group_id=123,
        status="pending",
        attempts=0,
        next_retry_at=None,
        lease_owner="lease-1",
        lease_expires_at=999,
        last_error=None,
        payload_text=None,
        created_at=1,
        updated_at=1,
        delivered_at=None,
        failed_at=None,
        message_id=None,
    )
    send_calls: list[int] = []
    delivered_calls: list[object] = []
    failure_calls: list[tuple[str, str, str]] = []

    class FakeBot:
        async def send_group_msg(self, group_id: int, **kwargs: object) -> object:
            send_calls.append(group_id)
            return {"message_id": 42}

    async def fake_mark_delivered(*args: object, **kwargs: object) -> bool:
        delivered_calls.append(args)
        return True

    async def fake_record_failure(
        delivery: BroadcastDelivery, lease_token: str, error: str, ts: int
    ) -> None:
        del lease_token, ts
        failure_calls.append((delivery.schedule_key, str(delivery.group_id), error))

    async def fake_payload_for_claim(
        schedule_key: str,
        deliveries: list[BroadcastDelivery],
        *,
        lease_token: str,
        timezone_name: str,
        now: int,
    ) -> str:
        del schedule_key, deliveries, lease_token, timezone_name, now
        # The bot disconnects while the payload is being prepared — after
        # the claim-level bot check, before the per-group send loop.
        scheduler._bot_instance = None
        return "payload"

    monkeypatch.setattr(
        scheduler, "broadcasts", SimpleNamespace(mark_delivered=fake_mark_delivered)
    )
    monkeypatch.setattr(scheduler, "_payload_for_claim", fake_payload_for_claim)
    monkeypatch.setattr(scheduler, "_record_broadcast_failure", fake_record_failure)

    bot = FakeBot()
    scheduler._bot_instance = bot  # connected when the claim is prepared
    results = await scheduler._deliver_claims(
        [delivery],
        lease_token="lease-1",
        timezone_name="Asia/Shanghai",
        timestamp=1,
    )
    assert send_calls == [], "must not send through a disconnected bot"
    assert delivered_calls == [], "must not consume the one-shot delivery"
    assert failure_calls == [("12:00", "123", "BotUnavailableError")]
    assert all(not item.success for item in results)


# ---------------------------------------------------------------------------
# 7. Single step exception does not stop later independent steps
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_operations_loop_step_failure_is_isolated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_loops(monkeypatch)
    steps: list[str] = []

    async def controlled_operations_loop() -> None:
        await scheduler._run_step(_fail_first, "first step failed")
        steps.append("second")
        await asyncio.sleep(0)
        steps.append("done")

    async def _fail_first() -> None:
        raise RuntimeError("forced step failure")

    monkeypatch.setattr(scheduler, "_operations_loop", controlled_operations_loop)
    await scheduler.start_application_tasks()
    await asyncio.sleep(0)
    # The loop ran its first step (failure logged/isolated) and continued.
    assert "second" in steps
    await scheduler.stop_application_tasks()


# ---------------------------------------------------------------------------
# 8. Cancellation propagates and the task actually exits
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_cancellation_propagates_and_task_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_loops(monkeypatch)
    exited = asyncio.Event()

    async def cancellable_loop() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            exited.set()

    monkeypatch.setattr(scheduler, "_maintenance_loop", cancellable_loop)
    await scheduler.start_application_tasks()
    task = scheduler._application_tasks["maintenance"]
    await asyncio.sleep(0)  # let the task enter its wait before cancelling
    await scheduler.stop_application_tasks()
    assert exited.is_set()
    assert task.done()
    assert task.cancelled()


# ---------------------------------------------------------------------------
# 9. Shutdown ends everything and clears registries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_ends_all_registered_tasks_and_clears_registries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_loops(monkeypatch)
    await scheduler.start_application_tasks()
    bot = object()
    await scheduler.start_scheduler(bot)  # type: ignore[arg-type]
    all_tasks = {
        **scheduler._application_tasks,
        **scheduler._scheduler_tasks,
    }
    await asyncio.sleep(0)

    await scheduler.shutdown_scheduler()
    assert scheduler._application_tasks == {}
    assert scheduler._scheduler_tasks == {}
    assert all(task.done() for task in all_tasks.values())


# ---------------------------------------------------------------------------
# 11. Entry-level orchestration reads the real bot.py registration
# ---------------------------------------------------------------------------


def _top_level_awaited_calls(func_node: ast.AsyncFunctionDef) -> list[str]:
    """Return the callee names of top-level ``await`` statements in the
    function body, in actual statement order.

    Only the body's direct statements are inspected (an ``Await`` nested
    inside a sub-block is not a phase of this orchestration), and the
    names are read from the AST in source order — never derived from an
    expected list.
    """

    awaited: list[str] = []
    for statement in func_node.body:
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Await):
            call = statement.value.value
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name):
                awaited.append(call.func.id)
    return awaited


def _extract_orchestration(bot_text: str) -> tuple[list[str], list[str]]:
    """Parse bot.py and return (startup awaited order, shutdown awaited
    order) read from the real registered callbacks' bodies in statement
    order."""
    tree = ast.parse(bot_text)

    def registered_names(attr: str) -> list[str]:
        names: list[str] = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute)
                and node.value.func.attr == attr
                and node.value.args
                and isinstance(node.value.args[0], ast.Name)
            ):
                names.append(node.value.args[0].id)
        return names

    def body_of(name: str) -> ast.AsyncFunctionDef | None:
        for node in tree.body:
            if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
                return node
        return None

    startup_regs = registered_names("on_startup")
    shutdown_regs = registered_names("on_shutdown")
    assert len(startup_regs) == 1, f"startup must be a single hook, got {startup_regs}"
    assert len(shutdown_regs) == 1, f"shutdown must be a single hook, got {shutdown_regs}"
    startup_body = body_of(startup_regs[0])
    shutdown_body = body_of(shutdown_regs[0])
    assert startup_body is not None, f"startup callback {startup_regs[0]} not defined in bot.py"
    assert shutdown_body is not None, f"shutdown callback {shutdown_regs[0]} not defined in bot.py"
    return _top_level_awaited_calls(startup_body), _top_level_awaited_calls(shutdown_body)


def _mutated_rejected(mutate: object) -> bool:
    """Run the order checker against an in-memory mutated copy of bot.py;
    return True when the checker rejects the mutation.  ``mutate`` receives
    the production text and returns the mutated copy (the production file
    is never touched and bot.py is never imported/run)."""
    from pathlib import Path

    bot_text = Path(__file__).resolve().parents[1].joinpath("bot.py").read_text(encoding="utf-8")
    mutated = mutate(bot_text)  # type: ignore[operator]
    assert isinstance(mutated, str) and mutated != bot_text, "mutation did not change the copy"
    startup_order, shutdown_order = _extract_orchestration(mutated)
    expected_startup = ["initialize_state", "start_resource_governor", "start_application_tasks"]
    expected_shutdown = ["shutdown_scheduler", "stop_resource_governor", "shutdown_state"]
    return not (startup_order == expected_startup and shutdown_order == expected_shutdown)


def test_bot_registers_single_ordered_startup_and_shutdown_sequences() -> None:
    """bot.py must register ONE startup callback and ONE shutdown callback
    whose bodies await the phases in order — not several independent hooks
    whose execution order depends on registration order.

    The awaited phase order is read from the real registered callbacks'
    AST bodies in actual statement order (see ``_top_level_awaited_calls``)
    and compared to the expected sequence; the expected list is never used
    to build the "actual" list.  In-memory counter-examples (swapped or
    removed phases) must be rejected by the same checker.
    """
    from pathlib import Path

    import src.plugins.scheduler as scheduler_module  # noqa: F401  (ensure plugin import parity)

    bot_path = Path(__file__).resolve().parents[1] / "bot.py"
    bot_text = bot_path.read_text(encoding="utf-8")

    startup_order, shutdown_order = _extract_orchestration(bot_text)
    assert startup_order == [
        "initialize_state",
        "start_resource_governor",
        "start_application_tasks",
    ], f"startup must await phases in statement order, got {startup_order}"
    assert shutdown_order == [
        "shutdown_scheduler",
        "stop_resource_governor",
        "shutdown_state",
    ], f"shutdown must await phases in statement order, got {shutdown_order}"

    # In-memory counter-examples: swapping two phases or deleting one phase
    # must make the checker reject (only the in-memory copy changes).
    def swap_phases(bot_text: str) -> str:
        # Swap the first two awaited names in both orchestrations by text
        # rewrite on the in-memory copy only.
        return bot_text.replace(
            "await initialize_state()\n    await start_resource_governor()",
            "await start_resource_governor()\n    await initialize_state()",
        )

    def delete_phase(bot_text: str) -> str:
        return bot_text.replace("    await shutdown_state()\n", "")

    assert _mutated_rejected(swap_phases), "checker must reject swapped startup phases"
    assert _mutated_rejected(delete_phase), "checker must reject a deleted phase"


# ---------------------------------------------------------------------------
# 12. Application-task start/stop share one lifecycle lock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_application_stop_and_start_are_mutually_exclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """While a stop is still awaiting old-task cancellation, a concurrent
    start must not create new tasks.  After the cleanup releases, both
    calls finish and at most one loop per category is active."""
    _patch_loops(monkeypatch)
    await scheduler.start_application_tasks()
    old_task = scheduler._application_tasks["operations"]
    old_names = set(scheduler._application_tasks)
    await asyncio.sleep(0)  # the old task is running

    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    real_cancel = scheduler._cancel_application_tasks

    async def paused_cancel(reason: str) -> None:
        cleanup_started.set()
        await release_cleanup.wait()
        await real_cancel(reason)

    monkeypatch.setattr(scheduler, "_cancel_application_tasks", paused_cancel)

    stopper = asyncio.create_task(scheduler.stop_application_tasks())
    await cleanup_started.wait()  # stop is inside its cancellation cleanup

    starter = asyncio.create_task(scheduler.start_application_tasks())
    await asyncio.sleep(0)  # give the start a chance to run (it must block)

    try:
        # While the cleanup is paused, no NEW task may be created and the
        # concurrent start must still be blocked on the lifecycle lock.
        assert set(scheduler._application_tasks) == old_names, (
            "a new task was created while the stop cleanup was still running"
        )
        assert not starter.done(), "start must block until the stop cleanup finishes"
    finally:
        release_cleanup.set()  # always release so the stopper can finish

    await asyncio.gather(stopper, starter)
    await asyncio.sleep(0)

    # Both calls finished; at most one active loop per category.
    assert len(scheduler._application_tasks) <= 2
    running = [t for t in scheduler._application_tasks.values() if not t.done()]
    assert len(running) <= 2
    # The old task was cancelled by the stop.
    assert old_task.done()

    # Final shutdown leaves nothing behind.
    await scheduler.shutdown_scheduler()
    assert scheduler._application_tasks == {}
    assert scheduler._scheduler_tasks == {}


# ---------------------------------------------------------------------------
# 10. Feature switches keep their meaning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_warmer_switch_still_controls_optional_connection_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # warmer disabled: no warm-cache task among connection-bound tasks.
    _patch_loops(monkeypatch, warmer=False)
    bot = object()
    await scheduler.start_scheduler(bot)  # type: ignore[arg-type]
    assert "warm-cache" not in scheduler._scheduler_tasks
    assert "broadcast" in scheduler._scheduler_tasks
    assert "alerts" in scheduler._scheduler_tasks
    await scheduler.shutdown_scheduler()
