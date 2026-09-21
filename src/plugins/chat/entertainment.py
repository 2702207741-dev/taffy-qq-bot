"""Entertainment command implementations with no NoneBot side effects."""

from __future__ import annotations

import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from types import ModuleType

from .skin_data import SKIN_MAP

NameResolver = Callable[[str], Awaitable[str | None]]


@dataclass(frozen=True, slots=True)
class CaseItem:
    name: str
    rarity: str


@dataclass(frozen=True, slots=True)
class CaseDefinition:
    name: str
    items: tuple[CaseItem, ...]
    rare_specials: tuple[str, ...]


# A reviewed snapshot of real case contents parsed from CS2 game-data catalogs.
# Keeping a small embedded set makes the simulation deterministic and available
# offline; rarity is attached to the item instead of being drawn independently.
CASES = (
    CaseDefinition(
        "Operation Bravo Case",
        (
            CaseItem("SG 553 | Wave Spray", "blue"),
            CaseItem("Dual Berettas | Black Limba", "blue"),
            CaseItem("Nova | Tempest", "blue"),
            CaseItem("Galil AR | Shattered", "blue"),
            CaseItem("UMP-45 | Bone Pile", "blue"),
            CaseItem("G3SG1 | Demeter", "blue"),
            CaseItem("USP-S | Overgrowth", "purple"),
            CaseItem("M4A4 | Zirka", "purple"),
            CaseItem("MAC-10 | Graven", "purple"),
            CaseItem("M4A1-S | Bright Water", "purple"),
            CaseItem("P90 | Emerald Dragon", "pink"),
            CaseItem("P2000 | Ocean Foam", "pink"),
            CaseItem("AWP | Graphite", "pink"),
            CaseItem("AK-47 | Fire Serpent", "red"),
            CaseItem("Desert Eagle | Golden Koi", "red"),
        ),
        (
            "★ Bayonet",
            "★ Bayonet | Fade",
            "★ Bayonet | Slaughter",
            "★ Bayonet | Case Hardened",
            "★ Bayonet | Crimson Web",
        ),
    ),
    CaseDefinition(
        "Winter Offensive Weapon Case",
        (
            CaseItem("Galil AR | Sandstorm", "blue"),
            CaseItem("Five-SeveN | Kami", "blue"),
            CaseItem("M249 | Magma", "blue"),
            CaseItem("PP-Bizon | Cobalt Halftone", "blue"),
            CaseItem("FAMAS | Pulse", "purple"),
            CaseItem("Dual Berettas | Marina", "purple"),
            CaseItem("MP9 | Rose Iron", "purple"),
            CaseItem("Nova | Rising Skull", "purple"),
            CaseItem("M4A1-S | Guardian", "pink"),
            CaseItem("P250 | Mehndi", "pink"),
            CaseItem("AWP | Redline", "pink"),
            CaseItem("M4A4 | Asiimov", "red"),
            CaseItem("Sawed-Off | The Kraken", "red"),
        ),
        (
            "★ Bayonet",
            "★ Bayonet | Fade",
            "★ Bayonet | Slaughter",
            "★ Bayonet | Case Hardened",
            "★ Bayonet | Crimson Web",
        ),
    ),
    CaseDefinition(
        "CS:GO Weapon Case 3",
        (
            CaseItem("CZ75-Auto | Crimson Web", "blue"),
            CaseItem("P2000 | Red FragCam", "blue"),
            CaseItem("Dual Berettas | Panther", "blue"),
            CaseItem("USP-S | Stainless", "blue"),
            CaseItem("Glock-18 | Blue Fissure", "blue"),
            CaseItem("CZ75-Auto | Tread Plate", "purple"),
            CaseItem("Tec-9 | Titanium Bit", "purple"),
            CaseItem("Desert Eagle | Heirloom", "purple"),
            CaseItem("Five-SeveN | Copper Galaxy", "purple"),
            CaseItem("CZ75-Auto | The Fuschia Is Now", "pink"),
            CaseItem("P250 | Undertow", "pink"),
            CaseItem("CZ75-Auto | Victoria", "red"),
        ),
        (
            "★ Bayonet",
            "★ Bayonet | Fade",
            "★ Bayonet | Slaughter",
            "★ Bayonet | Case Hardened",
            "★ Bayonet | Crimson Web",
        ),
    ),
)
RARITIES = (
    ("gold", 0.0026),
    ("red", 0.0064),
    ("pink", 0.0320),
    ("purple", 0.1598),
    ("blue", 0.7992),
)
RARITY_LABELS = {
    "gold": ("🟡", "金"),
    "red": ("🔴", "红"),
    "pink": ("🟣", "粉"),
    "purple": ("🟣", "紫"),
    "blue": ("🔵", "蓝"),
}


