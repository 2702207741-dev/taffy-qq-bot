"""Sticker reactions: curated library, four-axis selection, pending review.

Phase one (V0.4) was keyword/tag matching only.  V0.5 keeps every existing
guarantee — fail-closed, never attached to market/fact/command output,
probability + cooldown + daily cap — and upgrades selection to four axes:
mood/scene (keyword map), content (tags plus optional VLM-written
descriptions), relationship (intimacy band gates the allowed vibe) and
occasion (late-night preference).  Selection stays zero-LLM: descriptions
are produced offline and stored in the manifest.

Everything is fail-closed and bounded: an empty or invalid library disables
the feature silently, and unreviewed auto-collected images stay in a
pending pool until an admin explicitly accepts them (everything learned is
confirmable, reviewable and deletable).
"""

from __future__ import annotations

import json
import logging
import secrets
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .config import PROJECT_ROOT, bounded_env_float, bounded_env_int, runtime_setting

logger = logging.getLogger("taffy.services.stickers")

_STICKER_DIR = PROJECT_ROOT / "data" / "stickers"
_PENDING_DIR_NAME = "pending"
_MANIFEST_NAME = "manifest.json"
_ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
_MAX_BYTES = 8 * 1024 * 1024
_MANIFEST_CACHE_SECONDS = 30.0
_COOLDOWN = bounded_env_float("TAFFY_STICKER_COOLDOWN_SECONDS", 600.0, 0.0, 86_400.0)
_DAILY_CAP = bounded_env_int("TAFFY_STICKER_DAILY_CAP", 6, 0, 100)
_REPLY_PROBABILITY = bounded_env_float("TAFFY_STICKER_REPLY_PROBABILITY", 0.15, 0.0, 1.0)

#: Scene labels from the persona classifier that may earn a reaction.
_ELIGIBLE_SCENES = frozenset({"闲聊", "玩笑", "吐槽", "安慰", "庆祝"})
#: Reply keywords that map to tag hints (kept deliberately small).
_KEYWORD_TAGS = {
    "谢谢": ("感谢", "开心"),
    "哈哈": ("开心", "大笑"),
    "笑死": ("大笑",),
    "厉害": ("夸奖", "得意"),
    "牛": ("夸奖",),
    "晚安": ("睡觉", "晚安"),
    "早安": ("早安",),
    "呜呜": ("安慰",),
    "难过": ("安慰",),
}

#: Vibe labels a curator may attach; the intimacy band gates them so a
#: stranger never receives an over-familiar sticker.  Unlabelled stickers
#: always stay eligible (vibe gating is additive, not restrictive).
_VIBE_NEUTRAL = frozenset({"可爱", "礼貌", "温柔", "开心", "安慰"})
_VIBE_BY_BAND = {
    "陌生": _VIBE_NEUTRAL,
    "点头之交": _VIBE_NEUTRAL | {"搞笑"},
    "熟络": _VIBE_NEUTRAL | {"搞笑", "亲昵", "得意"},
    "老友": None,  # everything goes
}
#: Occasion: late-night hours prefer calm, soft tags (offset-free UTC+8 hour).
_LATE_NIGHT_HOURS = frozenset({0, 1, 2, 3, 4, 5, 23})
_LATE_NIGHT_TAGS = frozenset({"温柔", "睡觉", "晚安", "安慰"})

#: Smart selection (four axes + description matching) is factory-off; the
#: phase-one keyword behaviour remains the default until a curated library
#: with descriptions exists.  ``smart_select_enabled`` resolves the flag
#: lazily through the runtime settings, like every other switch.


def stickers_enabled() -> bool:
    raw = runtime_setting("TAFFY_STICKERS_ENABLED")
    if raw is None:
        return False  # off until the operator curates a library
    return str(raw).strip().casefold() in {"1", "true", "yes", "on"}


def smart_select_enabled() -> bool:
    raw = runtime_setting("TAFFY_STICKER_SMART_SELECT_ENABLED")
    if raw is None:
        return False
    return str(raw).strip().casefold() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class _StickerEntry:
    path: Path
    tags: frozenset[str]
    description: str
    vibe: frozenset[str]


@dataclass(frozen=True, slots=True)
class _Manifest:
    entries: tuple[_StickerEntry, ...]


_manifest_cache: tuple[float, Path, _Manifest] | None = None


