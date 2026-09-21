"""Configuration layering contract.

Pins the project resolution order used by ``load_runtime_settings`` /
``runtime_setting``:

    .env  <  .env.<environment>  <  explicit process environment

and proves a layered resolution can never combine an old base URL/key with a
new environment-file API format.  All values here are synthetic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.config import load_runtime_settings
from src.services.config import runtime_setting


def _write(root: Path, name: str, values: dict[str, str]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(
        "\n".join(f"{key}={value}" for key, value in values.items()) + "\n",
        encoding="utf-8",
    )


def test_env_prod_overrides_base_env(tmp_path: Path) -> None:
    _write(
        tmp_path, ".env", {"TAFFY_BASE_URL": "https://old.example/v1", "TAFFY_MODEL": "old-model"}
    )
    _write(
        tmp_path,
        ".env.prod",
        {
            "TAFFY_BASE_URL": "https://new.example/provider/v1",
            "TAFFY_MODEL": "new-model",
            "TAFFY_API_FORMAT": "openai",
        },
    )

    settings = load_runtime_settings(tmp_path, environ={})

    assert settings["taffy_base_url"] == "https://new.example/provider/v1"
    assert settings["taffy_model"] == "new-model"
    assert settings["taffy_api_format"] == "openai"


def test_explicit_process_environment_wins(tmp_path: Path) -> None:
    _write(tmp_path, ".env", {"TAFFY_BASE_URL": "https://old.example/v1"})
    _write(tmp_path, ".env.prod", {"TAFFY_BASE_URL": "https://new.example/provider/v1"})

    settings = load_runtime_settings(
        tmp_path, environ={"TAFFY_BASE_URL": "https://process.example/v1"}
    )

    assert settings["taffy_base_url"] == "https://process.example/v1"


def test_no_hybrid_old_url_or_key_with_new_format(tmp_path: Path) -> None:
    _write(
        tmp_path,
        ".env",
        {"TAFFY_BASE_URL": "https://old.example/v1", "TAFFY_API_KEY": "synthetic-old-key"},
    )
    _write(
        tmp_path,
        ".env.prod",
        {
            "TAFFY_BASE_URL": "https://new.example/provider/v1",
            "TAFFY_API_KEY": "synthetic-new-key",
            "TAFFY_API_FORMAT": "openai",
            "TAFFY_MODEL": "new-model",
        },
    )

    settings = load_runtime_settings(tmp_path, environ={})

    assert settings["taffy_base_url"] == "https://new.example/provider/v1"
    assert settings["taffy_api_format"] == "openai"
    assert settings["taffy_api_key"] == "synthetic-new-key"
    assert settings["taffy_model"] == "new-model"


def test_runtime_setting_prefers_live_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TAFFY_MODEL", "explicit-process-model")

    assert runtime_setting("TAFFY_MODEL") == "explicit-process-model"
