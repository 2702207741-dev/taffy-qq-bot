"""V0.5 night batch 7: MCP tool definition (zero-dependency JSON schema).

Validates that the published MCP schema is well-formed JSON, every listed
tool maps to a real desktop-API route, and the reference doc's skeleton is
present.  No MCP SDK involved — the contract is the JSON file itself.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "docs" / "mcp" / "taffy_market_tools.json"
REFERENCE_PATH = ROOT / "docs" / "mcp" / "MCP_REFERENCE.md"


def test_schema_is_valid_json_and_well_formed() -> None:
    payload = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert payload["protocol"] == "MCP 2025-06-18"
    assert payload["transport"] == "stdio (JSON-RPC 2.0)"
    tools = payload["tools"]
    assert len(tools) >= 10
    names = {tool["name"] for tool in tools}
    for tool in tools:
        assert tool["name"]
        assert tool["description"]
        assert "inputSchema" in tool
        assert "endpoint" in tool
    assert "market_item" in names
    assert "market_case_ev" in names
    assert "market_hoard" in names


def test_every_endpoint_exists_in_desktop_api() -> None:
    payload = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    desktop_source = (ROOT / "src" / "plugins" / "desktop_api.py").read_text(encoding="utf-8")
    for tool in payload["tools"]:
        endpoint = tool["endpoint"]
        route = endpoint.split("?")[0].split(" ", 1)[-1]  # strip method + query
        assert route in desktop_source, f"endpoint {route} not found in desktop_api.py"


def test_fail_closed_note_present() -> None:
    payload = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    notes = payload["notes"]
    assert "fail_closed" in notes
    assert "不编造" in notes["fail_closed"] or "补写" in notes["fail_closed"]


def test_reference_doc_has_stdio_skeleton() -> None:
    text = REFERENCE_PATH.read_text(encoding="utf-8")
    assert "JSON-RPC" in text
    assert "tools/list" in text
    assert "stdlib" in text
