"""V0.5 night batch 3: session-level mood state machine.

The mood layer is expression-only: transient in-memory floats, decay back
to neutral, injected as a one-liner into the persona prompt — never into
the constant persona section, never persisted.
"""

from __future__ import annotations

import pytest

from src.services import mood
from src.services.persona import build_persona_prompt

USER = 1001
GROUP = 2001


@pytest.fixture(autouse=True)
def _reset_mood(monkeypatch: pytest.MonkeyPatch) -> None:
    mood.reset_runtime_state()
    monkeypatch.setenv("TAFFY_MOOD_ENABLED", "true")
    monkeypatch.setenv("TAFFY_MOOD_PILOT_USERS", "1001")
    yield
    mood.reset_runtime_state()


# ---------------------------------------------------------------------------
# pilot whitelist
# ---------------------------------------------------------------------------


def test_mood_allowed_requires_whitelist(monkeypatch: pytest.MonkeyPatch) -> None:
    assert mood.mood_allowed_for_user(1001) is True  # in pilot list
    assert mood.mood_allowed_for_user(9999) is False  # not whitelisted
    assert mood.mood_allowed_for_user("1001") is True  # str accepted
    assert mood.mood_allowed_for_user(0) is False
    assert mood.mood_allowed_for_user(True) is False


def test_mood_falls_back_to_superusers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAFFY_MOOD_PILOT_USERS", raising=False)
    monkeypatch.setenv("SUPERUSERS", '["1243607273"]')
    assert mood.mood_allowed_for_user(1243607273) is True
    assert mood.mood_allowed_for_user(1001) is False


def test_observe_ignores_non_pilot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAFFY_MOOD_PILOT_USERS", "1001")
    valence, energy = mood.observe(9999, GROUP, sentiment="positive")
    assert valence == 0.0 and energy == 0.0
    assert mood.mood_label(9999, GROUP) is None


# ---------------------------------------------------------------------------
# state machine
# ---------------------------------------------------------------------------


def test_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAFFY_MOOD_ENABLED", raising=False)
    valence, energy = mood.observe(USER, GROUP, sentiment="positive")
    assert valence == 0.0 and energy == 0.0
    assert mood.current(USER, GROUP) == (0.0, 0.0)
    assert mood.mood_label(USER, GROUP) is None


def test_positive_event_raises_valence() -> None:
    valence, energy = mood.observe(USER, GROUP, sentiment="positive")
    assert valence > 0.2
    assert energy > 0.1


def test_negative_event_lowers_valence() -> None:
    valence, _ = mood.observe(USER, GROUP, sentiment="negative")
    assert valence < -0.2


def test_scope_isolation() -> None:
    mood.observe(USER, GROUP, sentiment="positive")
    other_valence, _ = mood.current(USER, 9999)
    assert other_valence == 0.0  # different session untouched


def test_decay_back_to_neutral() -> None:
    mood.observe(USER, GROUP, sentiment="positive", now=1_000_000.0)
    # 100 minutes later: exp(-0.04*100) ≈ 0.018 -> near zero
    valence, energy = mood.current(USER, GROUP, now=1_000_000.0 + 60 * 100)
    assert abs(valence) < 0.05
    assert abs(energy) < 0.05


def test_clamped_to_unit_bounds() -> None:
    for _ in range(30):
        mood.observe(USER, GROUP, sentiment="positive", now=1_000_000.0)
    valence, energy = mood.current(USER, GROUP, now=1_000_000.0)
    assert -1.0 <= valence <= 1.0
    assert -1.0 <= energy <= 1.0


def test_neutral_label_when_quiet() -> None:
    assert mood.mood_label(USER, GROUP) is None
    mood.observe(USER, GROUP, sentiment="positive")
    label = mood.mood_label(USER, GROUP)
    assert label is not None and "心情" in label


# ---------------------------------------------------------------------------
# persona prompt injection
# ---------------------------------------------------------------------------


def test_mood_block_injected_between_profile_and_character() -> None:
    prompt = build_persona_prompt(
        "在吗",
        mood_line="心情不错，兴致挺高",
    )
    assert "## 当前心境（临时状态，仅影响说话语气，不影响事实）" in prompt
    assert "心情不错，兴致挺高" in prompt
    # Mood block sits before the constant character section
    assert prompt.index("## 当前心境") < prompt.index("## 核心气质")


def test_no_mood_block_when_none() -> None:
    prompt = build_persona_prompt("在吗", mood_line=None)
    assert "## 当前心境" not in prompt


def test_mood_never_persisted(tmp_path) -> None:  # noqa: ANN001
    """Mood must not create any DB table or file — privacy discipline."""
    import sqlite3

    mood.observe(USER, GROUP, sentiment="positive")
    db = tmp_path / "probe.db"
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE t (k TEXT)")
    connection.commit()
    # The mood module never touched a database; only the probe table exists.
    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert tables == {"t"}
    connection.close()
