from __future__ import annotations

import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

# Import the isolation contract from the source tree under test (pytest
# inserts the project root because tests/ is a package, and the runner
# sets CWD/PYTHONPATH to the isolated copy, so this resolves inside it).
from tests.support import isolation

# Multi-factor gate: direct pytest without the controlled entry refuses to
# collect.  A bare marker variable is not enough — the token file, caller
# location, import roots, and dotenv absence are all verified, and the
# Python-layer filesystem / network / subprocess guards install here,
# before any application module is imported.
_ISOLATION_INFO = isolation.establish_parent_isolation(__file__)

# This file is imported before pytest collects test modules. Persistent service
# singletons are constructed during module import, so their paths must be
# redirected before any application module can observe the production config.
_TEST_SANDBOX = tempfile.TemporaryDirectory(prefix="taffy-pytest-")
_TEST_DATA_ROOT = Path(_TEST_SANDBOX.name).resolve()
_sandbox = _TEST_DATA_ROOT
try:
    _sandbox.relative_to(Path(_ISOLATION_INFO["root"]))
except ValueError:
    try:
        _sandbox.relative_to(Path(_ISOLATION_INFO["work"]))
    except ValueError as exc:
        raise RuntimeError(
            "isolation: pytest sandbox escaped the controlled trees: "
            f"{_sandbox} (run via scripts/run_isolated_tests.py)"
        ) from exc

os.environ.update(
    {
        "TAFFY_TEST_SANDBOX": str(_TEST_DATA_ROOT),
        "TAFFY_STATE_DB": str(_TEST_DATA_ROOT / "state" / "taffy.db"),
        "TAFFY_TELEMETRY_DB": str(_TEST_DATA_ROOT / "telemetry" / "telemetry.db"),
        "TAFFY_MARKET_CATALOG_DB": str(_TEST_DATA_ROOT / "market" / "market_catalog.db"),
        "TAFFY_MARKET_HISTORY_DB": str(_TEST_DATA_ROOT / "market" / "market_history.db"),
        # Derived-library defaults would otherwise resolve under the source
        # tree's data/ directory; pin every one of them into the sandbox.
        "TAFFY_CSQAQ_HISTORY_DB": str(_TEST_DATA_ROOT / "derived" / "csqaq_history.db"),
        "TAFFY_HOARD_INDEX_DB": str(_TEST_DATA_ROOT / "derived" / "hoard_index.db"),
        "TAFFY_CSGO_META_DB": str(_TEST_DATA_ROOT / "derived" / "csgo_meta.db"),
        "TAFFY_LOG_DIR": str(_TEST_DATA_ROOT / "logs"),
        "TAFFY_AUTOMATIC_BACKUP_DIR": str(_TEST_DATA_ROOT / "backups"),
        "TAFFY_CONFIG_BACKUP_DIR": str(_TEST_DATA_ROOT / "config-backups"),
        "TAFFY_SCHEDULE_CONFIG": str(_TEST_DATA_ROOT / "config" / "scheduler.json"),
        "TAFFY_STEAMDT_CACHE": str(_TEST_DATA_ROOT / "cache" / "steamdt_data.json"),
        # Sandbox the collection gate so a host .env cannot change test semantics.
        "TAFFY_MARKET_HISTORY_MIN_CATALOG": "1000",
        # The sitemap sync is a real network call; unit tests opt back in by
        # patching the module constant rather than the environment.
        "TAFFY_SITEMAP_SYNC_ENABLED": "false",
        # Mask outbound proxy discovery with a dead local port so a host
        # .env proxy keeps every test direct and offline; the probe fails
        # instantly and effective_proxy_url stays None.
        "HTTPS_PROXY": "http://127.0.0.1:1",
        "HTTP_PROXY": "http://127.0.0.1:1",
        "ALL_PROXY": "http://127.0.0.1:1",
        # The isolated state database starts empty for every pytest process.
        "TAFFY_ALLOW_AUTO_DB_MIGRATION": "true",
        # The desktop API switch must not inherit the host .env (F13 runtime
        # config may legitimately enable it); tests opt in per-module.
        "TAFFY_DESKTOP_API_ENABLED": "false",
        # Stickers default off; a host .env may enable them (F13 runtime
        # config may legitimately enable it); tests opt in per-module.
        "TAFFY_STICKERS_ENABLED": "false",
        # CSQAQ snapshot accumulation must not inherit the host .env: with
        # the switch on, command-path tests would write the production
        # csqaq_history.db. Tests opt in via setenv per-module.
        "TAFFY_CSQAQ_HISTORY_ENABLED": "false",
        # Same for the inspect quota switch (quota ledger + real fetches).
        "TAFFY_INSPECT_ENABLED": "false",
    }
)


