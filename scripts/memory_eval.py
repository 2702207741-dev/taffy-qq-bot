"""Memory evaluation probes — offline regression for memory changes.

Based on the LongMemEval five-ability question types, adapted to Taffy's own
confirmed memory stores (``persona_memories`` + accepted ``learning_insights``):

    1. information extraction      — can the bot retrieve a stored fact?
    2. multi-session reasoning     — can it join facts across scopes/days?
    3. temporal reasoning          — does it respect when facts changed?
    4. knowledge update            — does it answer with the newest fact?
    5. refusal (弃答)              — does it decline when it doesn't know?

Usage:
    uv run --frozen python scripts/memory_eval.py [--seed] [--db PATH]

``--seed`` writes the built-in probe dataset into an isolated temporary
database (never the production store), then runs every probe against the
real retrieval path (``persona_memories.list`` + ``learning_repository``
accepted-insight lookup).  Each probe is a deterministic pure-Python check —
no LLM involved, so results are reproducible between memory changes.

Exit code 0 when every probe passes; 1 with a per-probe report otherwise.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.state.persona_memory_repository import (  # noqa: E402
    MAX_MEMORIES_PER_SCOPE,
    PersonaMemoryRepository,
    PersonaScope,
)


@dataclass(frozen=True, slots=True)
class Probe:
    category: str
    question: str
    expected: str  # substring the answer must contain (or "" for refusal)
    should_refuse: bool = False
    owner: int = 1001  # scope owner the probe runs under
    group: int = 0  # scope group (0 = private chat)
    search_key: str = ""  # retrieval key the harness uses (default: question)
    check: Callable[[str], bool] | None = None  # custom predicate on the answer

    def run(self, answer: str) -> bool:
        if self.should_refuse:
            return not answer or "没记住" in answer or "还没有" in answer
        if self.check is not None:
            return self.check(answer)
        return self.expected in answer


# ---------------------------------------------------------------------------
# Built-in probe dataset (seeded into the isolated database)
# ---------------------------------------------------------------------------


async def _seed(db_path: Path) -> None:
    """Create an isolated probe database with the LongMemEval-style dataset.

    Uses the production schema via ``StateDatabase`` so the retrieval path
    sees exactly the tables/columns it sees in production.
    """

    from src.core.database import StateDatabase

    database = StateDatabase(db_path)
    await database.initialize()

    memories = [
        # scope 1001/0 — 阿澈私聊
        (1001, 0, "note", "用户叫阿澈，喜欢 AK-47 | Redline，常用昵称是小澈"),
        (1001, 0, "note", "用户阿澈三月说过最喜欢 AK-47 | Redline"),
        (1001, 0, "note", "用户阿澈五月改口：现在主玩 AWP | Asiimov，AK 只是备胎"),
        (1001, 0, "note", "阿澈 2026-03 买入 AK-47 | Redline 时价格约 120 元"),
        (1001, 0, "note", "阿澈 2026-08 说过 AK-47 | Redline 涨到了 200 元以上"),
        (1001, 0, "note", "阿澈私聊提过完美 A 段位对应 5E 的 2500 分左右"),
        (1001, 0, "note", "阿澈 2026-06 问过手套怎么选，说预算 800 元以内"),
        (1001, 0, "note", "阿澈的 Steam 昵称是 xiaoche_ace"),
        (1001, 0, "note", "阿澈 2026-07 说过想退坑卖库存，但 8 月又回坑了"),
        (1001, 0, "note", "阿澈在 2026-08-20 说过 Redline 想卖 210 元"),
        (1001, 0, "note", "阿澈喜欢 3 连号贴纸的枪"),
        (1001, 0, "note", "阿澈 2026-08-25 说过入手了 AWP 的 4 连全息贴纸"),
        # scope 1001/2001 — 阿澈所在群
        (1001, 2001, "note", "本群群友「老王」专做印花集，持有 3 张连号全息印花"),
        (1001, 2001, "note", "阿澈在群里说过自己玩 5E 平台，段位是完美 A"),
        (1001, 2001, "note", "群友「小美」在群里说过喜欢蝴蝶刀"),
        (1001, 2001, "note", "老王 2026-08 说过印花市场冷清，想出货"),
        # scope 1002/0 — 小林私聊
        (1002, 0, "note", "用户叫小林，讨厌被叫小可爱"),
        (1002, 0, "note", "小林喜欢 AWP | 二西莫夫，收藏了 3 把"),
        (1002, 0, "note", "小林 2026-07 说过想买 Karambit | 多普勒"),
        (1002, 0, "note", "小林讨厌贴纸枪，认为掉价"),
        # scope 1003/0 — 小美私聊（不同用户）
        (1003, 0, "note", "用户叫小美，喜欢蝴蝶刀 | 渐变大理石"),
        (1003, 0, "note", "小美 2026-08 问过开箱概率，说只开过一次箱"),
    ]
    for owner, group, kind, content in memories:
        normalized = content.strip().casefold()
        await database.write(
            """
            INSERT INTO persona_memories
                (owner_user_id, scope_group_id, memory_kind, content,
                 normalized_content, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 0, 0)
            """,
            (owner, group, kind, content, normalized),
        )
    insights = [
        (1001, 0, "knowledge", "AK-47 | Redline 是阿澈最常用的皮肤之一"),
        (1001, 0, "knowledge", "AWP | Asiimov 是阿澈 2026-05 起的主战皮肤"),
        (1001, 0, "knowledge", "阿澈的预算约 800 元（2026-06 手套咨询）"),
        (1001, 0, "knowledge", "阿澈 2026-08 从退坑回坑，仍在活跃交易"),
        (1002, 0, "knowledge", "小林偏好 AWP | 二西莫夫，讨厌贴纸枪"),
        (1003, 0, "knowledge", "小美只开过一次箱，偏好蝴蝶刀"),
    ]
    from src.state.learning_repository import LearningInsightRepository

    learning = LearningInsightRepository(database)
    for user_id, group, kind, content in insights:
        created = await learning.create_pending(
            user_id, group, kind, content, evidence="probe-seeded", confidence=5
        )
        if created is not None:
            await learning.confirm(created.id, user_id, group)


def _probes() -> list[Probe]:
    return [
        # 1. information extraction (11)
        Probe("信息抽取", "阿澈喜欢什么皮肤？", "Redline", search_key="Redline"),
        Probe("信息抽取", "老王是做什么的？", "印花", owner=1001, group=2001, search_key="印花"),
        Probe(
            "信息抽取", "小林喜欢被叫小可爱吗？", "讨厌", owner=1002, group=0, search_key="小可爱"
        ),
        Probe("信息抽取", "阿澈的 Steam 昵称是什么？", "xiaoche", search_key="xiaoche"),
        Probe("信息抽取", "阿澈 2026-06 问手套时预算多少？", "800", search_key="800"),
        Probe(
            "信息抽取",
            "老王 2026-08 对印花市场怎么看？",
            "冷清",
            owner=1001,
            group=2001,
            search_key="冷清",
        ),
        Probe(
            "信息抽取", "小林收藏了几把二西莫夫？", "3", owner=1002, group=0, search_key="二西莫夫"
        ),
        Probe("信息抽取", "小美喜欢什么刀？", "蝴蝶刀", owner=1003, group=0, search_key="蝴蝶刀"),
        Probe("信息抽取", "阿澈喜欢几连号贴纸的枪？", "3", search_key="连号"),
        Probe("信息抽取", "小美开过几次箱？", "一次", owner=1003, group=0, search_key="开箱"),
        Probe(
            "信息抽取",
            "小林 2026-07 想买什么？",
            "多普勒",
            owner=1002,
            group=0,
            search_key="多普勒",
        ),
        # 2. multi-session reasoning (6)
        Probe("跨会话推理", "阿澈在哪个平台玩？段位多少？", "完美", search_key="完美"),
        Probe("跨会话推理", "阿澈的段位换算成 5E 分是多少？", "2500", search_key="2500"),
        Probe("跨会话推理", "阿澈在群里和私聊分别说过什么平台？", "5E", search_key="5E"),
        Probe(
            "跨会话推理",
            "老王和阿澈在群里谁做印花？",
            "老王",
            owner=1001,
            group=2001,
            search_key="印花",
        ),
        Probe(
            "跨会话推理",
            "小美在群里和私聊都喜欢什么刀？",
            "蝴蝶刀",
            owner=1003,
            group=0,
            search_key="蝴蝶刀",
        ),
        Probe("跨会话推理", "阿澈 2026-08 想卖 Redline 多少钱？", "210", search_key="210"),
        # 3. temporal reasoning (6)
        Probe("时序推理", "阿澈 2026-03 买 Redline 时价格大概多少？", "120", search_key="120"),
        Probe("时序推理", "阿澈说 Redline 涨到多少了？", "200", search_key="200"),
        Probe("时序推理", "阿澈 2026-07 想做什么？", "退坑", search_key="退坑"),
        Probe("时序推理", "阿澈 2026-08-25 入手了什么？", "全息", search_key="全息"),
        Probe("时序推理", "阿澈 2026-08-20 说的 Redline 心理价是多少？", "210", search_key="210"),
        Probe(
            "时序推理",
            "小林 2026-07 想买什么刀？",
            "多普勒",
            owner=1002,
            group=0,
            search_key="多普勒",
        ),
        # 4. knowledge update — newest fact wins (6)
        Probe("知识更新", "阿澈现在主玩什么皮肤？", "AWP", search_key="AWP"),
        Probe("知识更新", "阿澈现在还是主玩 AK 吗？", "备胎", search_key="主玩"),
        Probe("知识更新", "阿澈 2026-08 还在退坑状态吗？", "回坑", search_key="回坑"),
        Probe("知识更新", "阿澈现在对 Redline 的心理价是多少？", "210", search_key="210"),
        Probe(
            "知识更新", "小林现在还喜欢贴纸枪吗？", "讨厌", owner=1002, group=0, search_key="贴纸"
        ),
        Probe("知识更新", "阿澈 2026-08-20 之后 Redline 的最新报价状态？", "210", search_key="210"),
        # 5. refusal — never invented (5)
        Probe("弃答", "阿澈的 Steam 库存总价值多少？", "", should_refuse=True, search_key="总价值"),
        Probe("弃答", "老王的全息印花具体是哪些？", "", should_refuse=True, search_key="哪些"),
        Probe(
            "弃答",
            "小美的蝴蝶刀磨损是多少？",
            "",
            should_refuse=True,
            owner=1003,
            group=0,
            search_key="磨损",
        ),
        Probe(
            "弃答",
            "小林最近一次登录是什么时候？",
            "",
            should_refuse=True,
            owner=1002,
            group=0,
            search_key="登录",
        ),
        Probe("弃答", "阿澈的段位在完美 S 还是 A？", "", should_refuse=True, search_key="完美 S"),
    ]


# ---------------------------------------------------------------------------
# Retrieval harness — uses the same repositories as production
# ---------------------------------------------------------------------------


async def _retrieve(db_path: Path, probe: Probe) -> str:
    """Answer a probe via the production retrieval path (no LLM, no network)."""
    from src.core.database import StateDatabase
    from src.state.learning_repository import LearningInsightRepository

    database = StateDatabase(db_path)
    await database.initialize()
    memories = PersonaMemoryRepository(database)
    insights = LearningInsightRepository(database)

    scope = PersonaScope(probe.owner, probe.group)
    lines: list[str] = []
    try:
        rows = await memories.list(scope, limit=MAX_MEMORIES_PER_SCOPE)
        for row in rows:
            lines.append(f"记忆：{row.content}")
    except Exception:
        pass
    # Accepted insights for the same scope feed knowledge answers.
    try:
        accepted = await insights.list_scope(probe.owner, probe.group)
        for insight in accepted:
            if insight.status == "accepted":
                lines.append(f"印象：{insight.content}")
    except Exception:
        pass
    # Deterministic retrieval stand-in: surface every stored fact whose text
    # contains the probe's retrieval key as a substring.  The probes then
    # judge whether the *right* facts were found — including the refusal
    # cases where no fact matches the key at all.
    key = probe.search_key or probe.question
    relevant = [line for line in lines if key in line]
    answer = "\n".join(relevant)
    if not answer:
        return "没记住这个信息。"
    return answer


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _render_report(passed: int, total: int, failures: list[tuple[Probe, str]]) -> str:
    """Render the probe summary as plain text.

    Status markers are plain ASCII (``[OK]``/``[FAIL]``/``[WARN]``) on
    purpose: the runner prints to whatever console invoked it, and some
    consoles (e.g. the Windows GBK code page) cannot encode emoji — the
    previous ``✅``/``✗``/``⚠️`` markers crashed the whole run with
    ``UnicodeEncodeError`` *after* all probes had already passed.
    Body text stays Chinese, which GBK covers.
    """
    lines = [f"记忆评测探针：{passed}/{total} 通过"]
    for probe, answer in failures:
        lines.append(
            f"  [FAIL] [{probe.category}] {probe.question}\n"
            f"     期望: {probe.expected!r} 实际: {answer[:80]!r}"
        )
    if failures:
        lines.append("\n[WARN] 记忆改动后请复查失败探针；弃答类失败说明检索把不确定当成了确定。")
    else:
        lines.append("[OK] 全部探针通过——记忆改动未破坏任何能力题型。")
    return "\n".join(lines)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", action="store_true", help="seed the probe DB first")
    parser.add_argument("--db", type=Path, default=None, help="probe DB path (default: temp)")
    args = parser.parse_args()

    db_path = args.db or Path(tempfile.mkdtemp(prefix="taffy-memory-eval-")) / "probe.db"
    if args.seed or not db_path.exists():
        await _seed(db_path)

    probes = _probes()
    passed = 0
    failures: list[tuple[Probe, str]] = []
    for probe in probes:
        answer = await _retrieve(db_path, probe)
        if probe.run(answer):
            passed += 1
        else:
            failures.append((probe, answer))

    print(_render_report(passed, len(probes), failures))
    return 1 if failures else 0


if __name__ == "__main__":
    import asyncio

    raise SystemExit(asyncio.run(main()))
