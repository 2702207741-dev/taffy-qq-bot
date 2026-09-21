"""Regression tests for the fail-closed single-line environment text helper."""

from __future__ import annotations

import logging

import pytest

from src.services.config import PROJECT_ROOT, bounded_env_text

LOGGER = "taffy.services.config"
NAME = "TAFFY_TEST_BOUNDED_TEXT_SETTING"
INVALID = f"Ignoring invalid text setting {NAME}"


@pytest.fixture(autouse=True)
def _capture_warnings(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.delenv(NAME, raising=False)
    caplog.set_level(logging.WARNING, logger=LOGGER)


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.message for record in caplog.records if record.name == LOGGER]


def test_missing_setting_uses_default_without_warning(caplog: pytest.LogCaptureFixture) -> None:
    assert bounded_env_text(NAME, "fallback") == "fallback"
    assert _warnings(caplog) == []


@pytest.mark.parametrize("blank", ["", "   ", "\t", "\n"])
def test_blank_setting_is_unconfigured_not_malformed(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    blank: str,
) -> None:
    """A blank value means "not configured", matching ``env_secret`` semantics.

    ``.env.example`` ships blanks, so treating them as malformed made every
    documented first start print a spurious configuration warning.
    """

    monkeypatch.setenv(NAME, blank)
    assert bounded_env_text(NAME, "fallback", maximum=20) == "fallback"
    assert _warnings(caplog) == []


def test_valid_value_is_trimmed_and_accepted(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv(NAME, "  real-model  ")
    assert bounded_env_text(NAME, "fallback") == "real-model"
    assert _warnings(caplog) == []


def test_overlong_value_falls_back_with_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv(NAME, "x" * 21)
    assert bounded_env_text(NAME, "fallback", maximum=20) == "fallback"
    assert _warnings(caplog) == [INVALID]


@pytest.mark.parametrize("control", ["a\tb", "a\nb", "a\x01b"])
def test_embedded_control_character_falls_back_with_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    control: str,
) -> None:
    """Control characters inside a value are rejected.

    A NUL is deliberately not exercised here: ``os.environ`` rejects embedded
    NUL bytes on Windows, so that input cannot reach a process environment.
    """

    monkeypatch.setenv(NAME, control)
    assert bounded_env_text(NAME, "fallback", maximum=20) == "fallback"
    assert _warnings(caplog) == [INVALID]


def test_example_config_leaves_the_backup_model_unconfigured() -> None:
    """The shipped default keeps the backup LLM fail-closed and silent."""

    entries: dict[str, str] = {}
    for line in (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, _, value = stripped.partition("=")
            entries[key.strip()] = value.strip()
    assert entries["TAFFY_BACKUP_LLM_MODEL"] == ""