def _load_manifest() -> _Manifest:
    """Read and validate the sticker manifest with a short cache.

    The cache is keyed by the resolved library directory. Timing it only would
    let a read taken from one directory serve another directory's result for
    ``_MANIFEST_CACHE_SECONDS``, which both breaks tests that relocate the
    library and hides a real refresh whenever the configured directory changes.
    """

    global _manifest_cache
    now = time.monotonic()
    directory = _STICKER_DIR.resolve()
    cached = _manifest_cache
    if cached is not None and cached[1] == directory and now - cached[0] < _MANIFEST_CACHE_SECONDS:
        return cached[2]
    manifest_path = _STICKER_DIR / _MANIFEST_NAME
    entries: list[_StickerEntry] = []
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raw = {}
    except (OSError, ValueError) as exc:
        logger.warning("Sticker manifest unreadable error=%s", type(exc).__name__)
        raw = {}
    items = raw.get("stickers") if isinstance(raw, dict) else None
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            name = item.get("file")
            tags = item.get("tags")
            if not isinstance(name, str) or not isinstance(tags, list):
                continue
            path = (_STICKER_DIR / name).resolve()
            if path.parent != _STICKER_DIR.resolve():
                logger.warning("Sticker manifest path traversal rejected: %s", name)
                continue
            if path.suffix.casefold() not in _ALLOWED_EXTENSIONS:
                continue
            try:
                if not path.is_file() or path.stat().st_size > _MAX_BYTES:
                    continue
            except OSError:
                continue
            clean_tags = frozenset(
                tag.strip() for tag in tags if isinstance(tag, str) and tag.strip()
            )
            description = item.get("desc")
            description = description.strip()[:200] if isinstance(description, str) else ""
            vibe_raw = item.get("vibe")
            vibe = frozenset(
                tag.strip()
                for tag in (vibe_raw if isinstance(vibe_raw, list) else ())
                if isinstance(tag, str) and tag.strip()
            )
            entries.append(_StickerEntry(path, clean_tags, description, vibe))
    manifest = _Manifest(tuple(entries))
    _manifest_cache = (now, directory, manifest)
    return manifest


def _wanted_tags(scene: str | None, reply_text: str) -> set[str]:
    wanted: set[str] = set()
    if scene in _ELIGIBLE_SCENES:
        wanted.add(scene)
    for keyword, tags in _KEYWORD_TAGS.items():
        if keyword in reply_text:
            wanted.update(tags)
    return wanted


def select_sticker(scene: str | None, reply_text: str) -> Path | None:
    """Pick one sticker path for the scene/keywords, or None when nothing fits."""

    if not stickers_enabled() or _DAILY_CAP <= 0:
        return None
    manifest = _load_manifest()
    if not manifest.entries:
        return None
    wanted = _wanted_tags(scene, reply_text)
    if not wanted:
        return None
    matches = [entry for entry in manifest.entries if entry.tags & wanted]
    if not matches:
        return None
    # Cryptographically secure pick: scanner policy forbids weak randomness
    # even for non-secret shuffling, and the cost is negligible here.
    return matches[secrets.randbelow(len(matches))].path


def find_sticker_by_text(
    text: str,
    *,
    intimacy_band: str | None = None,
    hour: int | None = None,
) -> Path | None:
    """Four-axis selection for the explicit tool path (zero LLM at runtime).

    Axes: content/tags, description text, relationship vibe gating and the
    late-night occasion preference.  Returns ``None`` when nothing fits —
    the caller then simply does not send a sticker.
    """

    if not stickers_enabled() or _DAILY_CAP <= 0:
        return None
    manifest = _load_manifest()
    if not manifest.entries:
        return None
    lowered = text.casefold()
    allowed_vibes = _VIBE_BY_BAND.get(intimacy_band or "", None) if intimacy_band else None
    late_night = hour in _LATE_NIGHT_HOURS if hour is not None else False

    scored: list[tuple[int, _StickerEntry]] = []
    for entry in manifest.entries:
        if allowed_vibes is not None and entry.vibe and not (entry.vibe & allowed_vibes):
            continue
        score = 0
        wanted = _wanted_tags(None, text)
        if entry.tags & wanted:
            score += 3
        # Content axis: the sticker's own tags literally appearing in the
        # reply text (e.g. tags=["搞笑"] matching "太搞笑了").
        if any(tag.casefold() in lowered for tag in entry.tags):
            score += 2
        for keyword, tags in _KEYWORD_TAGS.items():
            if keyword in text and entry.tags.intersection(tags):
                score += 2
        if entry.description and entry.description.casefold() in lowered:
            score += 2
        elif entry.description and any(
            tag in lowered and tag in entry.description.casefold() for tag in entry.tags
        ):
            score += 1
        if late_night and entry.tags & _LATE_NIGHT_TAGS:
            score += 1
        if score > 0:
            scored.append((score, entry))
    if not scored:
        return None
    top = max(score for score, _entry in scored)
    best = [entry for score, entry in scored if score == top]
    return best[secrets.randbelow(len(best))].path


