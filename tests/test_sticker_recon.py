"""V0.5 night batch 8: sticker reconnaissance phase two.

Auto-collected pending stickers receive a VLM description stored as a
sidecar file next to the pending image, so the review command can show what
the image is about before acceptance.  The curated library is untouched —
acceptance stays the only gate into the manifest.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.services import stickers
from src.services import vision as vision_module
from src.services.stickers import (
    load_pending_metadata,
    pending_metadata_path,
    save_pending_metadata,
)


def test_pending_metadata_roundtrip(tmp_path: Path) -> None:
    image = tmp_path / "meme.png"
    image.write_bytes(b"png")
    assert save_pending_metadata(image, "一只猫猫在卖萌", ["可爱", "猫"])
    metadata = load_pending_metadata(image)
    assert metadata is not None
    assert metadata["description"] == "一只猫猫在卖萌"
    assert metadata["tags"] == ["可爱", "猫"]


def test_pending_metadata_absent_returns_none(tmp_path: Path) -> None:
    image = tmp_path / "meme.png"
    image.write_bytes(b"png")
    assert load_pending_metadata(image) is None


def test_metadata_never_touches_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Recon metadata is sidecar-only; the manifest is untouched until accept."""
    library = tmp_path / "lib"
    library.mkdir(parents=True)
    monkeypatch.setattr(stickers, "_STICKER_DIR", library)
    pending = library / "pending"
    pending.mkdir()
    image = pending / "candidate.png"
    image.write_bytes(b"png")
    save_pending_metadata(image, "描述", ["标签"])
    assert not (library / "manifest.json").exists()  # manifest untouched
    assert pending_metadata_path(image).is_file()  # sidecar written


@pytest.mark.asyncio
async def test_describe_pending_skips_described(tmp_path: Path) -> None:
    image = tmp_path / "meme.png"
    image.write_bytes(b"png")
    save_pending_metadata(image, "已描述", ["旧"])
    with patch.object(
        vision_module,
        "describe_image",
        AsyncMock(return_value=("新描述", ["新"])),
    ):
        count = await vision_module.describe_pending_stickers([image], limit=5)
    assert count == 0  # already described -> skipped
    assert load_pending_metadata(image)["description"] == "已描述"


@pytest.mark.asyncio
async def test_describe_pending_writes_sidecar(tmp_path: Path) -> None:
    image = tmp_path / "meme.png"
    image.write_bytes(b"png")
    with patch.object(
        vision_module,
        "describe_image",
        AsyncMock(return_value=("一只狗在笑", ["狗", "搞笑"])),
    ):
        count = await vision_module.describe_pending_stickers([image], limit=5)
    assert count == 1
    metadata = load_pending_metadata(image)
    assert metadata is not None and metadata["description"] == "一只狗在笑"


@pytest.mark.asyncio
async def test_describe_pending_failure_fail_closed(tmp_path: Path) -> None:
    image = tmp_path / "meme.png"
    image.write_bytes(b"png")
    with patch.object(vision_module, "describe_image", AsyncMock(return_value=None)):
        count = await vision_module.describe_pending_stickers([image], limit=5)
    assert count == 0
    assert load_pending_metadata(image) is None  # nothing half-written


def test_review_command_shows_description() -> None:
    """The review listing shows sidecar descriptions when present.

    The command-layer rendering with a described pending sticker is asserted
    here through the shared helper used by test_sticker_commands.py.
    """
    import nonebot

    try:
        nonebot.get_driver()
    except ValueError:
        nonebot.init(_env_file=())

    import src.plugins.chat.sticker_commands as sticker_cmd_module

    finished: list[str] = []

    class _Finished(Exception):
        pass

    async def fake_finish(message: object) -> None:
        finished.append(str(message))
        raise _Finished

    async def fake_check(matcher: object, event: object, feature: str) -> bool:
        return True

    stickers_fake = SimpleNamespace(
        stickers_enabled=lambda: True,
        pending_stickers=lambda: [Path("meme1.png")],
        load_pending_metadata=lambda path: {"description": "一只猫猫在卖萌"},
        accept_pending_sticker=lambda index, tags: None,
        reject_pending_sticker=lambda index: None,
    )
    with (
        patch.object(sticker_cmd_module, "check_feature", fake_check),
        patch.object(sticker_cmd_module, "sticker_admin_cmd", SimpleNamespace(finish=fake_finish)),
        patch.object(sticker_cmd_module, "_superusers", lambda: {"1243607273"}),
        patch("src.services.stickers", stickers_fake, create=True),
        patch("src.services.vision", SimpleNamespace(), create=True),
    ):
        try:
            import asyncio

            asyncio.run(
                sticker_cmd_module.handle_sticker_admin(
                    None, SimpleNamespace(user_id=1243607273, get_plaintext=lambda: "塔菲贴图审核")
                )
            )
        except _Finished:
            pass
    assert finished and "一只猫猫在卖萌" in finished[0]


def test_sidecar_json_is_well_formed(tmp_path: Path) -> None:
    image = tmp_path / "m.gif"
    image.write_bytes(b"gif")
    save_pending_metadata(image, "动图", ["gif"])
    payload = json.loads(pending_metadata_path(image).read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    assert payload["stored_at"]
