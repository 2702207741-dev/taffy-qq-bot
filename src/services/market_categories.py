"""Pure market-name category predicates for ranking filters."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

_KNIFE_NAMES = (
    "Karambit",
    "Butterfly Knife",
    "M9 Bayonet",
    "Bayonet",
    "Flip Knife",
    "Gut Knife",
    "Huntsman Knife",
    "Falchion Knife",
    "Bowie Knife",
    "Shadow Daggers",
    "Talon Knife",
    "Ursus Knife",
    "Navaja Knife",
    "Stiletto Knife",
    "Skeleton Knife",
    "Survival Knife",
    "Paracord Knife",
    "Classic Knife",
    "Nomad Knife",
    "Kukri Knife",
)

_GLOVE_NAMES = (
    "Sport Gloves",
    "Driver Gloves",
    "Hand Wraps",
    "Moto Gloves",
    "Specialist Gloves",
    "Hydra Gloves",
    "Bloodhound Gloves",
)

_WEAR_SUFFIXES = (
    "(Factory New)",
    "(Minimal Wear)",
    "(Field-Tested)",
    "(Well-Worn)",
    "(Battle-Scarred)",
)

_WEAPON_ALIASES = {
    "ak": "AK-47",
    "ak47": "AK-47",
    "m4": "M4A4",
    "m4a1": "M4A1-S",
    "awp": "AWP",
    "沙鹰": "Desert Eagle",
    "沙漠之鹰": "Desert Eagle",
    "usp": "USP-S",
    "格洛克": "Glock-18",
    "爪子刀": "Karambit",
    "蝴蝶刀": "Butterfly Knife",
    "m9": "M9 Bayonet",
    "刺刀": "Bayonet",
}

# A specific-weapon fallback only accepts ASCII weapon-name shapes; anything
# else (Chinese gibberish, punctuation) is an unrecognized category.
_WEAPON_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9 .&'()\-]*")


@dataclass(frozen=True, slots=True)
class CategorySpec:
    label: str
    matcher: Callable[[str], bool]


def _is_knife(name: str) -> bool:
    probe = name.removeprefix("StatTrak™ ")
    return probe.startswith("★ ") and any(knife in name for knife in _KNIFE_NAMES)


def _is_gloves(name: str) -> bool:
    probe = name.removeprefix("StatTrak™ ")
    return probe.startswith("★ ") and any(glove in name for glove in _GLOVE_NAMES)


_AGENT_PREFIXES = ("'", "Special Agent", "Sir Bloody", "Cmdr.", "Number K", "Twofer")


def _is_agent(name: str) -> bool:
    return name.startswith(_AGENT_PREFIXES)


def _is_sticker(name: str) -> bool:
    return name.startswith("Sticker ")


def _is_music_kit(name: str) -> bool:
    return name.startswith("Music Kit")


def _is_weapon(name: str) -> bool:
    if _is_knife(name) or _is_gloves(name):
        return False
    if name.startswith(("★ ", "'", "Sticker ", "Music Kit", "Patch ", "Sealed Graffiti")):
        return False
    if " | " not in name:
        return False
    return name.endswith(_WEAR_SUFFIXES)


def _weapon_prefix_matcher(weapon: str) -> Callable[[str], bool]:
    def matcher(name: str) -> bool:
        return name.startswith(
            (
                f"{weapon} |",
                f"StatTrak™ {weapon} |",
                f"★ {weapon} |",
                f"StatTrak™ ★ {weapon} |",
            )
        )

    return matcher


def _is_miscellaneous(name: str) -> bool:
    return not (
        _is_knife(name)
        or _is_gloves(name)
        or _is_agent(name)
        or _is_sticker(name)
        or _is_music_kit(name)
        or _is_weapon(name)
    )


_MACRO_CATEGORIES: dict[str, CategorySpec] = {
    keyword: CategorySpec(label, matcher)
    for keyword, label, matcher in (
        *[(key, "刀具", _is_knife) for key in ("刀", "刀具", "匕首")],
        *[(key, "手套", _is_gloves) for key in ("手套",)],
        *[(key, "探员", _is_agent) for key in ("探员", "特工")],
        *[(key, "印花", _is_sticker) for key in ("印花", "贴纸")],
        *[(key, "音乐盒", _is_music_kit) for key in ("音乐盒",)],
        *[(key, "武器", _is_weapon) for key in ("武器", "枪械")],
        *[(key, "其他", _is_miscellaneous) for key in ("其他", "杂项")],
    )
}

_CATEGORY_HINT = (
    "刀/刀具、手套、探员、印花/贴纸、音乐盒、武器/枪械、其他，或具体枪名（如 AK-47、AWP）"
)


def resolve_category(query: str) -> CategorySpec | None:
    """Resolve a user category keyword into a label plus name matcher."""

    cleaned = query.strip()
    if not cleaned:
        return None
    macro = _MACRO_CATEGORIES.get(cleaned)
    if macro is not None:
        return macro
    weapon = _WEAPON_ALIASES.get(cleaned.casefold(), cleaned)
    if " | " in weapon or len(weapon) > 40 or _WEAPON_NAME_PATTERN.fullmatch(weapon) is None:
        return None
    return CategorySpec(weapon, _weapon_prefix_matcher(weapon))


def category_hint() -> str:
    return _CATEGORY_HINT


__all__ = ["CategorySpec", "category_hint", "resolve_category"]
