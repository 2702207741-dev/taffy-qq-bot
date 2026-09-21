from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

from src.core.config import (
    KNOWN_ENV_KEYS,
    audit_env_key_names,
    audit_project_env_keys,
    load_runtime_settings,
    runtime_env_files,
)


def test_runtime_settings_use_absolute_environment_layer_and_process_override(
    tmp_path: Path,
) -> None:
    (tmp_path / ".env").write_text(
        "ENVIRONMENT=staging\nVALUE=base\nBASE_ONLY=yes\n",
        encoding="utf-8",
    )
    (tmp_path / ".env.staging").write_text(
        "VALUE=staging\nSTAGING_ONLY=yes\n",
        encoding="utf-8",
    )

    settings = load_runtime_settings(tmp_path, {"VALUE": "process"})
    env_files = runtime_env_files(tmp_path, {})

    assert settings["value"] == "process"
    assert settings["base_only"] == "yes"
    assert settings["staging_only"] == "yes"
    assert env_files == (tmp_path.resolve() / ".env", tmp_path.resolve() / ".env.staging")


def test_invalid_environment_name_cannot_escape_project_root(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("VALUE=base\n", encoding="utf-8")
    (tmp_path / ".env.prod").write_text("VALUE=prod\n", encoding="utf-8")

    settings = load_runtime_settings(tmp_path, {"ENVIRONMENT": "../../outside"})
    env_files = runtime_env_files(tmp_path, {"ENVIRONMENT": "../../outside"})

    assert settings["value"] == "prod"
    assert env_files[1] == tmp_path.resolve() / ".env.prod"


# ─── 配置防呆：启动期键名校验（warn-only，不阻断） ──────────────


def _warnings_for(settings: dict[str, str], caplog: pytest.LogCaptureFixture) -> list[str]:
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="taffy.core.config"):
        audit_env_key_names(settings)
    return [record.getMessage() for record in caplog.records]


def test_unknown_typo_key_logs_warning_with_suggestion(caplog: pytest.LogCaptureFixture) -> None:
    warnings = _warnings_for({"TAFFY_VISION_ENABLE": "true"}, caplog)

    assert len(warnings) == 1
    assert "TAFFY_VISION_ENABLE" in warnings[0]
    assert "TAFFY_VISION_ENABLED" in warnings[0]


def test_completely_unknown_key_logs_warning_without_suggestion(
    caplog: pytest.LogCaptureFixture,
) -> None:
    warnings = _warnings_for({"BOGUS_TOTALLY_UNKNOWN": "x"}, caplog)

    assert len(warnings) == 1
    assert "BOGUS_TOTALLY_UNKNOWN" in warnings[0]
    assert "拼写错误" not in warnings[0]


def test_normal_keys_produce_zero_warnings(caplog: pytest.LogCaptureFixture) -> None:
    warnings = _warnings_for(
        {"TAFFY_VISION_ENABLED": "false", "HOST": "127.0.0.1", "ADMIN_API_TOKEN": "x" * 40},
        caplog,
    )

    assert warnings == []


def test_audit_is_warn_only_and_never_blocks(caplog: pytest.LogCaptureFixture) -> None:
    # Typo keys must not raise; the call simply returns after logging.
    audit_env_key_names({"TAFFY_VISION_ENABLE": "true", "HOST": "127.0.0.1"})
    assert len(caplog.records) >= 1


def test_known_key_list_matches_env_example() -> None:
    """新增 .env 键必须同步进 KNOWN_ENV_KEYS（防漂移一致性契约）。

    Every non-commented ``KEY=value`` line in ``.env.example`` must appear in
    the canonical list; the list may additionally carry runtime-only knobs
    that have no example entry.
    """

    root = Path(__file__).resolve().parent.parent
    example_text = (root / ".env.example").read_text(encoding="utf-8")
    example_keys = {
        match.group(1)
        for match in re.finditer(r"^\s*([A-Za-z][A-Za-z0-9_]*)\s*=", example_text, re.MULTILINE)
    }
    missing = sorted(example_keys - KNOWN_ENV_KEYS)
    assert missing == [], f".env.example 键未同步进 KNOWN_ENV_KEYS: {missing}"


def test_project_env_audit_reads_dotenv_layers_and_flags_typos(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """接线契约：启动审计读 .env/.env.<env> 两个 dotenv 层（不含进程环境）。"""

    env_content = "\n".join(["HOST=127.0.0.1", "ENVIRONMENT=prod", "TAFFY_TOOLS_ENABELD=true", ""])
    (tmp_path / ".env").write_text(env_content, encoding="utf-8")
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="taffy.core.config"):
        audit_project_env_keys(tmp_path)
    messages = [record.getMessage() for record in caplog.records]
    assert any("TAFFY_TOOLS_ENABELD" in m and "TAFFY_TOOLS_ENABLED" in m for m in messages), (
        "typo key in the dotenv layer must be flagged with a suggestion"
    )
    assert any("Env key audit: 3 loaded, 1 unknown" in m for m in messages), (
        "the summary INFO line proves the audit actually ran"
    )


def test_project_env_audit_clean_dotenv_produces_zero_warnings(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    env_content = "\n".join(["HOST=127.0.0.1", "PORT=8080", ""])
    (tmp_path / ".env").write_text(env_content, encoding="utf-8")
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="taffy.core.config"):
        audit_project_env_keys(tmp_path)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == []
    assert any("Env key audit: 2 loaded, 0 unknown" in r.getMessage() for r in caplog.records)