async def maybe_send_sticker(
    bot: object, event: object, *, scene: str | None, reply_text: str
) -> None:
    """Send one sticker after a persona reply if every gate passes.

    Gates: master switch, probability, per-scope cooldown, per-scope daily
    cap, non-empty validated library, eligible scene.  With the smart
    selector enabled, the automatic path also applies the relationship and
    occasion axes; otherwise the phase-one keyword rules apply.  Failures
    only log.
    """

    try:
        if not stickers_enabled() or secrets.randbelow(100) >= int(_REPLY_PROBABILITY * 100):
            return
        scope_key = scope_key_for_event(event)
        if scope_key is None:
            return
        if not cooldown_and_cap_allow(scope_key):
            return
        chosen: Path | None = None
        if smart_select_enabled():
            chosen = await _smart_pick(event, reply_text)
        if chosen is None:
            chosen = select_sticker(scene, reply_text)
        if chosen is None:
            return
        from nonebot.adapters.onebot.v11 import MessageSegment

        await bot.send(  # type: ignore[attr-defined]
            event,
            MessageSegment.image(file=f"file:///{chosen.as_posix()}"),
        )
        record_send(scope_key)
    except Exception as exc:
        logger.warning("Sticker send skipped error=%s", type(exc).__name__)


async def _smart_pick(event: object, reply_text: str) -> Path | None:
    """Four-axis pick for the automatic path; None falls back to phase one."""

    try:
        from datetime import datetime, timezone

        from src.services.relationship_service import familiarity_for_scope
        from src.services.tz import to_market_tz
        from src.state.persona_memory_repository import PersonaScope

        user_id = getattr(event, "user_id", None)
        group_id = getattr(event, "group_id", 0)
        if not isinstance(user_id, int) or user_id <= 0:
            return None
        scope = PersonaScope(user_id, group_id if isinstance(group_id, int) and group_id > 0 else 0)
        band = await familiarity_for_scope(scope)
        hour = to_market_tz(datetime.now(timezone.utc)).hour
        return find_sticker_by_text(reply_text, intimacy_band=band, hour=hour)
    except Exception as exc:
        logger.warning("Smart sticker pick failed error=%s", type(exc).__name__)
        return None


def scope_key_for_event(event: object) -> tuple[str, int] | None:
    user_id = getattr(event, "user_id", None)
    group_id = getattr(event, "group_id", None)
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        return None
    day = int(time.time()) // 86400
    return (f"g_{group_id}" if isinstance(group_id, int) else f"p_{user_id}", day)


_cooldown_state: dict[str, float] = {}
_daily_state: dict[tuple[str, int], int] = {}


def cooldown_and_cap_allow(scope_key: tuple[str, int]) -> bool:
    """Public gate for the explicit tool path (skips probability only)."""

    now = time.monotonic()
    if now - _cooldown_state.get(scope_key[0], 0.0) < _COOLDOWN:
        return False
    if _daily_state.get(scope_key, 0) >= _DAILY_CAP:
        return False
    return True


def record_send(scope_key: tuple[str, int]) -> None:
    _cooldown_state[scope_key[0]] = time.monotonic()
    _daily_state[scope_key] = _daily_state.get(scope_key, 0) + 1


# ---------------------------------------------------------------------------
# Pending pool: auto-collected images stay unreviewed until an admin
# explicitly accepts them into the curated library (confirm-first rule).
# ---------------------------------------------------------------------------


def pending_dir() -> Path:
    return _STICKER_DIR / _PENDING_DIR_NAME


def autocollect_enabled() -> bool:
    raw = runtime_setting("TAFFY_STICKER_AUTOCOLLECT_ENABLED")
    if raw is None:
        return False
    return str(raw).strip().casefold() in {"1", "true", "yes", "on"}


def record_pending_sticker(source: Path) -> Path | None:
    """Copy one candidate image into the pending pool; None when rejected."""

    try:
        if source.suffix.casefold() not in _ALLOWED_EXTENSIONS:
            return None
        if source.stat().st_size > _MAX_BYTES:
            return None
        directory = pending_dir()
        directory.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        target = directory / f"{stamp}-{source.name}"
        counter = 1
        while target.exists():
            target = directory / f"{stamp}-{counter}-{source.name}"
            counter += 1
        shutil.copyfile(source, target)
        return target
    except OSError as exc:
        logger.warning("Pending sticker copy failed error=%s", type(exc).__name__)
        return None


def pending_stickers() -> list[Path]:
    directory = pending_dir()
    try:
        return sorted(
            (
                path
                for path in directory.iterdir()
                if path.is_file() and path.suffix.casefold() in _ALLOWED_EXTENSIONS
            ),
            key=lambda path: path.name,
        )
    except OSError:
        return []


def pending_metadata_path(path: Path) -> Path:
    """Sidecar JSON path for one pending sticker's VLM description.

    Phase-two reconnaissance: auto-collected images get a machine
    description + suggested vibe tags stored *next to* the pending file
    (never inside the manifest — the curated library stays review-gated).
    """

    return path.with_suffix(path.suffix + ".desc.json")


