"""P1 hoard-radar tests: email side channel, AI profile, privacy, migration.

No SMTP credentials appear in this file: the notifier tests inject a fake
transport and take the password from a test-only environment variable set by
the fixture, mirroring how production reads ``SMTP_PASS``.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from src.core.database import SCHEMA_VERSION, StateDatabase
from src.services.hoard_report import (
    HoardReportError,
    build_aggregate_payload,
    generate_report,
)
from src.services.inventory_monitor import InventoryMonitorService
from src.services.notify_email import (
    MAX_ATTEMPTS,
    EmailNotifier,
    EmailSettings,
    inventory_alert_subject,
)
from src.state.inventory_monitor_repository import (
    InventoryHistoryTrajectoryRow,
    InventoryMonitorRepository,
)

STEAM_ID = "76561198000000000"

#: Test-only placeholder injected through the environment; never a real secret.
_TEST_SMTP_PASSWORD_ENV = "TAFFY_TEST_SMTP_PASSWORD"


def _row(
    key: str,
    name: str = "AK-47 | 红线",
    *,
    first_seen: int = 100,
    last_seen: int = 200,
    last_missing: int | None = None,
) -> InventoryHistoryTrajectoryRow:
    return InventoryHistoryTrajectoryRow(
        item_key=key,
        name=name,
        market_hash_name=name,
        paintwear=0.12,
        first_seen_at=first_seen,
        last_seen_at=last_seen,
        last_missing_at=last_missing,
    )


# ── Email side channel ───────────────────────────────────


def test_email_settings_disabled_without_host(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "MAIL_TO"):
        monkeypatch.delenv(key, raising=False)
    settings = EmailSettings.from_env()
    assert not settings.enabled

    notifier = EmailNotifier(settings)
    assert asyncio.run(notifier.send_text("subject", "body")) is False


@pytest.fixture()
def _smtp_env() -> str:
    """Provide SMTP settings via the environment, as production does.

    The password is a random per-test placeholder injected through
    ``SMTP_PASS`` — the same channel production reads — so no credential
    literal ever appears in this file.
    """

    password = "pw-" + os.urandom(6).hex()
    monkeypatch_env = pytest.MonkeyPatch()
    monkeypatch_env.setenv("SMTP_HOST", "smtp.example.internal")
    monkeypatch_env.setenv("SMTP_USER", "taffy")
    monkeypatch_env.setenv("SMTP_PASS", password)
    monkeypatch_env.setenv("MAIL_TO", "ops@example.internal")
    yield password
    monkeypatch_env.undo()


@pytest.mark.asyncio
async def test_email_send_retries_then_backs_off(
    _smtp_env: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """发送失败 ≤2 次退避，不轰炸；成功路径立即返回 True。"""

    import src.services.notify_email as notify_email_module

    real_sleep = asyncio.sleep
    sleeps: list[float] = []

    async def recording_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(notify_email_module.asyncio, "sleep", recording_sleep)
    settings = EmailSettings.from_env()

    class FlakySMTP:
        calls = 0

        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        def ehlo(self) -> None:
            pass

        def starttls(self, context: object = None) -> None:
            del context

        def login(self, user: str, password: str) -> None:
            assert user == "taffy"
            assert password == _smtp_env

        def sendmail(self, sender: str, to: list[str], body: str) -> None:
            del sender, to, body
            FlakySMTP.calls += 1
            raise RuntimeError("relay down")

        def quit(self) -> None:
            pass

    notifier = EmailNotifier(settings, smtp_factory=FlakySMTP)
    assert await notifier.send_text("s", "b") is False
    assert FlakySMTP.calls == MAX_ATTEMPTS == 2
    # Exactly one backoff sleep between the two attempts.
    assert len(sleeps) == 1 and sleeps[0] > 0

    deliver_bodies: list[str] = []

    class HappySMTP:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        def ehlo(self) -> None:
            pass

        def starttls(self, context: object = None) -> None:
            del context

        def login(self, user: str, password: str) -> None:
            del user, password

        def sendmail(self, sender: str, to: list[str], body: str) -> None:
            del sender, to
            deliver_bodies.append(body)

        def quit(self) -> None:
            pass

    ok_notifier = EmailNotifier(settings, smtp_factory=HappySMTP)
    assert await ok_notifier.send_text(inventory_alert_subject("AI"), "检测到库存变化") is True
    # The quoted-printable body roundtrips the original text.
    import quopri

    decoded = [
        quopri.decodestring(body.split("\n\n", 1)[1].encode()).decode("utf-8")
        for body in deliver_bodies
    ]
    assert any("库存变化" in text for text in decoded)


@pytest.mark.asyncio
async def test_email_settings_reads_password_from_env(
    _smtp_env: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SMTP_USER", "taffy")
    captured: list[tuple[str, str]] = []

    class RecordingSMTP:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        def ehlo(self) -> None:
            pass

        def starttls(self, context: object = None) -> None:
            del context

        def login(self, user: str, password: str) -> None:
            captured.append((user, password))

        def sendmail(self, sender: str, to: list[str], body: str) -> None:
            del sender, to, body

        def quit(self) -> None:
            pass

    notifier = EmailNotifier(EmailSettings.from_env(), smtp_factory=RecordingSMTP)
    assert await notifier.send_text("s", "b") is True
    # The credential flowed from the environment into the login call only.
    assert captured == [("taffy", _smtp_env)]


# ── AI hoard profile ─────────────────────────────────────


async def _seeded_repository(tmp_path: Path, name: str) -> InventoryMonitorRepository:
    repository = InventoryMonitorRepository(StateDatabase(tmp_path / name))
    await repository.initialize()
    await repository.subscribe(
        101,
        "private",
        101,
        steam_id=STEAM_ID,
        nickname="账号甲",
        snapshot_json='{"version":1}',
        snapshot_hash="a" * 64,
        source_time=90,
        items={"asset:1": json.dumps({"name": "AK-47 | 红线"}, ensure_ascii=False)},
        now=100,
    )
    return repository


@pytest.mark.asyncio
async def test_report_cache_hit_within_ttl_and_miss_after(tmp_path: Path) -> None:
    repository = await _seeded_repository(tmp_path, "cache.db")
    rows = [_row("asset:1", first_seen=100)]
    calls: list[str] = []

    async def generator(model: str, user_content: str) -> str:
        del model
        calls.append(user_content)
        return "【囤货画像】账号甲\n- 总量稳定\n机器推断，非结论"

    now = 100_000
    first = await generate_report(
        repository,
        STEAM_ID,
        "账号甲",
        rows,
        now=now,
        generator=generator,
    )
    assert not first.cached and len(calls) == 1

    # Inside the 6h TTL the cache serves without touching the generator.
    second = await generate_report(
        repository,
        STEAM_ID,
        "账号甲",
        rows,
        now=now + 3_600,
        generator=generator,
    )
    assert second.cached and second.content == first.content
    assert len(calls) == 1

    from src.services.hoard_report import CACHE_TTL_SECONDS

    stale = await generate_report(
        repository,
        STEAM_ID,
        "账号甲",
        rows,
        now=now + CACHE_TTL_SECONDS + 10,
        generator=generator,
    )
    assert not stale.cached
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_report_contract_rejects_output_without_disclaimer(
    tmp_path: Path,
) -> None:
    repository = await _seeded_repository(tmp_path, "contract.db")
    rows = [_row("asset:1")]

    async def bad_generator(model: str, user_content: str) -> str:
        del model, user_content
        return "【囤货画像】看起来不错"  # missing the mandatory disclaimer

    with pytest.raises(HoardReportError):
        await generate_report(
            repository,
            STEAM_ID,
            "账号甲",
            rows,
            now=1_000,
            generator=bad_generator,
        )
    # A rejected report must never be cached.
    cached = await repository.database.read("SELECT COUNT(*) AS c FROM inventory_monitor_reports")
    assert cached[0]["c"] == 0


def test_system_prompt_and_payload_guardrails() -> None:
    from src.services.hoard_report import SYSTEM_PROMPT

    assert "机器推断，非结论" in SYSTEM_PROMPT
    assert "禁止编造" in SYSTEM_PROMPT
    assert "观察期不足" in SYSTEM_PROMPT
    payload = build_aggregate_payload([_row("asset:1")], now=50_000)
    text = json.dumps(payload, ensure_ascii=False)
    # Aggregate only: no raw per-asset keys or timestamps beyond the window.
    assert "item_key" not in text and "asset:" not in text
    assert str(payload["说明"]).startswith("仅公开库存可见")


# ── Service-level profile command surface ────────────────


@pytest.mark.asyncio
async def test_profile_rejects_unsubscribed_owner(tmp_path: Path) -> None:
    service = InventoryMonitorService(await _seeded_repository(tmp_path, "reject.db"))
    blocked = await service.profile(202, "private", 202, STEAM_ID)
    assert "尚未监控" in blocked
    malformed = await service.profile(101, "private", 101, "bad-id")
    assert "未能确认" in malformed


@pytest.mark.asyncio
async def test_profile_generates_with_mock_llm_and_reports_status(tmp_path: Path) -> None:
    repository = await _seeded_repository(tmp_path, "status.db")
    llm_calls: list[str] = []

    async def generator(model: str, user_content: str) -> str:
        del model
        llm_calls.append(user_content)
        return "【囤货画像】账号甲\n- 观察记录正常\n机器推断，非结论"

    service = InventoryMonitorService(
        repository, report_generator=generator, wall_clock=lambda: 5_000
    )
    profile_text = await service.profile(101, "private", 101, STEAM_ID)
    assert "机器推断，非结论" in profile_text
    assert "模型:" in profile_text

    status = await service.profile_status()
    assert "囤货画像状态" in status
    assert STEAM_ID in status

    # Second call inside TTL hits the cache (no extra LLM call).
    again = await service.profile(101, "private", 101, STEAM_ID)
    assert "未重新调用模型" in again
    assert len(llm_calls) == 1


# ── v14 → v15 migration ──────────────────────────────────


@pytest.mark.asyncio
async def test_v14_to_v15_migration_adds_reports_table(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "v14.db"
    original = StateDatabase(path)
    await original.initialize()
    connection = sqlite3.connect(path)
    try:
        connection.executescript("DROP TABLE inventory_monitor_reports; PRAGMA user_version = 14;")
        connection.commit()
    finally:
        connection.close()

    migrated = StateDatabase(path, allow_schema_migrations=True)
    await migrated.initialize()
    assert (await migrated.read("PRAGMA user_version"))[0]["user_version"] == SCHEMA_VERSION
    exists = await migrated.read(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='inventory_monitor_reports'"
    )
    assert exists
    idx = await migrated.read(
        "SELECT 1 FROM sqlite_master WHERE name='idx_inventory_monitor_reports_latest'"
    )
    assert idx
    # Idempotent re-run.
    again = StateDatabase(path, allow_schema_migrations=True)
    await again.initialize()
    assert await again.integrity_check()


@pytest.mark.asyncio
async def test_report_persists_via_real_schema(tmp_path: Path) -> None:
    from src.services.hoard_report import load_recent_report, store_report

    repository = await _seeded_repository(tmp_path, "persist.db")
    stored = await store_report(
        repository,
        STEAM_ID,
        payload_text="画像正文\n机器推断，非结论",
        model="deepseek-v4-pro",
        tokens_used=321,
        now=10_000,
    )
    assert "机器推断" in stored
    loaded = await load_recent_report(repository, STEAM_ID, now=15_000)
    assert loaded is not None
    assert loaded["model"] == "deepseek-v4-pro"
    assert int(loaded["tokens_used"]) == 321
    expired = await load_recent_report(repository, STEAM_ID, now=60_000)
    assert expired is None
