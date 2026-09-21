"""V0.5 Window A: hybrid retrieval, scoring factor port and case-EV calculator."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import nonebot
import pytest
from nonebot.exception import FinishedException

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(_env_file=())

from src.core.database import SCHEMA_VERSION, StateDatabase
from src.services import case_ev as case_ev_module
from src.services import case_meta as case_meta_module
from src.services import embedding as embedding_module
from src.services import market_scoring as scoring_module
from src.services import memory_search
from src.services.case_ev import (
    CaseEvItem,
    CaseEvResult,
    WearQuote,
    _wear_variants,
    case_ev_payload,
    compute_case_ev,
    render_case_ev,
)
from src.services.case_meta import (
    SPECIAL_ODDS,
    CaseMetaStore,
    CrateInfo,
    CrateItem,
    _parse_pool_items,
    _validate_public_url,
    rarity_to_tier,
)
from src.services.embedding import (
    EMBEDDING_DIM,
    cosine_similarity,
    decode_vector_blob,
    encode_vector_blob,
    parse_embeddings_response,
)
from src.services.memory_search import hybrid_search
from src.services.tools import execute_tool
from src.state.insight_embedding_repository import InsightEmbeddingRepository
from src.state.persona_memory_repository import PersonaScope
from src.state.runtime import database, learning_insights

# ---------------------------------------------------------------------------
# Schema v17→v18: FTS5 trigram index + embedding store
# ---------------------------------------------------------------------------


async def _seed_accepted_insight(user_id: int, content: str, *, kind: str = "knowledge") -> int:
    insight = await learning_insights.create_pending(user_id, 0, kind, content, "测试依据")
    assert insight is not None
    assert await learning_insights.confirm(insight.id, user_id, 0)
    return insight.id


@pytest.mark.asyncio
async def test_v17_to_v18_migration_backfills_fts_and_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "v17.db"
    database = StateDatabase(path)
    await database.initialize()
    # Seed rows, then strip the v18 objects to recreate the exact v17 shape.
    await database.write(
        "INSERT INTO learning_insights (user_id, scope_group_id, kind, content, evidence, "
        "confidence, status) VALUES (71, 0, 'knowledge', '常查价格：梦魇武器箱 AK-47 渐变之色', "
        "'历史查询', 3, 'accepted')"
    )
    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP TRIGGER learning_insights_fts_ai")
        connection.execute("DROP TRIGGER learning_insights_fts_ad")
        connection.execute("DROP TRIGGER learning_insights_fts_au")
        connection.execute("DROP TABLE learning_insights_fts")
        connection.execute("DROP TABLE insight_embeddings")
        connection.execute("PRAGMA user_version = 17")
        connection.commit()
    finally:
        connection.close()

    migrated = StateDatabase(path, allow_schema_migrations=True)
    await migrated.initialize()
    assert (await migrated.read("PRAGMA user_version"))[0]["user_version"] == SCHEMA_VERSION

    fts_rows = await migrated.read("SELECT rowid FROM learning_insights_fts")
    assert [row["rowid"] for row in fts_rows] == [1], "existing rows must be backfilled"
    embedding_rows = await migrated.read("SELECT * FROM insight_embeddings")
    assert embedding_rows == []
    assert await migrated.integrity_check()

    # Idempotent rerun must neither fail nor duplicate the FTS rows.
    again = StateDatabase(path, allow_schema_migrations=True)
    await again.initialize()
    assert len(await again.read("SELECT rowid FROM learning_insights_fts")) == 1


@pytest.mark.asyncio
async def test_fts_triggers_keep_index_in_sync(tmp_path: Path) -> None:
    from src.state.learning_repository import LearningInsightRepository

    store = StateDatabase(tmp_path / "state.db")
    repo = LearningInsightRepository(store)
    await repo.initialize()
    insight = await repo.create_pending(81, 0, "knowledge", "常查价格：二西莫夫 AWP", "证据")
    assert insight is not None
    assert await repo.confirm(insight.id, 81, 0)
    rows = await store.read(
        "SELECT rowid FROM learning_insights_fts WHERE learning_insights_fts MATCH ?",
        ('"二西莫夫"',),
    )
    assert rows, "insert trigger must index new rows"
    await repo.merge_update(insight.id, content="常查价格：野牛 蓝色渐变")
    stale = await store.read(
        "SELECT rowid FROM learning_insights_fts WHERE learning_insights_fts MATCH ?",
        ('"二西莫夫"',),
    )
    assert stale == [], "update trigger must drop the old content"
    fresh = await store.read(
        "SELECT rowid FROM learning_insights_fts WHERE learning_insights_fts MATCH ?",
        ('"蓝色渐变"',),
    )
    assert fresh
    await repo.delete(insight.id, 81, 0)
    # Physical deletion removes the index entry; status flips never touch it.
    count = (await store.read("SELECT COUNT(*) AS c FROM learning_insights_fts"))[0]["c"]
    assert count == 0


# ---------------------------------------------------------------------------
# Embedding service
# ---------------------------------------------------------------------------


def test_parse_embeddings_response_validates_structure() -> None:
    good = {"data": [{"embedding": [0.5] * EMBEDDING_DIM, "index": 0}]}
    assert parse_embeddings_response(good) == [[0.5] * EMBEDDING_DIM]
    assert parse_embeddings_response({"data": [{"embedding": [0.5] * 3}]}) is None
    assert parse_embeddings_response({"data": []}) is None
    assert parse_embeddings_response({"nope": True}) is None
    assert parse_embeddings_response("not a dict") is None
    bad_float = {"data": [{"embedding": [float("inf")] + [0.0] * (EMBEDDING_DIM - 1)}]}
    assert parse_embeddings_response(bad_float) is None


def test_vector_blob_roundtrip_and_cosine() -> None:
    vector = [0.1 * (index % 7) for index in range(EMBEDDING_DIM)]
    blob = encode_vector_blob(vector)
    assert blob is not None and len(blob) == 4096
    decoded = decode_vector_blob(blob)
    assert decoded is not None and len(decoded) == EMBEDDING_DIM
    assert decoded[0] == pytest.approx(vector[0])
    assert decode_vector_blob(b"short") is None
    assert encode_vector_blob([0.1, 0.2]) is None
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine_similarity([], []) == 0.0


@pytest.mark.asyncio
async def test_embed_texts_fail_closed_when_disabled_or_broken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TAFFY_MEMORY_SEMANTIC_ENABLED", raising=False)
    monkeypatch.delenv("TAFFY_EMBEDDING_API_KEY", raising=False)
    assert embedding_module.semantic_enabled() is False
    assert await embedding_module.embed_texts(["文本"]) is None

    monkeypatch.setenv("TAFFY_MEMORY_SEMANTIC_ENABLED", "true")
    monkeypatch.setenv("TAFFY_EMBEDDING_API_KEY", "sk-test-value")

    async def explode(*args: object, **kwargs: object) -> dict:
        raise RuntimeError("network down")

    with patch.object(embedding_module, "request_json", side_effect=explode):
        assert await embedding_module.embed_texts(["文本"]) is None

    with patch.object(
        embedding_module,
        "request_json",
        AsyncMock(return_value={"data": [{"embedding": [0.1] * 8}]}),
    ):
        assert await embedding_module.embed_texts(["文本"]) is None, "wrong dimension rejects"


@pytest.mark.asyncio
async def test_embed_texts_honours_daily_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAFFY_MEMORY_SEMANTIC_ENABLED", "true")
    monkeypatch.setenv("TAFFY_EMBEDDING_API_KEY", "sk-test-value")
    with patch.object(embedding_module, "_DAILY_BUDGET", 0):
        with patch.object(
            embedding_module,
            "request_json",
            AsyncMock(side_effect=AssertionError("budget exhausted must not call the API")),
        ):
            assert await embedding_module.embed_texts(["文本"]) is None


@pytest.mark.asyncio
async def test_insight_embedding_repository_roundtrip(tmp_path: Path) -> None:
    from src.state.learning_repository import LearningInsightRepository

    store = StateDatabase(tmp_path / "state.db")
    await store.initialize()
    insights = LearningInsightRepository(store)
    await insights.initialize()
    insight = await insights.create_pending(31, 0, "knowledge", "常查价格：二西莫夫", "证据")
    assert insight is not None
    await insights.confirm(insight.id, 31, 0)

    repository = InsightEmbeddingRepository(store)
    vector = [0.25] * EMBEDDING_DIM
    blob = encode_vector_blob(vector)
    assert blob is not None
    assert await repository.put(insight.id, embedding=blob, model="BAAI/bge-m3") is True
    found = await repository.get([insight.id], model="BAAI/bge-m3")
    assert set(found) == {insight.id}
    assert decode_vector_blob(found[insight.id].embedding)[0] == pytest.approx(0.25)
    assert await repository.get([insight.id], model="other-model") == {}
    assert await repository.put(insight.id, embedding=b"tiny", model="BAAI/bge-m3") is False
    # Foreign keys: no embedding may exist without its parent insight.
    with pytest.raises(sqlite3.IntegrityError):
        await repository.put(99999, embedding=blob, model="BAAI/bge-m3")


# ---------------------------------------------------------------------------
# Hybrid search channels
# ---------------------------------------------------------------------------


@pytest.fixture()
async def clean_insights():
    await learning_insights.initialize()
    yield
    await database.write("DELETE FROM learning_insights")
    await database.write("DELETE FROM insight_embeddings")


def _tool_ctx(user_id: int) -> object:
    from src.services.tools import ToolContext

    return ToolContext(user_id=user_id, group_id=0)


@pytest.mark.asyncio
async def test_hybrid_search_bm25_route_and_scope_isolation(clean_insights) -> None:
    await _seed_accepted_insight(91, "常查价格：梦魇武器箱里的 AK-47 渐变之色")
    await _seed_accepted_insight(92, "其他用户的知识：二西莫夫很贵")

    result = await hybrid_search(91, 0, "渐变之色 现在什么价")
    assert result.channel == "bm25"
    assert result.hits and "渐变之色" in result.hits[0].insight.content

    empty = await hybrid_search(91, 0, "二西莫夫")
    assert empty.hits == (), "another scope's insights must stay invisible"

    note = await hybrid_search(91, 0, "渐")
    assert note.channel == "legacy" and note.note is not None


@pytest.mark.asyncio
async def test_hybrid_search_fuses_vector_route_with_rrf(
    clean_insights, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TAFFY_MEMORY_SEMANTIC_ENABLED", "true")
    monkeypatch.setenv("TAFFY_EMBEDDING_API_KEY", "sk-test-value")
    monkeypatch.setenv("TAFFY_MEMORY_SEMANTIC_PILOT_USERS", "93")
    insight_id = await _seed_accepted_insight(93, "常查价格：AK-47 渐变之色")
    await _seed_accepted_insight(93, "用户喜欢蝴蝶刀 深红之网", kind="preference")

    monkey_vectors = {
        "常查价格：AK-47 渐变之色": [1.0] + [0.0] * (EMBEDDING_DIM - 1),
        "用户喜欢蝴蝶刀 深红之网": [0.0] + [1.0] + [0.0] * (EMBEDDING_DIM - 2),
    }

    async def fake_batch(texts: list[str]) -> list[list[float]]:
        return [monkey_vectors[text] for text in texts]

    async def fake_query(text: str) -> list[float]:
        del text  # the query is semantically closest to the AK-47 insight
        return [0.9] + [0.1] + [0.0] * (EMBEDDING_DIM - 2)

    with (
        patch.object(memory_search, "embed_texts", side_effect=fake_batch),
        patch.object(memory_search, "embed_single", side_effect=fake_query),
    ):
        result = await hybrid_search(93, 0, "渐变之色 AK-47 价格")

    assert result.channel == "hybrid"
    assert result.hits[0].insight.id == insight_id


@pytest.mark.asyncio
async def test_hybrid_search_degrades_when_semantic_fails(
    clean_insights, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TAFFY_MEMORY_SEMANTIC_ENABLED", "true")
    monkeypatch.setenv("TAFFY_EMBEDDING_API_KEY", "sk-test-value")
    monkeypatch.setenv("TAFFY_MEMORY_SEMANTIC_PILOT_USERS", "94")
    await _seed_accepted_insight(94, "常查价格：AK-47 渐变之色")

    async def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("provider down")

    with (
        patch.object(memory_search, "embed_texts", side_effect=broken),
        patch.object(memory_search, "embed_single", side_effect=broken),
    ):
        result = await hybrid_search(94, 0, "渐变之色 价格")

    assert result.channel == "bm25", "BM25 must survive a dead semantic route"
    assert "语义通道" in (result.note or "")
    assert result.hits and "渐变之色" in result.hits[0].insight.content


@pytest.mark.asyncio
async def test_memory_tool_reports_channel_note(clean_insights) -> None:
    await _seed_accepted_insight(95, "常查价格：AK-47 | Redline")
    text, action = await execute_tool(
        _tool_ctx(95),
        "search_memories",
        {"keyword": "Redline 常查价格"},
    )
    assert action is None
    assert "常查价格" in text
    assert "检索通道" in text


# ---------------------------------------------------------------------------
# Scoring factor port (年化衰减 / 集中度 / 收益达标度)
# ---------------------------------------------------------------------------

_FLAT = [100.0] * 40
_FALLING = [200.0 - index * 1.5 for index in range(40)]


def _factor(score, name: str):
    return next(factor for factor in score.sell.factors if factor.name == name)


def test_new_sell_factors_present_with_balanced_weights() -> None:
    score = scoring_module.compute_trade_score(
        _FLAT,
        sample_days=40,
        sell_count=100,
        bid_count=10,
        spread_ratio=0.99,
        rent_annual=2.0,
        platform_sell_counts=(10, 8, 5, 4, 2, 1),
    )
    assert score is not None
    names = [factor.name for factor in score.sell.factors]
    assert {"年化衰减", "集中度", "收益达标度"} <= set(names)
    total = sum(factor.weight for factor in score.sell.factors)
    assert abs(total - 1.0) < 1e-9


def test_annualized_decay_scores_holding_bleed() -> None:
    score = scoring_module.compute_trade_score(
        _FALLING,
        sample_days=40,
        sell_count=100,
        bid_count=10,
        spread_ratio=None,
        rent_annual=-20.0,
    )
    assert score is not None
    decay = _factor(score, "年化衰减")
    assert decay.score == 100.0, "price decay plus negative rent must max the factor"
    assert "租金年化" in decay.basis

    healthy = scoring_module.compute_trade_score(
        [100.0 + index for index in range(40)],
        sample_days=40,
        sell_count=100,
        bid_count=10,
        spread_ratio=None,
        rent_annual=15.0,
    )
    assert healthy is not None
    assert _factor(healthy, "年化衰减").score == 0.0


def test_profit_attainment_rewritten_to_90d_high_room() -> None:
    plateau = [100.0] * 39 + [99.0]
    score = scoring_module.compute_trade_score(
        plateau, sample_days=40, sell_count=100, bid_count=10, spread_ratio=None
    )
    assert score is not None
    attainment = _factor(score, "收益达标度")
    assert attainment.score == 100.0, "within 2% of the 90d high must be full marks"

    trough = [100.0] + [50.0] * 39
    deep = scoring_module.compute_trade_score(
        trough, sample_days=40, sell_count=100, bid_count=10, spread_ratio=None
    )
    assert deep is not None
    assert _factor(deep, "收益达标度").score == 0.0


def test_concentration_factor_uses_platform_hhi() -> None:
    spread = scoring_module.compute_trade_score(
        _FLAT,
        sample_days=40,
        sell_count=100,
        bid_count=10,
        spread_ratio=None,
        platform_sell_counts=(5, 5, 5, 5, 5, 5),
    )
    assert spread is not None
    concentration = _factor(spread, "集中度")
    assert concentration.score == pytest.approx(100.0)
    assert "HHI" in concentration.basis

    single = scoring_module.compute_trade_score(
        _FLAT,
        sample_days=40,
        sell_count=100,
        bid_count=10,
        spread_ratio=None,
        platform_sell_counts=(30,),
    )
    assert single is not None
    # Missing data removes the factor entirely (established redistribution
    # semantics) and its weight flows back to the survivors.
    assert all(factor.name != "集中度" for factor in single.sell.factors)
    assert "集中度" in (single.sell.note or "")
    assert abs(sum(factor.weight for factor in single.sell.factors) - 1.0) < 1e-9


def test_missing_csqaq_data_redistributes_and_notes() -> None:
    score = scoring_module.compute_trade_score(
        _FLAT, sample_days=40, sell_count=None, bid_count=None, spread_ratio=None
    )
    assert score is not None
    assert score.sell.note is not None
    assert "流动性" in score.sell.note and "年化衰减" in score.sell.note
    assert "集中度" in score.sell.note
    total = sum(factor.weight for factor in score.sell.factors)
    assert abs(total - 1.0) < 1e-9, "redistribution keeps the weight sum at 1"


# ---------------------------------------------------------------------------
# Case metadata store
# ---------------------------------------------------------------------------


def test_rarity_mapping_and_host_validation() -> None:
    assert rarity_to_tier({"id": "rarity_rare_weapon", "name": "Mil-Spec Grade"}) == "milspec"
    assert rarity_to_tier({"id": "rarity_mythical_weapon", "name": "Restricted"}) == "restricted"
    assert rarity_to_tier({"id": "rarity_legendary_weapon", "name": "Classified"}) == "classified"
    assert rarity_to_tier({"id": "rarity_ancient_weapon", "name": "Covert"}) == "covert"
    assert rarity_to_tier({"id": "rarity_common", "name": "Base Grade"}) is None
    assert rarity_to_tier("junk") is None

    assert _validate_public_url("https://cdn.jsdelivr.net/gh/x") is True
    assert _validate_public_url("http://localhost/x") is False
    assert _validate_public_url("http://127.0.0.1/x") is False
    assert _validate_public_url("http://192.168.1.4/x") is False
    assert _validate_public_url("ftp://cdn.jsdelivr.net/x") is False


def _sample_en_crates() -> str:
    return json.dumps(
        [
            {
                "id": "crate-1",
                "name": "Dreams & Nightmares Case",
                "type": "Case",
                "first_sale_date": "2021-11-17",
                "contains": [
                    {"name": "AK-47 | Phantom", "rarity": {"id": "rarity_ancient_weapon"}},
                    {"name": "M4A1 | Nightmare", "rarity": {"id": "rarity_legendary_weapon"}},
                    {"name": "USP | Sleep", "rarity": {"id": "rarity_rare_weapon"}},
                ],
                "contains_rare": [{"name": "★ Karambit | Fade"}],
            },
            {
                "id": "crate-2",
                "name": "Another Case",
                "type": "Case",
                "contains": [{"name": "P250 | Sims", "rarity": {"id": "rarity_rare_weapon"}}],
                "contains_rare": [],
            },
        ]
    )


def _sample_zh_crates() -> str:
    return json.dumps(
        [
            {
                "id": "crate-1",
                "name": "梦魇武器箱",
                "type": "Case",
                "market_hash_name": "Dreams & Nightmares Case",
            }
        ]
    )


def test_parse_crates_merges_zh_names_and_pools() -> None:
    parsed = case_meta_module._parse_crates(_sample_en_crates(), _sample_zh_crates())
    assert set(parsed) == {"crate-1", "crate-2"}
    crate = parsed["crate-1"]
    assert crate["name_zh"] == "梦魇武器箱"
    assert crate["market_hash_name"] == "Dreams & Nightmares Case"
    items, rare_count, unknown = _parse_pool_items(crate)
    tiers = {item.tier for item in items}
    assert tiers == {"milspec", "classified", "covert"}
    assert rare_count == 1 and unknown == 0


@pytest.mark.asyncio
async def test_case_meta_store_roundtrip_and_lookup(tmp_path: Path) -> None:
    store = CaseMetaStore(tmp_path / "meta.db")
    await store.initialize()
    assert await store.find_crate("不存在") is None

    # Simulate a completed sync snapshot.
    parsed = case_meta_module._parse_crates(_sample_en_crates(), _sample_zh_crates())
    now = int(time.time())

    def seed(connection: sqlite3.Connection) -> None:
        for crate in parsed.values():
            items, rare_count, unknown = case_meta_module._parse_pool_items(crate)
            connection.execute(
                "INSERT INTO crates (crate_id, name_en, name_zh, market_hash_name, "
                "first_sale_date, rare_count, unknown_count, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(crate["crate_id"]),
                    str(crate["name_en"]),
                    crate["name_zh"],
                    str(crate["market_hash_name"]),
                    crate["first_sale_date"],
                    rare_count,
                    unknown,
                    now,
                ),
            )
            for item in items:
                connection.execute(
                    "INSERT INTO crate_items (crate_id, item_name, item_name_zh, tier) "
                    "VALUES (?, ?, NULL, ?)",
                    (str(crate["crate_id"]), item.item_name, item.tier),
                )
        connection.execute(
            "INSERT INTO sync_state (source, fetched_at, status) VALUES ('bymykel_crates', ?, 'ok')",
            (now,),
        )
        connection.commit()

    await store._database_call(seed)

    zh_hit = await store.find_crate("梦魇武器箱")
    assert zh_hit is not None and zh_hit.market_hash_name == "Dreams & Nightmares Case"
    en_hit = await store.find_crate("dreams & nightmares case")
    assert en_hit is not None and en_hit.crate_id == "crate-1"
    ambiguous = await store.find_crate("case")
    assert ambiguous is None, "substring hitting several crates must refuse"

    items = await store.crate_items("crate-1")
    assert len(items) == 3

    await store.store_ev("Dreams & Nightmares Case", {"ev": 1.23})
    cached = await store.cached_ev("Dreams & Nightmares Case", ttl_seconds=3600)
    assert cached == {"ev": 1.23}
    assert await store.cached_ev("Dreams & Nightmares Case", ttl_seconds=-1) is None


# ---------------------------------------------------------------------------
# Case EV computation and rendering
# ---------------------------------------------------------------------------


def _crate() -> CrateInfo:
    return CrateInfo(
        crate_id="crate-1",
        name_en="Dreams & Nightmares Case",
        name_zh="梦魇武器箱",
        market_hash_name="Dreams & Nightmares Case",
        crate_type="Case",
        first_sale_date="2021-11-17",
        rare_count=50,
        unknown_count=0,
        fetched_at=int(time.time()),
    )


def _pool() -> tuple[CrateItem, ...]:
    return (
        CrateItem("AK-47 | Phantom", None, "covert"),
        CrateItem("M4A1 | Nightmare", None, "classified"),
        CrateItem("USP | Sleep", None, "milspec"),
    )


def _catalog() -> tuple[str, ...]:
    names: list[str] = []
    for base in ("AK-47 | Phantom", "M4A1 | Nightmare", "USP | Sleep"):
        for wear in ("Factory New", "Field-Tested", "Battle-Scarred"):
            names.append(f"{base} ({wear})")
    return tuple(names)


def _prices(name: str) -> float:
    if "(" not in name:
        return 10.0  # the tradable case itself
    wear = name[name.rfind("(") + 1 : -1]
    if "Phantom" in name:
        return {"Factory New": 100.0, "Field-Tested": 50.0, "Battle-Scarred": 30.0}[wear]
    if "Nightmare" in name:
        return {"Factory New": 40.0, "Field-Tested": 20.0, "Battle-Scarred": 10.0}[wear]
    return {"Factory New": 4.0, "Field-Tested": 2.0, "Battle-Scarred": 1.0}[wear]


@pytest.mark.asyncio
async def test_compute_case_ev_matches_official_odds_math() -> None:
    async def fake_price(name: str) -> dict | None:
        return {"min_sell": _prices(name)}

    with patch.object(case_ev_module, "_price_one", side_effect=fake_price):
        result = await compute_case_ev(_crate(), _pool(), _catalog())

    assert result.case_price is not None
    covert_avg = (100.0 + 50.0 + 30.0) / 3
    classified_avg = (40.0 + 20.0 + 10.0) / 3
    milspec_avg = (4.0 + 2.0 + 1.0) / 3
    expected = (
        case_ev_module.TIER_ODDS["covert"] * covert_avg
        + case_ev_module.TIER_ODDS["classified"] / 1 * classified_avg
        + case_ev_module.TIER_ODDS["milspec"] / 1 * milspec_avg
    )
    assert result.ev == pytest.approx(expected, rel=1e-9)
    assert result.unpriced_items == 0 and result.missing_prices == 0


@pytest.mark.asyncio
async def test_compute_case_ev_handles_missing_prices() -> None:
    async def partial_price(name: str) -> dict | None:
        if "Phantom" in name:
            return None  # whole covert skin unpriced
        return {"min_sell": _prices(name)}

    with patch.object(case_ev_module, "_price_one", side_effect=partial_price):
        result = await compute_case_ev(_crate(), _pool(), _catalog())

    assert result.unpriced_items == 1
    assert result.missing_prices == 3
    phantom = next(item for item in result.items if "Phantom" in item.item_name)
    assert phantom.avg_price is None and phantom.contribution == 0.0


def test_wear_variants_respect_catalog_order() -> None:
    variants = _wear_variants(_catalog(), "USP | Sleep")
    assert variants == [
        "USP | Sleep (Factory New)",
        "USP | Sleep (Field-Tested)",
        "USP | Sleep (Battle-Scarred)",
    ]
    assert _wear_variants(_catalog(), "Unknown Skin") == []


def test_render_states_negative_ev_and_disclaimer() -> None:
    result = CaseEvResult(
        market_hash_name="Dreams & Nightmares Case",
        display_name="梦魇武器箱",
        case_price=10.0,
        items=(
            CaseEvItem(
                "USP | Sleep",
                None,
                "milspec",
                case_ev_module.TIER_ODDS["milspec"],
                (WearQuote("Field-Tested", "USP | Sleep (Field-Tested)", 2.0),),
                2.0,
                case_ev_module.TIER_ODDS["milspec"] * 2.0,
            ),
        ),
        special_count=50,
        unpriced_items=0,
        ev=case_ev_module.TIER_ODDS["milspec"] * 2.0,
        missing_prices=0,
        unavailable_prices=0,
    )
    text = render_case_ev(result, now=1_800_000_000)
    assert "梦魇武器箱 开箱EV" in text
    assert "开箱期望为负" in text
    assert "概率为 Valve 公开掉率，历史数据不构成收益承诺" in text
    assert "罕见特殊物品 ×50" in text and "下界" in text
    payload = case_ev_payload(result)
    assert payload["return_rate_percent"] == pytest.approx(round(result.ev / 10.0 * 100, 2))
    assert payload["special_odds"] == SPECIAL_ODDS


@pytest.mark.asyncio
async def test_build_case_ev_reply_fail_closed_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = CaseMetaStore(tmp_path / "meta.db")
    monkeypatch.setattr(case_ev_module, "case_meta_store", store)

    async def sync_fail(*args: object, **kwargs: object) -> bool:
        return False

    with patch.object(store, "sync_if_due", side_effect=sync_fail):
        reply = await case_ev_module.build_case_ev_reply("梦魇武器箱")
    assert "元数据" in reply and "同步" in reply

    with patch.object(store, "sync_if_due", AsyncMock(return_value=True)):
        reply = await case_ev_module.build_case_ev_reply("梦魇武器箱")
    assert "没有唯一匹配" in reply


# ---------------------------------------------------------------------------
# Coverage batch: sync flow, pricing orchestration, command wiring
# ---------------------------------------------------------------------------


def _fake_upstream() -> dict[str, str]:
    return {
        "https://cdn.jsdelivr.net/gh/ByMykel/CSGO-API@main/public/api/en/crates.json": _sample_en_crates(),
        "https://cdn.jsdelivr.net/gh/ByMykel/CSGO-API@main/public/api/zh-CN/crates.json": _sample_zh_crates(),
        "https://cdn.jsdelivr.net/gh/jonese1234/Csgo-Case-Data@master/CaseContents.json": json.dumps(
            {
                "Cases": {
                    "Dreams & Nightmares Case": {
                        "Items": ["AK-47 | Phantom", "M4A1 | Nightmare", "USP | Sleep"]
                    }
                }
            }
        ),
    }


@pytest.mark.asyncio
async def test_case_meta_sync_fetches_and_stores(tmp_path: Path) -> None:
    store = CaseMetaStore(tmp_path / "meta.db")
    await store.initialize()
    responses = _fake_upstream()

    async def fake_fetch_file(url: str) -> str:
        return responses[url]

    with (
        patch.object(
            case_meta_module,
            "_META_BASE_URL",
            "https://cdn.jsdelivr.net/gh/ByMykel/CSGO-API@main/public/api",
        ),
        patch.object(
            case_meta_module,
            "_CASEDATA_URL",
            "https://cdn.jsdelivr.net/gh/jonese1234/Csgo-Case-Data@master/CaseContents.json",
        ),
        patch.object(case_meta_module, "_fetch_file", side_effect=fake_fetch_file),
    ):
        assert await store.sync_if_due() is True

    assert await store.last_sync() is not None
    crate = await store.find_crate("梦魇武器箱")
    assert crate is not None and crate.rare_count == 1 and crate.unknown_count == 0
    items = await store.crate_items("crate-1")
    assert {item.tier for item in items} == {"milspec", "classified", "covert"}

    # A sync inside the TTL window must not re-download.
    with patch.object(
        case_meta_module,
        "_fetch_file",
        side_effect=AssertionError("TTL not respected"),
    ):
        assert await store.sync_if_due() is True


@pytest.mark.asyncio
async def test_case_meta_sync_uses_mirror_when_primary_fails(tmp_path: Path) -> None:
    store = CaseMetaStore(tmp_path / "meta.db")
    await store.initialize()
    responses = _fake_upstream()
    seen: list[str] = []

    async def flaky_fetch_file(url: str) -> str:
        seen.append(url)
        if url.startswith("https://cdn.jsdelivr.net"):
            raise RuntimeError("primary down")
        return responses[url.replace("fastly.jsdelivr.net/gh", "cdn.jsdelivr.net/gh")]

    with (
        patch.object(
            case_meta_module,
            "_META_BASE_URL",
            "https://cdn.jsdelivr.net/gh/ByMykel/CSGO-API@main/public/api",
        ),
        patch.object(
            case_meta_module,
            "_CASEDATA_URL",
            "https://cdn.jsdelivr.net/gh/jonese1234/Csgo-Case-Data@master/CaseContents.json",
        ),
        patch.object(case_meta_module, "_fetch_file", side_effect=flaky_fetch_file),
    ):
        assert await store.sync_if_due() is True
    assert any("fastly.jsdelivr.net" in url for url in seen), "mirror must be tried"
    crate = await store.find_crate("Dreams & Nightmares Case")
    assert crate is not None


@pytest.mark.asyncio
async def test_case_meta_background_first_sync_reports_syncing(
    tmp_path: Path,
) -> None:
    store = CaseMetaStore(tmp_path / "meta.db")
    await store.initialize()
    responses = _fake_upstream()

    async def slow_fetch_file(url: str) -> str:
        await asyncio.sleep(0.01)
        return responses[url]

    with (
        patch.object(
            case_meta_module,
            "_META_BASE_URL",
            "https://cdn.jsdelivr.net/gh/ByMykel/CSGO-API@main/public/api",
        ),
        patch.object(
            case_meta_module,
            "_CASEDATA_URL",
            "https://cdn.jsdelivr.net/gh/jonese1234/Csgo-Case-Data@master/CaseContents.json",
        ),
        patch.object(case_meta_module, "_fetch_file", side_effect=slow_fetch_file),
    ):
        first = await store.sync_if_due(background=True)
        assert first is False, "no snapshot yet: caller must report syncing"
        task = store._sync_task
        assert task is not None
        await asyncio.wait_for(task, timeout=5)
        # A follow-up call sees the settled snapshot inside the TTL window.
        assert await store.sync_if_due(background=True) is True
    crate = await store.find_crate("梦魇武器箱")
    assert crate is not None


@pytest.mark.asyncio
async def test_case_meta_sync_failure_keeps_previous_snapshot(tmp_path: Path) -> None:
    store = CaseMetaStore(tmp_path / "meta.db")
    await store.initialize()
    now = int(time.time())

    def seed(connection: sqlite3.Connection) -> None:
        connection.execute(
            "INSERT INTO crates (crate_id, name_en, name_zh, market_hash_name, "
            "first_sale_date, rare_count, unknown_count, fetched_at) "
            "VALUES ('crate-1', 'Old Case', NULL, 'Old Case', NULL, 0, 0, ?)",
            (now - 999_999,),
        )
        connection.execute(
            "INSERT INTO sync_state (source, fetched_at, status) "
            "VALUES ('bymykel_crates', ?, 'ok')",
            (now - 999_999,),
        )
        connection.commit()

    await store._database_call(seed)

    async def broken_fetch(*args: object, **kwargs: object) -> str:
        raise RuntimeError("offline")

    with (
        patch.object(
            case_meta_module,
            "_META_BASE_URL",
            "https://cdn.jsdelivr.net/gh/ByMykel/CSGO-API@main/public/api",
        ),
        patch.object(
            case_meta_module,
            "_CASEDATA_URL",
            "https://cdn.jsdelivr.net/gh/jonese1234/Csgo-Case-Data@master/CaseContents.json",
        ),
        patch.object(case_meta_module, "_fetch_file", side_effect=broken_fetch),
    ):
        assert await store.sync_if_due(force=True) is True, "stale snapshot stays usable"
    crate = await store.find_crate("Old Case")
    assert crate is not None, "old snapshot survives a failed sync"


@pytest.mark.asyncio
async def test_price_one_validates_and_fails_closed() -> None:
    valid = {
        "success": True,
        "updateTime": int(time.time() * 1000),
        "data": [
            {"platform": "BUFF", "sellPrice": 12.5, "sellCount": 3},
            {"platform": "C5", "sellPrice": 11.0, "sellCount": 1},
        ],
    }

    async def ok_get(*args: object, **kwargs: object) -> dict:
        return valid

    async def bad_get(*args: object, **kwargs: object) -> dict:
        return {"success": False}

    with patch.object(case_ev_module.market_service, "get", side_effect=ok_get):
        snapshot = await case_ev_module._price_one("AK-47 | Phantom (Field-Tested)")
    assert snapshot is not None and snapshot["min_sell"] == 11.0

    with patch.object(case_ev_module.market_service, "get", side_effect=bad_get):
        assert await case_ev_module._price_one("AK-47 | Phantom (Field-Tested)") is None


@pytest.mark.asyncio
async def test_build_case_ev_reply_success_and_cache(tmp_path: Path) -> None:
    store = CaseMetaStore(tmp_path / "meta.db")
    await store.initialize()
    now = int(time.time())

    def seed(connection: sqlite3.Connection) -> None:
        connection.execute(
            "INSERT INTO crates (crate_id, name_en, name_zh, market_hash_name, "
            "first_sale_date, rare_count, unknown_count, fetched_at) "
            "VALUES ('crate-1', 'Dreams & Nightmares Case', '梦魇武器箱', "
            "'Dreams & Nightmares Case', '2021-11-17', 50, 0, ?)",
            (now,),
        )
        connection.execute(
            "INSERT INTO crate_items (crate_id, item_name, item_name_zh, tier) "
            "VALUES ('crate-1', 'USP | Sleep', NULL, 'milspec')"
        )
        connection.execute(
            "INSERT INTO sync_state (source, fetched_at, status) "
            "VALUES ('bymykel_crates', ?, 'ok')",
            (now,),
        )
        connection.commit()

    await store._database_call(seed)

    async def fake_sync(*args: object, **kwargs: object) -> bool:
        return True

    async def fake_price(name: str) -> dict | None:
        del name
        return {"min_sell": 5.0}

    with (
        patch.object(case_ev_module, "case_meta_store", store),
        patch.object(store, "sync_if_due", side_effect=fake_sync),
        patch.object(
            case_ev_module,
            "_catalog_market_names",
            AsyncMock(return_value=("USP | Sleep (Field-Tested)",)),
        ),
        patch.object(case_ev_module, "_price_one", side_effect=fake_price),
    ):
        reply = await case_ev_module.build_case_ev_reply("梦魇武器箱")
        assert "开箱期望为负" in reply
        assert "概率为 Valve 公开掉率" in reply
        assert "罕见特殊物品 ×50" in reply

        # Second call within the TTL must come from the cached payload.
        with patch.object(
            case_ev_module,
            "_price_one",
            side_effect=AssertionError("cache hit must not reprice"),
        ):
            cached_reply = await case_ev_module.build_case_ev_reply("梦魇武器箱")
        assert cached_reply == reply


@pytest.mark.asyncio
async def test_build_case_ev_payload_unsynced_returns_none(tmp_path: Path) -> None:
    store = CaseMetaStore(tmp_path / "meta.db")
    await store.initialize()
    with patch.object(case_ev_module, "case_meta_store", store):

        async def no_sync(*args: object, **kwargs: object) -> bool:
            return False

        with patch.object(store, "sync_if_due", side_effect=no_sync):
            assert await case_ev_module.build_case_ev_payload("梦魇武器箱") is None


def _ev_event(user_id: int, text: str) -> SimpleNamespace:
    return SimpleNamespace(user_id=user_id, get_plaintext=lambda text=text: text)


@pytest.mark.asyncio
async def test_case_ev_command_replies() -> None:
    from src.plugins.chat import case_ev_commands

    finish = AsyncMock(side_effect=FinishedException())
    with (
        patch.object(case_ev_commands._RATE_LIMITER, "allow", AsyncMock(return_value=True)),
        patch.object(
            case_ev_commands,
            "persona_scope",
            return_value=PersonaScope(9101, 0),
        ),
        patch.object(
            case_ev_module,
            "build_case_ev_reply",
            AsyncMock(return_value="梦魇武器箱 开箱EV\n开箱期望为负"),
        ),
        patch.object(case_ev_commands.case_ev_cmd, "finish", finish),
        pytest.raises(FinishedException),
    ):
        await case_ev_commands.handle_case_ev(
            object(),
            _ev_event(9101, "开箱EV 梦魇武器箱"),
        )
    assert "开箱EV" in str(finish.await_args.args[0])


@pytest.mark.asyncio
async def test_bm25_route_reports_index_outage(clean_insights) -> None:
    await _seed_accepted_insight(96, "常查价格：AK-47 渐变之色")

    real_read = database.read

    async def fts_outage(sql: object, parameters: object = ()) -> list:
        if "learning_insights_fts" in str(sql):
            raise RuntimeError("fts index corrupted")
        return await real_read(sql, parameters)

    with patch.object(database, "read", side_effect=fts_outage):
        result = await hybrid_search(96, 0, "渐变之色 价格")
    assert result.channel == "legacy"
    assert "关键词索引" in (result.note or "")
    assert result.hits, "legacy term matcher keeps the answer available"
