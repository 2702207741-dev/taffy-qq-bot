from __future__ import annotations

from src.services.market_categories import category_hint, resolve_category


def _matches(query: str, name: str) -> bool:
    spec = resolve_category(query)
    assert spec is not None, f"category unresolved: {query}"
    return spec.matcher(name)


def test_macro_categories_classify_representative_names() -> None:
    assert _matches("刀具", "★ Karambit | Fade (Factory New)")
    assert _matches("刀", "StatTrak™ ★ Butterfly Knife | Crimson Web (Minimal Wear)")
    assert _matches("匕首", "★ M9 Bayonet | Doppler (Factory New)")
    assert not _matches("刀具", "★ Sport Gloves | Vice (Factory New)")

    assert _matches("手套", "★ Sport Gloves | Vice (Factory New)")
    assert _matches("手套", "★ Hand Wraps | Cobalt Skulls (Field-Tested)")
    assert not _matches("手套", "★ Karambit | Fade (Factory New)")

    assert _matches("探员", "'Blueberries' Buckshot | NSWC SEAL")
    assert _matches("特工", "'The Doctor' Romanov | Sabre")

    assert _matches("印花", "Sticker | Hello AK-47")
    assert _matches("贴纸", "Sticker | Hello AK-47 (Gold)")

    assert _matches("音乐盒", "Music Kit | Perfect World, 花脸 Hua Lian (Painted Face)")

    assert _matches("武器", "AK-47 | Redline (Field-Tested)")
    assert _matches("武器", "StatTrak™ M4A1-S | Printstream (Field-Tested)")
    assert not _matches("武器", "Sticker | Hello AK-47")
    assert not _matches("武器", "★ Karambit | Fade (Factory New)")
    assert not _matches("武器", "'Blueberries' Buckshot | NSWC SEAL")

    assert _matches("其他", "Operation Breakout Weapon Case")
    assert _matches("杂项", "Sealed Graffiti | Chibi (Frogly)")


def test_specific_weapon_prefix_and_aliases() -> None:
    assert _matches("AK-47", "AK-47 | Asiimov (Minimal Wear)")
    assert _matches("AK-47", "StatTrak™ AK-47 | Redline (Field-Tested)")
    assert _matches("AK-47", "AK-47 | 荣耀之巅")  # prefix match needs no wear suffix
    assert _matches("ak", "AK-47 | Asiimov (Minimal Wear)")
    assert _matches("ak47", "StatTrak™ AK-47 | Redline (Field-Tested)")
    assert _matches("m4", "M4A4 | Poseidon (Field-Tested)")
    assert _matches("m4a1", "M4A1-S | Printstream (Minimal Wear)")
    assert _matches("沙鹰", "Desert Eagle | Printstream (Field-Tested)")
    assert _matches("AWP", "AWP | Dragon Lore (Field-Tested)")
    assert _matches("爪子刀", "★ Karambit | Fade (Factory New)")
    assert _matches("蝴蝶刀", "★ Butterfly Knife | Fade (Factory New)")
    assert not _matches("AWP", "AK-47 | Redline (Field-Tested)")


def test_unresolvable_queries_return_none_and_hint_lists_options() -> None:
    assert resolve_category("") is None
    assert resolve_category("   ") is None
    assert resolve_category("AK-47 | Redline") is None  # injection guard
    assert resolve_category("x" * 41) is None
    assert resolve_category("不存在的分类xyz") is None  # non-ASCII is never a weapon
    assert resolve_category("AK-47;drop table") is None  # punctuation is not a weapon

    hint = category_hint()
    assert "刀具" in hint and "手套" in hint and "探员" in hint and "印花" in hint


def test_weapon_without_wear_suffix_still_matches_weapon_prefix() -> None:
    # Weapon prefix categories are name-prefix based; the wear suffix rule
    # only gates the broad "武器" macro category.
    assert _matches("AK-47", "AK-47 | 荣耀之巅")
    assert not _matches("武器", "AK-47 | 荣耀之巅")


def test_stattrak_knife_variants_classify_as_knives_not_weapons() -> None:
    name = "StatTrak™ ★ Butterfly Knife | Crimson Web (Minimal Wear)"
    assert _matches("刀具", name)
    assert not _matches("武器", name)
    assert _matches("蝴蝶刀", name)


def test_agent_classification_covers_named_prefixes() -> None:
    for name in (
        "'Blueberries' Buckshot | NSWC SEAL",
        "Special Agent Ava | FBI",
        "Sir Bloody Darryl Royale | The Professionals",
        "Cmdr. Mae 'Dead Cold' Jamison | SWAT",
        "Number K | The Professionals",
    ):
        assert _matches("探员", name), f"agent not matched: {name}"
        assert not _matches("武器", name), f"agent misclassified as weapon: {name}"

    # Non-agent items that share a token must not match.
    assert not _matches("探员", "MP5-SD | Agent (Battle-Scarred)")
    assert not _matches("探员", "Sticker | Agent Select")
    assert not _matches("探员", "★ Specialist Gloves | Field Agent (Factory New)")
