"""Semi-automated learning: rule-based observations and LLM reflections.

Observations are cheap, rule-based and never call the LLM: they scan the
last 24 hours of session messages, match local catalog names (>=3 mentions)
and reuse the persona style-feedback vocabulary.  Reflections run once a
day per scope, ask the current model for a bounded JSON digest, and write
only ``pending`` rows.  Every insight stays confirmable, reviewable and
deletable; evidence is always a summary, never raw conversation text.

V0.5 memory foundations (all behind kill switches): the nightly
consolidation pass merges near-duplicate accepted insights, protects
well-attested knowledge from decay and — when enabled — asks the model for
cross-insight links (mem0/Letta-style sleep-time compute).  Explicit
``记住`` writes can route through an ADD/UPDATE/NOOP decision instead of
blind appending.  Retrieval hits reinforce weights (access-based
forgetting).
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

from ..state.persona_style_repository import parse_style_feedback
from ..state.runtime import database, learning_insights
from .config import env_bool
from .llm import _LLM_SEMAPHORE, try_anthropic
from .market_catalog_store import market_catalog_store
from .tz import to_market_tz

logger = logging.getLogger("taffy.learning")

_OBSERVATION_WINDOW_SECONDS = 24 * 3600
_MIN_MENTIONS = 3
_MIN_QUERIES = 3
_DAILY_PENDING_LIMIT = 3
_REFLECTION_BACKLOG_LIMIT = 10
_REFLECTION_MAX_ITEMS_PER_USER = 5
_REFLECTION_MAX_MESSAGES_PER_USER = 50
_REFLECTION_MAX_MODEL_CALLS = 30
_RAW_TEXT_FRAGMENT_MIN = 20
_REINFORCE_WINDOW_SECONDS = 24 * 3600
_REINFORCE_MAX_INSIGHTS = 500
_KNOWLEDGE_MAX_CONFIDENCE = 5

_ASCII_TERM_RE = re.compile(r"[a-z0-9][a-z0-9\-']{2,}")
_CJK_CHAR_RE = re.compile(r"[\u4e00-\u9fff]")

# Consolidation bounds: the merge pass is deterministic and cheap, but the
# link suggestion spends model calls, so both scope and call counts stay
# bounded per nightly run.
_MERGE_SIMILARITY = 0.82
_MERGE_WEIGHT_BOOST = 0.25
_MERGE_WEIGHT_CEILING = 2.0
_CONSOLIDATION_MAX_SCOPES = 10
_CONSOLIDATION_MAX_INSIGHTS_PER_SCOPE = 40
_CONSOLIDATION_MAX_LINKS_PER_SCOPE = 10
_CONSOLIDATION_MAX_MODEL_CALLS = 10
_DUPLICATE_SIMILARITY = 0.82
_DECISION_MAX_CANDIDATES = 3
_DECISION_SIMILARITY = 0.60

_REFLECTION_SYSTEM_PROMPT = (
    "你是塔菲的每日反思器，不扮演角色。"
    '基于用户消息中给定的昨日聊天片段，提炼最多5条"可确认的观察"。'
    "只输出一个 JSON 数组，不要输出任何其他文字："
    '[{"kind":"preference|style|knowledge|cultural","content":"印象内容",'
    '"confidence":1到5,"evidence":"日期+话题摘要"}]。'
    "kind=knowledge 用于当天被回答过、之后可复述的事实问题"
    "（例如用户多次查询某饰品价格，content 写成“常查价格：<饰品市场名>”这样的可复用知识点）；"
    "禁止引用聊天原文（content 和 evidence 都只能写摘要）；"
    "禁止推断宗教、政治、健康、情感关系等敏感内容。"
)

_LINK_SYSTEM_PROMPT = (
    "你是记忆整理器，不扮演角色。"
    "给定同一用户范围内已确认的印象列表（id + 内容），"
    "找出彼此相关或互补的印象对，输出一个 JSON 数组，不要输出其他文字："
    '[{"from":<id>,"to":<id>,"relation":"简短关系，如 同一饰品/相互矛盾/补充细节"}]。'
    "最多 10 条；from 和 to 必须是给定列表中不同的 id；"
    "没有相关对就输出 []。禁止编造列表之外的 id。"
)

_MEMORY_DECISION_SYSTEM_PROMPT = (
    "你是长期记忆整理器，不扮演角色。"
    "给定一条用户新记的内容和几条已存在的相似记忆，判断该怎么处理："
    '只输出一个 JSON 对象：{"decision":"ADD|UPDATE|NOOP","target_id":<已存在记忆的id或null>,'
    '"content":"UPDATE 时给出的合并后的新记忆文本"}。'
    "新信息与某条已有记忆重复或只是换种说法 → NOOP；"
    "新信息补充或修正了某条已有记忆 → UPDATE（content 写合并后的完整文本，target_id 指向那条记忆）；"
    "是全新的信息 → ADD。禁止输出 JSON 以外的文字。"
)


def _parse_session_key(session_key: str) -> tuple[int, int] | None:
    """Decode ``p_<user>`` or ``g_<group>_<user>`` into (user_id, group_id)."""

    if session_key.startswith("p_"):
        suffix = session_key[2:]
        if suffix.isdecimal():
            return int(suffix), 0
    elif session_key.startswith("g_"):
        parts = session_key[2:].split("_", 1)
        if len(parts) == 2 and parts[0].isdecimal() and parts[1].isdecimal():
            return int(parts[1]), int(parts[0])
    return None


async def _catalog_names() -> tuple[str, ...]:
    try:
        entries = await market_catalog_store.entries()
    except Exception as exc:
        logger.warning("Catalog unavailable for observation error=%s", type(exc).__name__)
        return ()
    names: set[str] = set()
    for entry in entries:
        names.add(entry.market_name)
        if getattr(entry, "display_name", None):
            names.add(str(entry.display_name))
    return tuple(sorted(names, key=len, reverse=True))


async def _recent_messages(
    since: int,
    until: int | None = None,
) -> list[tuple[int, int, str]]:
    """Return (user_id, group_id, content) rows in ``[since, until)``."""

    try:
        if until is None:
            rows = await database.read(
                """
                SELECT session_key, content FROM session_messages
                WHERE created_at >= ?
                """,
                (int(since),),
            )
        else:
            rows = await database.read(
                """
                SELECT session_key, content FROM session_messages
                WHERE created_at >= ? AND created_at < ?
                """,
                (int(since), int(until)),
            )
    except Exception as exc:
        logger.warning("Session scan failed error=%s", type(exc).__name__)
        return []
    messages: list[tuple[int, int, str]] = []
    for row in rows:
        scope = _parse_session_key(str(row["session_key"]))
        if scope is None:
            continue
        content = str(row["content"]).strip()
        if not content or len(content) > 500:
            continue
        messages.append((scope[0], scope[1], content))
    return messages


async def _recent_usage_inputs(since: int) -> list[tuple[int, int, str]]:
    """Return (user_id, group_id, input_text) audit rows newer than ``since``."""

    try:
        rows = await database.read(
            """
            SELECT user_id, group_id, input_text FROM usage_log
            WHERE created_at >= ?
            """,
            (int(since),),
        )
    except Exception as exc:
        logger.warning("Usage scan failed error=%s", type(exc).__name__)
        return []
    inputs: list[tuple[int, int, str]] = []
    for row in rows:
        text = str(row["input_text"]).strip()
        if text and len(text) <= 500:
            inputs.append((int(row["user_id"]), int(row["group_id"]), text))
    return inputs


def _name_match_index(names: tuple[str, ...]) -> dict[str, tuple[tuple[str, str], ...]]:
    """Bucket catalog names by first character, longest first.

    Scanning a message against thousands of names is quadratic when done as
    one linear pass per message; the bucket index touches only candidates
    whose initial character actually occurs in the message.  Each entry keeps
    ``(lowered, original)`` so matches still surface the display casing.
    """

    buckets: dict[str, list[tuple[str, str]]] = {}
    for name in names:
        lowered = name.casefold()
        if lowered:
            buckets.setdefault(lowered[0], []).append((lowered, name))
    return {
        first: tuple(sorted(items, key=lambda pair: len(pair[0]), reverse=True))
        for first, items in buckets.items()
    }


def _find_mentions(
    content_lowered: str,
    buckets: dict[str, tuple[tuple[str, str], ...]],
    limit: int = 3,
) -> tuple[str, ...]:
    """Distinct catalog names occurring in the pre-lowered text.

    One message can legitimately mention several items; every distinct hit
    counts (capped at ``limit``) instead of only the first.
    """

    found: list[str] = []
    for position, character in enumerate(content_lowered):
        for lowered_name, original in buckets.get(character, ()):
            if content_lowered.startswith(lowered_name, position) and original not in found:
                found.append(original)
                if len(found) >= limit:
                    return tuple(found)
    return tuple(found)


def _matches_any(content: str, names: tuple[str, ...]) -> str | None:
    """Single-shot compatibility matcher; prefer a prebuilt bucket index."""

    buckets = _name_match_index(names)
    hits = _find_mentions(content.casefold(), buckets)
    return hits[0] if hits else None


async def observe_once(*, now: float | None = None) -> int:
    """Run the rule-based observation pass; return insights created."""

    if not learning_enabled():
        return 0
    current = int(time.time() if now is None else now)
    since = current - _OBSERVATION_WINDOW_SECONDS
    messages = await _recent_messages(since)
    usage_inputs = await _recent_usage_inputs(since)
    if not messages and not usage_inputs:
        return 0
    names = await _catalog_names()
    buckets = _name_match_index(names) if names else {}

    mentions: dict[tuple[int, int], Counter[str]] = defaultdict(Counter)
    query_topics: dict[tuple[int, int], Counter[str]] = defaultdict(Counter)
    style_scopes: set[tuple[int, int]] = set()
    for user_id, group_id, content in messages:
        if buckets:
            for matched in _find_mentions(content.casefold(), buckets):
                mentions[(user_id, group_id)][matched] += 1
        try:
            if parse_style_feedback(content) is not None:
                style_scopes.add((user_id, group_id))
        except Exception:
            continue
    for user_id, group_id, text in usage_inputs:
        if buckets:
            for matched in _find_mentions(text.casefold(), buckets):
                query_topics[(user_id, group_id)][matched] += 1

    created = 0
    # The pending quota is per scope: check it once per scope instead of once
    # per candidate name, and track consumption locally.
    for (user_id, group_id), counter in mentions.items():
        quota = await learning_insights.count_created_since(user_id, group_id, since=since)
        for name, count in counter.items():
            if count < _MIN_MENTIONS or quota >= _DAILY_PENDING_LIMIT:
                continue
            evidence = f"近 24 小时{'群内' if group_id else '私聊'} {count} 次提到 '{name}'"
            if (
                await learning_insights.create_pending(
                    user_id,
                    group_id,
                    "preference",
                    f"关注饰品：{name[:80]}",
                    evidence,
                )
                is not None
            ):
                created += 1
                quota += 1
    for (user_id, group_id), counter in query_topics.items():
        quota = await learning_insights.count_created_since(user_id, group_id, since=since)
        for name, count in counter.items():
            if count < _MIN_QUERIES or quota >= _DAILY_PENDING_LIMIT:
                continue
            evidence = f"近 24 小时{'群内' if group_id else '私聊'} {count} 次查询 '{name}'"
            if (
                await learning_insights.create_pending(
                    user_id,
                    group_id,
                    "knowledge",
                    f"常查价格：{name[:80]}",
                    evidence,
                )
                is not None
            ):
                created += 1
                quota += 1
    for user_id, group_id in style_scopes:
        if (
            await learning_insights.count_created_since(user_id, group_id, since=since)
            >= _DAILY_PENDING_LIMIT
        ):
            continue
        if (
            await learning_insights.create_pending(
                user_id,
                group_id,
                "style",
                "喜欢即时调整回答风格",
                "近 24 小时出现过风格反馈句（如“短一点”“温柔一点”）",
            )
            is not None
        ):
            created += 1
    return created


async def _reflection_model_call(
    messages: list[dict[str, str]],
    max_tokens: int,
) -> str:
    """Run one reflection model call under the shared LLM concurrency budget."""

    async with _LLM_SEMAPHORE:
        return await try_anthropic(_REFLECTION_SYSTEM_PROMPT, messages, max_tokens)


def _json_digest(content: str) -> list[dict[str, object]] | None:
    """Parse the reflection payload; tolerate a fenced code block."""

    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.info("Reflection JSON parse failed error=%s", exc)
        return None
    if not isinstance(parsed, list):
        return None
    cleaned: list[dict[str, object]] = []
    for item in parsed:
        if isinstance(item, dict) and all(key in item for key in ("kind", "content", "evidence")):
            cleaned.append(item)
    return cleaned or None


def _leaks_raw_text(item: dict[str, object], raw_messages: list[str]) -> bool:
    """Reject any summary that quotes a raw message fragment."""

    for field in ("content", "evidence"):
        value = str(item.get(field) or "")
        for raw in raw_messages:
            fragment = raw.strip()
            if len(fragment) < _RAW_TEXT_FRAGMENT_MIN:
                continue
            if fragment in value:
                return True
    return False


async def _scope_known_contents(
    user_id: int, group_id: int
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Existing pending+accepted contents per kind, for reflection dedup."""

    known: dict[str, list[str]] = defaultdict(list)
    for status in ("pending", "accepted"):
        for insight in await learning_insights.list_scope(user_id, group_id, status=status):
            known[insight.kind].append(insight.content)
    return tuple((str(kind), tuple(contents)) for kind, contents in known.items())


