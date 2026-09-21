"""Startup smoke through the fixed child entry.

The child runs under the same isolation as the parent: scrubbed
environment, source-copy import roots, synthetic state paths, and the
fixed ``tests.support.childmain`` entry (no ad-hoc snippets).  It only
initialises and shuts down synthetic state — it never starts the
production server or the bot.  The child's audit is merged into the
parent log, so a swallowed denial still fails this test.
"""

from __future__ import annotations

import os
from pathlib import Path

from tests.support import isolation


def test_bot_import_and_state_lifecycle_work_outside_project_cwd(tmp_path: Path) -> None:
    root = Path(os.environ[isolation.ISOLATION_ROOT_ENV]).resolve()
    token = os.environ[isolation.ISOLATION_TOKEN_ENV]
    work = Path(os.environ[isolation.ISOLATION_WORK_ENV]).resolve()

    state_path = tmp_path / "state" / "smoke.db"
    extra_env = {
        "ENVIRONMENT": "smoke-test",
        "HOST": "127.0.0.1",
        "TAFFY_STATE_DB": str(state_path),
        "TAFFY_LOG_DIR": str(tmp_path / "logs"),
        "TAFFY_SCHEDULE_CONFIG": str(tmp_path / "missing-schedule.json"),
        "TAFFY_API_KEY": "",
        "ANTHROPIC_API_KEY": "",
        "DEEPSEEK_API_KEY": "",
        "STEAMDT_API_KEY": "",
        "ADMIN_API_TOKEN": "",
    }
    # Scrubbed builder drops credential-shaped keys even when explicitly
    # passed: the child must prove the same by reporting no secret hits.
    for key in ("TAFFY_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY", "STEAMDT_API_KEY"):
        assert key not in isolation.scrubbed_child_env(
            root=root, token=token, work=work, extra=extra_env
        )

    payload = {
        "sentinels": [],
        "env": {
            "ENVIRONMENT": "smoke-test",
            "TAFFY_STATE_DB": str(state_path),
            "TAFFY_LOG_DIR": str(tmp_path / "logs"),
            "TAFFY_SCHEDULE_CONFIG": str(tmp_path / "missing-schedule.json"),
        },
    }
    result, _child_audit = isolation.run_isolated_child(
        "smoke",
        payload,
        root=root,
        token=token,
        work=work,
        extra_env=extra_env,
        timeout=120.0,
    )

    assert result.get("isolated") is True, result
    assert result.get("token_ok") is True
    assert result.get("dotenv_hits") == []
    assert result.get("secret_env_hits") == [], result.get("secret_env_hits")
    assert Path(result["project_root"]).resolve() == root
    assert result.get("smoke_ready") is True
    # chat/alert/inventory/compare/daily/news/reddit/admin_status/scheduler/
    # watchlist/admin_api/desktop_api — keep the smoke test honest about
    # plugin count so an accidental drop fails fast.
    assert result.get("plugins") == 12
    assert Path(result["database_path"]).resolve() == state_path.resolve()
    assert state_path.is_file()