def _random_skin(rng: ModuleType = random) -> str:
    alias, (weapon, skin) = rng.choice(list(SKIN_MAP.items()))
    return (
        f"🎲 随机皮肤（目录抽取）\n──────────\n{weapon} | {skin}\n昵称: {alias}\n"
        "──────────\n💡 查询价格时请同时补充武器和磨损"
    )


def _open_cases(message: str, rng: ModuleType = random) -> str:
    count = 10 if message.startswith("十连开") else 1
    case = rng.choice(CASES)
    lines = [f"📦 {'十连开箱' if count > 1 else '开箱模拟'}"]
    lines.append(f"武器箱: {case.name}")
    lines.append("──────────")

    for _ in range(count):
        roll = rng.random()
        accumulated = 0.0
        tier = "blue"
        for candidate, probability in RARITIES:
            accumulated += probability
            if roll <= accumulated:
                tier = candidate
                break

        if tier == "gold":
            item_name = rng.choice(case.rare_specials)
        else:
            candidates = [item.name for item in case.items if item.rarity == tier]
            item_name = rng.choice(candidates)
        emoji, label = RARITY_LABELS[tier]
        lines.append(f"{emoji} [{label}] {item_name}")

    lines.append("──────────")
    lines.append("概率: 蓝79.92% / 紫15.98% / 粉3.20% / 红0.64% / 金0.26%")
    lines.append("规则: 先抽稀有度，再从该箱对应稀有度物品中等概率抽取")
    lines.append("⚠️ 纯模拟，不消耗真实武器箱或钥匙，也不生成真实物品")
    return "\n".join(lines)


async def _alchemy(message: str, resolve_name: NameResolver, rng: ModuleType = random) -> str:
    query = message.replace("炼金", "").strip()
    skins = list(SKIN_MAP.items())
    if not query:
        materials = rng.sample(skins, 10)
        _, (weapon, skin) = rng.choice(skins)
        lines = ["🧪 娱乐炼金（非游戏合约，随机材料）\n──────────\n投入材料:"]
        lines.extend(
            f"  {material_weapon} | {material_skin}"
            for _, (material_weapon, material_skin) in materials
        )
        lines.append(f"──────────\n✨ 炼金结果: {weapon} | {skin}")
        lines.append("──────────\n⚠️ 幻想模拟，不复刻游戏合约的稀有度与收藏品规则")
        return "\n".join(lines)

    market_name = await resolve_name(query)
    if not market_name:
        aliases = [alias for alias in SKIN_MAP if query in alias]
        if not aliases:
            return f"未识别材料'{query}'，请用饰品昵称"
        market_name = aliases[0]

    weapon, separator, skin = market_name.partition(" | ")
    if not separator:
        weapon, skin = market_name, ""
    materials = [(query, (weapon, skin))]
    materials.extend(rng.sample([item for item in skins if item[0] != query], 9))
    _, (result_weapon, result_skin) = rng.choice(skins)

    lines = ["🧪 娱乐炼金（非游戏合约）\n──────────\n投入材料:"]
    lines.extend(
        f"  {material_weapon} | {material_skin}"
        for _, (material_weapon, material_skin) in materials
    )
    lines.append(f"──────────\n✨ 炼金结果: {result_weapon} | {result_skin}")
    lines.append("──────────\n⚠️ 幻想模拟，不复刻游戏合约的稀有度与收藏品规则")
    return "\n".join(lines)


async def handle_entertainment_command(
    message: str,
    *,
    resolve_name: NameResolver,
    rng: ModuleType = random,
) -> str | None:
    """Return an entertainment response, or ``None`` for unrelated input."""

    if message == "随机皮肤":
        return _random_skin(rng)
    if message.startswith(("开箱", "十连开")):
        return _open_cases(message, rng)
    if message.startswith("炼金"):
        return await _alchemy(message, resolve_name, rng)
    return None
