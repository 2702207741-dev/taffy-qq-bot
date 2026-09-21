"""CLI regression tests for scripts/memory_eval.py console output.

The runner once crashed with ``UnicodeEncodeError`` on Windows GBK
consoles: the emoji markers (✅/✗/⚠️) are not GBK-encodable, so a
fully-passing run still exited non-zero at the final ``print``.
Report markers are plain ASCII by contract — these tests nail that.
"""

import io
import sys

from scripts import memory_eval


def _gbk_console() -> io.TextIOWrapper:
    """A strict GBK console, like a default Windows terminal."""
    return io.TextIOWrapper(io.BytesIO(), encoding="gbk", errors="strict")


def test_success_report_prints_on_gbk_console() -> None:
    report = memory_eval._render_report(34, 34, [])
    assert "[OK]" in report
    assert "34/34 通过" in report
    # Exact repro of the production crash: printing the old ✅ line here
    # raised UnicodeEncodeError after all probes had passed.
    with _gbk_console() as console:
        print(report, file=console)


def test_failure_report_is_gbk_safe_and_marked() -> None:
    probe = memory_eval.Probe(
        "弃答",
        "阿澈的库存价值多少？",
        "",
        should_refuse=True,
        search_key="总价值",
    )
    report = memory_eval._render_report(33, 34, [(probe, "某个错误答案")])
    assert "[FAIL]" in report
    assert "[WARN]" in report
    assert "33/34 通过" in report
    report.encode("gbk")  # raises UnicodeEncodeError on regression
    with _gbk_console() as console:
        print(report, file=console)


async def test_main_seed_end_to_end(tmp_path, monkeypatch, capsys) -> None:
    db = tmp_path / "probe.db"
    monkeypatch.setattr(sys, "argv", ["memory_eval.py", "--seed", "--db", str(db)])
    assert await memory_eval.main() == 0
    out = capsys.readouterr().out
    assert "34/34 通过" in out
    assert "[OK]" in out
