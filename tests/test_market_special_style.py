from __future__ import annotations

import pytest

from src.services.market_special_style import (
    parse_special_style_query,
    validated_special_style,
)


@pytest.mark.parametrize(
    ("query", "clean", "code"),
    (
        ("爪子刀 多普勒 红宝石 崭新", "爪子刀 多普勒 崭新", "ruby"),
        ("多普勒P2 M9 崭新", "多普勒 M9 崭新", "p2"),
        ("蝴蝶刀 多普勒 black pearl", "蝴蝶刀 多普勒", "blackpearl"),
        ("淬火 AK 单面全蓝 久经", "淬火 AK 久经", "singleblue"),
        ("渐变之色 一档 崭新", "渐变之色 崭新", "1st"),
        ("普通红线 久经", "普通红线 久经", None),
    ),
)
def test_special_style_parser_removes_only_documented_selector(
    query: str,
    clean: str,
    code: str | None,
) -> None:
    result = parse_special_style_query(query)
    assert result.clean_query == clean
    assert result.code == code


def test_special_style_validation_is_closed_to_documented_codes() -> None:
    assert validated_special_style("Ruby") == "ruby"
    assert validated_special_style(None) is None
    assert validated_special_style("made-up") is None
