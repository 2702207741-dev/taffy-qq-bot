"""Fetch the public SteamDT summary into an atomically replaced JSON cache."""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from src.services.config import steamdt_cache_path

PROJECT_ROOT = Path(__file__).resolve().parent


def _cache_file_path() -> Path:
    """Resolve the cache dynamically for tests and explicit process overrides."""

    return steamdt_cache_path()


def _number(value: str) -> float:
    try:
        multiplier = 10_000 if "万" in value else 1
        clean = value.replace("万", "").replace(",", "").replace("件", "").strip()
        return float(clean) * multiplier
    except (TypeError, ValueError):
        return 0.0


def _next(lines: list[str], index: int) -> str | None:
    return lines[index] if 0 <= index < len(lines) else None


def parse_page_text(text: str) -> dict[str, Any]:
    """Extract only the fields consumed by the bot from visible page text."""

    result: dict[str, Any] = {"success": True}
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if line.startswith(("连跌", "连涨")):
            result["trend_label"] = line
            if value := _next(lines, index + 1):
                result["index"] = _number(value)
            if raw := _next(lines, index + 2):
                match = re.match(r"([+-]?[\d.]+)([+-][\d.]+)%?", raw.strip())
                if match:
                    result["change_val"] = float(match.group(1))
                    result["change_pct"] = float(match.group(2))

        if "饰品成交额环比" in line:
            result["turnover_change"] = re.sub(r".*?(↓|↑)", r"\1", line).strip()
            if value := _next(lines, index + 1):
                result["turnover"] = value
            if (value := _next(lines, index + 2)) and "昨日" in value:
                result["turnover_yest"] = value.replace("昨日", "").strip()

        if line.startswith("饰品成交量"):
            result["volume_change"] = re.sub(r".*?(↓|↑)", r"\1", line).strip()
            if value := _next(lines, index + 1):
                result["volume"] = value
            if (value := _next(lines, index + 2)) and "昨日" in value:
                result["volume_yest"] = value.replace("昨日", "").strip()

        if "饰品存世量" in line and (value := _next(lines, index + 1)):
            result["total_items"] = value

        if "当前在线人数" in line:
            if value := _next(lines, index + 1):
                result["online_now"] = value
            if (value := _next(lines, index + 2)) and "昨日" in value:
                result["online_yest"] = value.replace("昨日", "").strip()
    return result


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as file:
            temporary = file.name
            json.dump(value, file, ensure_ascii=False, separators=(",", ":"))
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def scrape() -> dict[str, Any]:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(
                "https://www.steamdt.com/",
                timeout=30_000,
                wait_until="domcontentloaded",
            )
            page.wait_for_timeout(8_000)
            result = parse_page_text(page.inner_text("body"))
        finally:
            browser.close()
    _atomic_write_json(_cache_file_path(), result)
    return result


def main() -> int:
    try:
        result = scrape()
    except Exception as exc:
        print(
            json.dumps({"success": False, "error": type(exc).__name__}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