def _is_duplicate_item(
    item: dict[str, object],
    known: tuple[tuple[str, tuple[str, ...]], ...],
) -> bool:
    """True when a reflection item merely restates an existing impression."""

    kind = str(item.get("kind") or "")
    content = str(item.get("content") or "")
    for existing_kind, contents in known:
        if existing_kind != kind:
            continue
        for existing in contents:
            if text_similarity(content, existing) >= _DUPLICATE_SIMILARITY:
                return True
    return False


async def reflect_once(*, now: float | None = None) -> int:
    """Run the daily LLM reflection; return pending insights created."""

    if not learning_enabled():
        return 0
    current = int(time.time() if now is None else now)
    # "Yesterday" follows the configured market timezone and is a half-open
    # window [local midnight - 1d, local midnight) so today's messages are
    # never included and consecutive days never overlap.
    local_midnight = to_market_tz(datetime.fromtimestamp(current, tz=timezone.utc)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    yesterday_start = int((local_midnight - timedelta(days=1)).timestamp())
    messages = await _recent_messages(yesterday_start, until=int(local_midnight.timestamp()))
    by_scope: dict[tuple[int, int], list[str]] = defaultdict(list)
    for user_id, group_id, content in messages:
        by_scope[(user_id, group_id)].append(content)

    created = 0
    model_calls = 0
    for (user_id, group_id), contents in sorted(by_scope.items()):
        if model_calls >= _REFLECTION_MAX_MODEL_CALLS:
            break
        if await learning_insights.pending_count(user_id, group_id) >= _REFLECTION_BACKLOG_LIMIT:
            continue
        bounded = contents[:_REFLECTION_MAX_MESSAGES_PER_USER]
        joined = "\n".join(f"- {line[:200]}" for line in bounded)
        if not joined:
            continue
        try:
            model_calls += 1
            output = await _reflection_model_call(
                [
                    {
                        "role": "user",
                        "content": f"昨日聊天片段（仅用于提炼，不要引用原文）：\n{joined}",
                    }
                ],
                900,
            )
        except Exception as exc:
            logger.warning("Reflection model call failed error=%s", type(exc).__name__)
            continue
        digest = _json_digest(output)
        if digest is None:
            continue
        known = await _scope_known_contents(user_id, group_id)
        for item in digest[:_REFLECTION_MAX_ITEMS_PER_USER]:
            if _leaks_raw_text(item, bounded):
                logger.info("Reflection item dropped for quoting raw text")
                continue
            if _is_duplicate_item(item, known):
                logger.info("Reflection item dropped as duplicate impression")
                continue
            raw_confidence = item.get("confidence")
            confidence = (
                int(raw_confidence)
                if isinstance(raw_confidence, int) and not isinstance(raw_confidence, bool)
                else 1
            )
            if (
                await learning_insights.create_pending(
                    user_id,
                    group_id,
                    str(item.get("kind") or "knowledge"),
                    str(item.get("content") or "")[:300],
                    str(item.get("evidence") or "每日反思")[:400],
                    confidence=confidence,
                    now=current,
                )
                is not None
            ):
                created += 1
    return created


async def decay_once(*, now: float | None = None) -> int:
    """Apply the daily forgetting curve; return archived count."""

    current = int(time.time() if now is None else now)
    return await learning_insights.decay(now=current)


def _terms(text: str) -> tuple[str, ...]:
    """Extract searchable terms: lowercase ASCII words plus CJK bigrams.

    Knowledge summaries mix Chinese prose with ASCII market names, so plain
    substring containment against these terms gives a dependency-free,
    zero-LLM match for both the ``塔菲知识`` command and reinforcement.
    """

    lowered = text.casefold()
    terms: list[str] = [match.group(0) for match in _ASCII_TERM_RE.finditer(lowered)]
    terms.extend(
        first + second
        for first, second in zip(lowered, lowered[1:], strict=False)
        if _CJK_CHAR_RE.match(first) and _CJK_CHAR_RE.match(second)
    )
    return tuple(dict.fromkeys(terms))


def match_score(query: str, content: str) -> int:
    """Count overlapping terms between a question and one knowledge summary."""

    query_lowered = query.casefold()
    content_lowered = content.casefold()
    score = sum(1 for term in _terms(query) if term in content_lowered)
    score += sum(1 for term in _terms(content) if term in query_lowered)
    return score


async def reinforce_once(*, now: float | None = None) -> int:
    """Reinforce accepted knowledge the user asked about again; return bumps.

    Runs at most once per day (the scheduler claims a day-bucketed lease), so
    counting audit rows from the last 24 hours cannot double-apply.  Each
    recurring mention raises confidence by one point, capped at five.
    """

    if not learning_enabled():
        return 0
    current = int(time.time() if now is None else now)
    insights, _total = await learning_insights.admin_list(
        status="accepted", limit=_REINFORCE_MAX_INSIGHTS
    )
    bumped = 0
    for insight in insights:
        if insight.kind != "knowledge" or insight.confidence >= _KNOWLEDGE_MAX_CONFIDENCE:
            continue
        terms = _terms(insight.content)
        if not terms:
            continue
        if not await _usage_mentions_any(insight.user_id, terms, current):
            continue
        if await learning_insights.bump_confidence(insight.id, now=current):
            bumped += 1
    return bumped


async def _usage_mentions_any(
    user_id: int,
    terms: tuple[str, ...],
    current: int,
) -> bool:
    """Return whether any recent audited input from the user hits a term."""

    try:
        rows = await database.read(
            """
            SELECT input_text FROM usage_log
            WHERE user_id = ? AND created_at >= ? AND created_at <= ?
            """,
            (user_id, current - _REINFORCE_WINDOW_SECONDS, current),
        )
    except Exception as exc:
        logger.warning("Reinforce usage read failed error=%s", type(exc).__name__)
        return False
    for row in rows:
        lowered = str(row["input_text"]).casefold()
        if any(term in lowered for term in terms):
            return True
    return False


def learning_enabled() -> bool:
    return env_bool("TAFFY_LEARNING_ENABLED", True)


def consolidation_enabled() -> bool:
    return env_bool("TAFFY_MEMORY_CONSOLIDATION_ENABLED", False)


def memory_write_decision_enabled() -> bool:
    return env_bool("TAFFY_MEMORY_WRITE_DECISION_ENABLED", False) and learning_enabled()


def text_similarity(left: str, right: str) -> float:
    """Normalized similarity between two memory texts (0..1)."""

    a = left.strip().casefold()
    b = right.strip().casefold()
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


async def record_insight_hit(insight_id: int, *, now: int | None = None) -> bool:
    """Reinforce an accepted insight because retrieval actually used it."""

    if not learning_enabled():
        return False
    try:
        return await learning_insights.record_hit(insight_id, now=now)
    except Exception as exc:
        logger.warning("Insight hit recording failed error=%s", type(exc).__name__)
        return False


async def consolidate_once(*, now: float | None = None) -> dict[str, int]:
    """Nightly sleep-time pass: merge, protect and (optionally) link.

    Merge: near-duplicate accepted impressions of the same kind collapse
    into the older row (the newer is soft-deleted as ``deprecated``).
    Protect: knowledge at maximum confidence stops decaying entirely.
    Link: one bounded model call per scope suggests cross-insight relations
    that are stored for auditable associative recall.  Every step is
    deterministic or validation-gated; nothing here surfaces to the user
    without the existing review commands.
    """

    if not learning_enabled() or not consolidation_enabled():
        return {"merged": 0, "protected": 0, "links": 0}
    current = int(time.time() if now is None else now)
    insights, _total = await learning_insights.admin_list(status="accepted", limit=1000)
    by_scope: dict[tuple[int, int], list] = defaultdict(list)
    for insight in insights:
        by_scope[(insight.user_id, insight.scope_group_id)].append(insight)

    merged = 0
    protected = 0
    links = 0
    link_scopes = 0
    for (user_id, scope), items in sorted(by_scope.items()):
        # Protection first: max-confidence knowledge is stable semantic memory
        # and stops decaying entirely.
        for insight in items:
            if (
                insight.kind == "knowledge"
                and insight.confidence >= _KNOWLEDGE_MAX_CONFIDENCE
                and not insight.is_protected
            ):
                await _protect_insight(insight.id, now=current)
                protected += 1

        # Deterministic merge: same-kind near-duplicates collapse into the
        # older row so the knowledge base stops accumulating restatements.
        by_kind: dict[str, list] = defaultdict(list)
        for insight in items:
            by_kind[insight.kind].append(insight)
        for kind_items in by_kind.values():
            ordered = sorted(kind_items, key=lambda item: item.id)
            for index, keeper in enumerate(ordered):
                if keeper.status != "accepted":
                    continue
                for duplicate in ordered[index + 1 :]:
                    if duplicate.status != "accepted":
                        continue
                    if text_similarity(keeper.content, duplicate.content) >= _MERGE_SIMILARITY:
                        if await learning_insights.supersede(
                            duplicate.id, user_id, scope, now=current
                        ):
                            boosted = min(
                                _MERGE_WEIGHT_CEILING, keeper.weight + _MERGE_WEIGHT_BOOST
                            )
                            await learning_insights.merge_update(
                                keeper.id, weight=boosted, now=current
                            )
                            merged += 1

        # Associative links: one bounded, validation-gated call per scope.
        if link_scopes >= _CONSOLIDATION_MAX_SCOPES:
            continue
        link_scopes += 1
        links += await _suggest_links(user_id, scope, now=current)
    return {"merged": merged, "protected": protected, "links": links}


async def _protect_insight(insight_id: int, *, now: int) -> None:
    """Mark one insight as protected (exempt from decay)."""

    try:
        await database.write(
            "UPDATE learning_insights SET is_protected = 1, updated_at = ? WHERE id = ?",
            (now, insight_id),
        )
    except Exception as exc:
        logger.warning("Insight protection failed error=%s", type(exc).__name__)


async def _suggest_links(user_id: int, scope: int, *, now: int) -> int:
    """Ask the model for cross-insight relations; store validated ones only."""

    insights, _total = await learning_insights.admin_list(
        user_id=user_id, status="accepted", limit=_CONSOLIDATION_MAX_INSIGHTS_PER_SCOPE
    )
    scope_items = [item for item in insights if item.scope_group_id == scope]
    if len(scope_items) < 2:
        return 0
    valid_ids = {item.id for item in scope_items}
    listing = "\n".join(f"{item.id}. {item.content}" for item in scope_items)
    try:
        async with _LLM_SEMAPHORE:
            output = await try_anthropic(
                _LINK_SYSTEM_PROMPT,
                [{"role": "user", "content": f"已确认印象列表：\n{listing}"}],
                600,
            )
    except Exception as exc:
        logger.warning("Link suggestion model call failed error=%s", type(exc).__name__)
        return 0
    try:
        parsed = json.loads(output.strip())
    except json.JSONDecodeError:
        logger.info("Link suggestion JSON parse failed")
        return 0
    if not isinstance(parsed, list):
        return 0
    created = 0
    for entry in parsed[:_CONSOLIDATION_MAX_LINKS_PER_SCOPE]:
        if not isinstance(entry, dict):
            continue
        raw_from = entry.get("from")
        raw_to = entry.get("to")
        if raw_from is None or raw_to is None:
            continue
        try:
            from_id = int(raw_from)
            to_id = int(raw_to)
            relation = str(entry.get("relation") or "").strip()
        except (TypeError, ValueError):
            continue
        if from_id not in valid_ids or to_id not in valid_ids or from_id == to_id:
            continue
        if not 2 <= len(relation) <= 60:
            continue
        try:
            if await learning_insights.add_link(
                user_id,
                scope,
                from_insight_id=from_id,
                to_insight_id=to_id,
                relation=relation,
                now=now,
            ):
                created += 1
        except Exception as exc:
            logger.warning("Memory link insert failed error=%s", type(exc).__name__)
    return created


async def memory_write_decision(
    user_id: int,
    scope_group_id: int,
    content: str,
    *,
    now: float | None = None,
) -> tuple[str, int | None, str | None]:
    """mem0-style write decision for one explicit ``记住`` request.

    Returns ``(decision, target_id, merged_content)`` where decision is
    ``add`` (no similar memory), ``update`` (rewrite ``target_id`` with
    ``merged_content``) or ``noop`` (already known).  Any failure — model
    unavailable, malformed output, validation error — falls back to
    ``add`` so an explicit user request is never silently lost.
    """

    if not memory_write_decision_enabled():
        return ("add", None, None)
    from ..state.persona_memory_repository import (
        MAX_MEMORY_CHARS,
        PersonaScope,
    )
    from ..state.runtime import persona_memories

    scope = PersonaScope(user_id, scope_group_id)
    try:
        memories = await persona_memories.list(scope)
    except Exception as exc:
        logger.warning("Memory decision candidate scan failed error=%s", type(exc).__name__)
        return ("add", None, None)
    candidates = [
        memory
        for memory in memories
        if memory.kind == "note"
        and text_similarity(content, memory.content) >= _DECISION_SIMILARITY
    ]
    candidates.sort(key=lambda memory: text_similarity(content, memory.content), reverse=True)
    candidates = candidates[:_DECISION_MAX_CANDIDATES]
    if not candidates:
        return ("add", None, None)
    listing = "\n".join(f"{memory.id}. {memory.content}" for memory in candidates)
    try:
        async with _LLM_SEMAPHORE:
            output = await try_anthropic(
                _MEMORY_DECISION_SYSTEM_PROMPT,
                [
                    {
                        "role": "user",
                        "content": (
                            f"用户新记的内容：{content[:MAX_MEMORY_CHARS]}\n\n"
                            f"已存在的相似记忆：\n{listing}"
                        ),
                    }
                ],
                300,
            )
    except Exception as exc:
        logger.warning("Memory decision model call failed error=%s", type(exc).__name__)
        return ("add", None, None)
    try:
        parsed = json.loads(output.strip())
        decision = str(parsed.get("decision") or "").strip().casefold()
        target_id = int(parsed.get("target_id")) if parsed.get("target_id") is not None else None
        merged = str(parsed.get("content") or "").strip()
    except (json.JSONDecodeError, TypeError, ValueError):
        logger.info("Memory decision JSON parse failed")
        return ("add", None, None)
    valid_targets = {memory.id for memory in candidates}
    if decision == "noop":
        return ("noop", None, None)
    if decision == "update" and target_id in valid_targets and merged:
        if len(merged) > MAX_MEMORY_CHARS:
            logger.info("Memory decision merged content rejected as too long")
            return ("add", None, None)
        return ("update", target_id, merged)
    return ("add", None, None)


__all__ = [
    "consolidate_once",
    "consolidation_enabled",
    "decay_once",
    "learning_enabled",
    "match_score",
    "memory_write_decision",
    "memory_write_decision_enabled",
    "observe_once",
    "record_insight_hit",
    "reflect_once",
    "reinforce_once",
    "text_similarity",
]