def save_pending_metadata(path: Path, description: str, tags: list[str]) -> bool:
    """Persist the VLM description for a pending sticker; False on failure."""

    try:
        metadata = {
            "description": description[:200],
            "tags": [tag.strip() for tag in tags if isinstance(tag, str) and tag.strip()][:8],
            "stored_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        }
        pending_metadata_path(path).write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return True
    except OSError as exc:
        logger.warning("Pending sticker metadata write failed error=%s", type(exc).__name__)
        return False


def load_pending_metadata(path: Path) -> dict[str, object] | None:
    """Read the sidecar description for a pending sticker; None when absent."""

    try:
        metadata_path = pending_metadata_path(path)
        if not metadata_path.is_file():
            return None
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except (OSError, ValueError) as exc:
        logger.warning("Pending sticker metadata read failed error=%s", type(exc).__name__)
        return None


def accept_pending_sticker(index: int, tags: list[str]) -> Path | None:
    """Move a pending image into the library and register it in the manifest."""

    candidates = pending_stickers()
    if not 1 <= index <= len(candidates):
        return None
    source = candidates[index - 1]
    try:
        target = _STICKER_DIR / source.name
        counter = 1
        while target.exists():
            target = _STICKER_DIR / f"{source.stem}-{counter}{source.suffix}"
            counter += 1
        shutil.copyfile(source, target)
        source.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Pending sticker accept failed error=%s", type(exc).__name__)
        return None
    entry: dict[str, object] = {
        "file": target.name,
        "tags": [tag.strip() for tag in tags if isinstance(tag, str) and tag.strip()],
    }
    if _append_manifest_entry(entry):
        return target
    return None


def reject_pending_sticker(index: int) -> Path | None:
    candidates = pending_stickers()
    if not 1 <= index <= len(candidates):
        return None
    source = candidates[index - 1]
    try:
        source.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Pending sticker reject failed error=%s", type(exc).__name__)
        return None
    return source


def _append_manifest_entry(entry: dict[str, object]) -> bool:
    manifest_path = _STICKER_DIR / _MANIFEST_NAME
    try:
        raw: dict[str, object] = {}
        try:
            existing = manifest_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            existing = ""
        if existing.strip():
            loaded = json.loads(existing)
            if isinstance(loaded, dict):
                raw = loaded
        items = raw.get("stickers")
        if not isinstance(items, list):
            items = []
        items.append(entry)
        raw["stickers"] = items
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, ValueError) as exc:
        logger.warning("Manifest append failed error=%s", type(exc).__name__)
        return False
    _manifest_cache = None
    return True


def entries_missing_description(*, limit: int = 20) -> list[_StickerEntry]:
    """Curated entries without a description, for the offline tagging run."""

    return [entry for entry in _load_manifest().entries if not entry.description][
        : max(1, min(int(limit), 50))
    ]


def apply_description(path: Path, description: str, tags: list[str]) -> bool:
    """Persist a VLM-written description (+extra tags) into the manifest."""

    manifest_path = _STICKER_DIR / _MANIFEST_NAME
    try:
        raw: dict[str, object] = {}
        try:
            existing = manifest_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            existing = ""
        if existing.strip():
            loaded = json.loads(existing)
            if isinstance(loaded, dict):
                raw = loaded
        raw_items = raw.get("stickers")
        if not isinstance(raw_items, list):
            return False
        items: list[object] = list(raw_items)
        clean_description = description.strip()[:200]
        clean_tags = [tag.strip() for tag in tags if isinstance(tag, str) and tag.strip()]
        for item in items:
            if not isinstance(item, dict) or item.get("file") != path.name:
                continue
            raw_tags = item.get("tags")
            existing_tags = (
                {str(tag).strip() for tag in raw_tags if str(tag).strip()}
                if isinstance(raw_tags, list)
                else set()
            )
            existing_tags.update(clean_tags)
            item["tags"] = sorted(existing_tags)
            item["desc"] = clean_description
            break
        else:
            return False
        manifest_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, ValueError) as exc:
        logger.warning("Manifest description update failed error=%s", type(exc).__name__)
        return False
    _manifest_cache = None
    return True


def reset_runtime_state() -> None:
    """Test helper: clear cooldown, cap and manifest memo."""

    global _manifest_cache
    _cooldown_state.clear()
    _daily_state.clear()
    _manifest_cache = None


__all__ = [
    "accept_pending_sticker",
    "apply_description",
    "autocollect_enabled",
    "cooldown_and_cap_allow",
    "entries_missing_description",
    "find_sticker_by_text",
    "maybe_send_sticker",
    "pending_stickers",
    "record_pending_sticker",
    "record_send",
    "reject_pending_sticker",
    "reset_runtime_state",
    "select_sticker",
    "smart_select_enabled",
    "stickers_enabled",
]
