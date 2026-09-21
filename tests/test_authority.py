from __future__ import annotations

from pathlib import Path

import pytest

from src.core.database import StateDatabase
from src.services.authority import DENIED_MESSAGE, FEATURES, FeatureAuthority
from src.state.permission_repository import PermissionRepository


async def _repository(tmp_path: Path) -> tuple[StateDatabase, PermissionRepository]:
    database = StateDatabase(tmp_path / "state.db")
    await database.initialize()
    return database, PermissionRepository(database)


@pytest.mark.asyncio
async def test_missing_override_means_default_allowed(tmp_path: Path) -> None:
    _database, repository = await _repository(tmp_path)
    assert await repository.get(101, "watchlist") is None


@pytest.mark.asyncio
async def test_set_get_clear_round_trip(tmp_path: Path) -> None:
    _database, repository = await _repository(tmp_path)
    override = await repository.set(101, "alert", False)
    assert override.user_id == 101
    assert override.feature == "alert"
    assert override.allowed is False
    assert await repository.get(101, "alert") is False
    assert await repository.set(101, "alert", True)
    assert await repository.get(101, "alert") is True
    assert await repository.clear(101, "alert") is True
    assert await repository.get(101, "alert") is None
    assert await repository.clear(101, "alert") is False


@pytest.mark.asyncio
async def test_list_and_count_filter_by_user_and_feature(tmp_path: Path) -> None:
    _database, repository = await _repository(tmp_path)
    await repository.set(101, "market", False)
    await repository.set(101, "alert", False)
    await repository.set(202, "market", False)

    assert await repository.count() == 3
    assert await repository.count(user_id=101) == 2
    assert await repository.count(feature="market") == 2

    for_user = await repository.list(user_id=101)
    assert {item.feature for item in for_user} == {"market", "alert"}

    for_feature = await repository.list(feature="market")
    assert {item.user_id for item in for_feature} == {101, 202}


@pytest.mark.asyncio
async def test_authority_defaults_to_allowed_and_caches(tmp_path: Path) -> None:
    _database, repository = await _repository(tmp_path)
    authority = FeatureAuthority(repository)
    assert await authority.allowed(101, "market") is True
    assert await authority.allowed(101, "alert") is True
    await authority.set(101, "alert", False)
    assert await authority.allowed(101, "alert") is False
    assert await authority.allowed(101, "market") is True
    await authority.clear(101, "alert")
    assert await authority.allowed(101, "alert") is True


@pytest.mark.asyncio
async def test_authority_invalidate_after_external_change(tmp_path: Path) -> None:
    _database, repository = await _repository(tmp_path)
    authority = FeatureAuthority(repository)
    assert await authority.allowed(101, "news") is True
    await repository.set(101, "news", False)
    assert await authority.allowed(101, "news") is True  # cached
    authority.invalidate(user_id=101, feature="news")
    assert await authority.allowed(101, "news") is False
    authority.invalidate()
    await repository.clear(101, "news")
    assert await authority.allowed(101, "news") is True


def test_feature_registry_is_stable_and_denied_message_present() -> None:
    assert set(FEATURES) >= {
        "chat",
        "market",
        "watchlist",
        "alert",
        "inventory",
        "compare",
        "news",
        "entertainment",
    }
    assert "未对你开放" in DENIED_MESSAGE


@pytest.mark.asyncio
async def test_permission_repository_rejects_invalid_inputs(tmp_path: Path) -> None:
    _database, repository = await _repository(tmp_path)
    with pytest.raises(ValueError):
        await repository.set(0, "market", False)
    with pytest.raises(ValueError):
        await repository.set(101, "", False)
    with pytest.raises(ValueError):
        await repository.set(101, "market", 1)  # type: ignore[arg-type]
