"""Parse documented SteamDT special-style selectors without guessing."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SpecialStyleQuery:
    clean_query: str
    code: str | None = None
    label: str | None = None


_STYLE_ALIASES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("blackpearl", "黑珍珠", ("黑珍珠", "black pearl", "blackpearl")),
    ("sapphire", "蓝宝石", ("蓝宝石", "sapphire")),
    ("emerald", "绿宝石", ("绿宝石", "emerald")),
    ("ruby", "红宝石", ("红宝石", "ruby")),
    ("singleblue", "单面全蓝", ("单面全蓝", "single blue", "singleblue")),
    ("sun", "官图太阳", ("官图太阳", "太阳款", "sun")),
    ("p1", "多普勒一阶段", ("多普勒一阶段", "一阶段", "阶段1", "phase 1", "p1")),
    ("p2", "多普勒二阶段", ("多普勒二阶段", "二阶段", "阶段2", "phase 2", "p2")),
    ("p3", "多普勒三阶段", ("多普勒三阶段", "三阶段", "阶段3", "phase 3", "p3")),
    ("p4", "多普勒四阶段", ("多普勒四阶段", "四阶段", "阶段4", "phase 4", "p4")),
    ("10th", "十档", ("十档", "10档", "10th")),
    ("9th", "九档", ("九档", "9档", "9th")),
    ("8th", "八档", ("八档", "8档", "8th")),
    ("7th", "七档", ("七档", "7档", "7th")),
    ("6th", "六档", ("六档", "6档", "6th")),
    ("5th", "五档", ("五档", "5档", "5th")),
    ("4th", "四档", ("四档", "4档", "4th")),
    ("3rd", "三档", ("三档", "3档", "3rd")),
    ("2nd", "二档", ("二档", "2档", "2nd")),
    ("1st", "一档", ("一档", "1档", "1st")),
)

SPECIAL_STYLE_LABELS = {code: label for code, label, _ in _STYLE_ALIASES}
SPECIAL_STYLE_CODES = frozenset(SPECIAL_STYLE_LABELS)


def _alias_pattern(alias: str) -> re.Pattern[str]:
    escaped = re.escape(alias).replace(r"\ ", r"\s*")
    if alias.isascii() and alias[-1:].isalnum():
        return re.compile(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", re.IGNORECASE)
    return re.compile(escaped, re.IGNORECASE)


def parse_special_style_query(query: str) -> SpecialStyleQuery:
    text = " ".join(query.strip().split())
    if not text:
        return SpecialStyleQuery("")
    for code, label, aliases in _STYLE_ALIASES:
        for alias in aliases:
            match = _alias_pattern(alias).search(text)
            if match is None:
                continue
            cleaned = " ".join((text[: match.start()] + " " + text[match.end() :]).split())
            return SpecialStyleQuery(cleaned, code, label)
    return SpecialStyleQuery(text)


def validated_special_style(value: str | None) -> str | None:
    if value is None:
        return None
    clean = value.strip().casefold()
    return clean if clean in SPECIAL_STYLE_CODES else None


__all__ = [
    "SPECIAL_STYLE_CODES",
    "SPECIAL_STYLE_LABELS",
    "SpecialStyleQuery",
    "parse_special_style_query",
    "validated_special_style",
]
