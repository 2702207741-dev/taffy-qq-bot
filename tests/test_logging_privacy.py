"""Tests for the privacy logging sink (B6): exception text and Bearer redaction."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from src.core import logging_privacy


def _record(message: str, *, exception: Any = None, name: str = "taffy.test") -> dict[str, Any]:
    return {
        "name": name,
        "message": message,
        "exception": exception,
        "level": type("Level", (), {"no": 20, "name": "INFO"})(),
        "extra": {"nonebot_log_level": 20},
    }


def _exception_record(message: str) -> dict[str, Any]:
    try:
        raise RuntimeError(message)
    except RuntimeError as exc:
        exc_info = type(exc), exc, exc.__traceback__
    record = _record("call failed")
    record["exception"] = type(
        "RecordException", (), {"type": exc_info[0], "value": exc_info[1], "traceback": exc_info[2]}
    )()
    return record


def test_bearer_tokens_are_redacted_in_rendered_text() -> None:
    # Built at runtime so the scanner never sees a literal bearer token here.
    token = "abcdef0123456789" + "abcd"
    line = f"request failed with Authorization: Bearer {token}"
    redacted = logging_privacy._redact_text(line)
    assert token not in redacted
    assert "[redacted]" in redacted


def test_exception_tracebacks_are_redacted_before_rendering() -> None:
    token = "abcdef0123456789" + "abcd"
    record = _exception_record(f"upstream rejected Authorization=Bearer {token}")

    template = logging_privacy._redacting_format(record)

    assert "sanitized_exception" in template
    rendered = record["extra"]["sanitized_exception"]
    assert rendered != ""
    assert token not in rendered
    assert record["exception"] is None  # already consumed by the custom renderer


def test_records_without_exception_keep_empty_sanitized_block() -> None:
    record = _record("plain message")

    logging_privacy._redacting_format(record)

    assert record["extra"]["sanitized_exception"] == ""


def test_privacy_filter_still_redacts_plain_messages() -> None:
    record = _record("user_id=123456 requested 悠久の霊刀")

    assert logging_privacy._privacy_filter(record) is True
    assert "123456" not in str(record["message"])

    event = _record(
        "OneBot V11 Bot 123 -> [message.private]: 你好",
        name="nonebot.message",
    )
    assert logging_privacy._privacy_filter(event) is True
    assert str(event["message"]).startswith("OneBot message event received")


def test_same_record_rendered_by_two_sinks_keeps_the_traceback() -> None:
    """M-02：一个 record 按注册顺序交给每个 sink 渲染。

    控制台 sink 先渲染并把 ``record["exception"]`` 清空，文件 sink 随后渲染同
    一个 record。若渲染器把「已渲染」记在可变字段上，第二个 sink 就会拿到空
    字符串 —— 结果就是 ``taffy.log`` 永不含 traceback。这里显式渲染两次。
    """

    record = _exception_record("boom")

    logging_privacy._redacting_format(record)  # sink 1：控制台
    logging_privacy._redacting_format(record)  # sink 2：文件

    rendered = record["extra"]["sanitized_exception"]
    assert rendered != ""
    assert "Traceback" in rendered
    assert "RuntimeError" in rendered


def test_two_loguru_sinks_both_receive_the_traceback(tmp_path: Path) -> None:
    """把两次渲染放到真实的 loguru 双 sink 拓扑里再验证一遍。"""

    log_file = tmp_path / "taffy.log"
    sink_ids = [
        logging_privacy.logger.add(io.StringIO(), format=logging_privacy._redacting_format),
        logging_privacy.logger.add(log_file, format=logging_privacy._redacting_format),
    ]
    try:
        try:
            raise RuntimeError("probe 1234567890")
        except RuntimeError:
            logging_privacy.logger.exception("call failed")
    finally:
        for sink_id in sink_ids:
            logging_privacy.logger.remove(sink_id)

    text = log_file.read_text(encoding="utf-8")
    assert "Traceback" in text
    assert "RuntimeError" in text
    # 脱敏必须仍然生效：异常文本里的长数字不得落盘
    assert "1234567890" not in text
    assert "[redacted-id]" in text