@pytest.fixture(autouse=True)
def _isolation_audit_enforcer() -> object:
    """Fail any test that triggers an unclaimed boundary denial.

    Guards raise, but business code may catch the exception and degrade;
    without this fixture such a swallowed probe would still count as a
    pass.  Negative tests wrap their probes in
    ``isolation.expect_blocked`` to claim the denials they assert.
    """
    mark = isolation.audit_snapshot_size()
    yield
    unclaimed = isolation.unclaimed_blocked_since(mark)
    assert not unclaimed, (
        "unexpected isolation boundary attempts swallowed during test: "
        + "; ".join(f"{item['kind']}:{item['detail']}" for item in unclaimed[:5])
    )


@pytest.fixture(scope="session", autouse=True)
def _isolation_session_gate() -> object:
    """Session gate: unclaimed denials fail the run, not just print.

    Covers denials outside any single test window (collection swallows,
    fixture setup, late callbacks).  A failing teardown here yields a
    non-zero exit; the audit report itself must also be writable.
    """
    yield
    isolation.session_audit_check()
    work = Path(os.environ.get("TAFFY_ISOLATION_WORK", "") or Path.cwd())
    isolation.write_audit_report(work / "audit")


@pytest.fixture(autouse=True)
def _feature_permissions_default_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """Plugin tests assume every feature is allowed unless they say otherwise.

    Permission-specific tests patch the real authority back in and seed the
    repository explicitly.
    """

    from src.plugins import feature_gate

    monkeypatch.setattr(
        feature_gate,
        "authority",
        SimpleNamespace(allowed=AsyncMock(return_value=True)),
    )


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Final fail-closed gate: leftover denials or an unwritable audit
    report must change the process exit code, never just print.

    ``--collect-only`` and collection-time swallows never run the
    session-scoped fixture teardown, so this hook is the last chance to
    turn an unclaimed denial into a non-zero exit.
    """
    work = Path(os.environ.get("TAFFY_ISOLATION_WORK", "") or Path.cwd())
    pending = isolation.session_audit_failures()
    # Write the audit report first so a failing run still leaves the
    # evidence trail that names every unclaimed denial.
    try:
        report = isolation.write_audit_report(work / "audit")
    except OSError as exc:
        raise OSError(
            f"isolation audit report could not be written: {type(exc).__name__}: {exc}"
        ) from exc
    if pending:
        preview = "; ".join(f"{item['kind']}:{item['detail']}" for item in pending[:5])
        raise isolation.IsolationViolationError(
            f"isolation: {len(pending)} unclaimed boundary denials at session end: {preview}"
        )
    events = isolation.audit_events()
    blocked = sum(1 for item in events if item["outcome"] == "blocked")
    unclaimed = sum(1 for item in events if item["outcome"] == "blocked" and not item["claimed"])
    print(
        f"\n[isolation-audit] events={len(events)} blocked={blocked} "
        f"unclaimed-blocked={unclaimed} "
        f"restrictions={len(isolation.restrictions())} report={report}"
    )
    for line in isolation.restrictions():
        print(f"[isolation-restricted] {line}")
