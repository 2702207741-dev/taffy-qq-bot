from __future__ import annotations

import unittest

from scrape_steamdt import _number, parse_page_text


class SteamDtParserTests(unittest.TestCase):
    def test_extracts_market_summary(self) -> None:
        result = parse_page_text(
            """
            连涨 2天
            123.45
            +1.25+1.02%
            饰品成交额环比↑3%
            ¥10万
            昨日 ¥9万
            饰品成交量↓2%
            2万件
            昨日 2.1万件
            当前在线人数
            100万
            昨日 90万
            """
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["index"], 123.45)
        self.assertEqual(result["change_pct"], 1.02)
        self.assertEqual(result["turnover"], "¥10万")
        self.assertEqual(result["turnover_yest"], "¥9万")
        self.assertEqual(result["turnover_change"], "↑3%")
        self.assertEqual(result["volume"], "2万件")
        self.assertEqual(result["volume_yest"], "2.1万件")
        self.assertEqual(result["volume_change"], "↓2%")

    def test_number_parser_uses_wan_as_multiplier(self) -> None:
        self.assertEqual(_number("2.5万件"), 25_000)
        self.assertEqual(_number("not-a-number"), 0)


if __name__ == "__main__":
    unittest.main()
