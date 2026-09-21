"""通知中心：事件规则（开关/免打扰/聚合）+ windows-toasts 回退托盘气泡。"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

_log = logging.getLogger("taffy.desktop.notify")

EVENT_KINDS = ("alert_delivered", "alert_failed", "job_failed", "new_reddit", "watchlist_low")
AGGREGATION_WINDOW_SECONDS = 300.0
DEFAULT_RULES: dict[str, dict[str, Any]] = {
    kind: {"toast": True, "sound": False, "dnd_start": None, "dnd_end": None}
    for kind in EVENT_KINDS
}


@dataclass
class Notification:
    kind: str
    title: str
    body: str
    epoch: int
    delivered: bool = False
    suppressed: bool = False


def _parse_hhmm(value: Any) -> tuple[int, int] | None:
    if not isinstance(value, str) or ":" not in value:
        return None
    try:
        hour, minute = value.split(":", 1)
        parsed = (int(hour), int(minute))
    except ValueError:
        return None
    if 0 <= parsed[0] <= 23 and 0 <= parsed[1] <= 59:
        return parsed
    return None


def _minutes_now(epoch: int) -> int:
    local = time.localtime(epoch)
    return local.tm_hour * 60 + local.tm_min


class NotificationCenter:
    """规则驱动的桌面通知；发送失败回退到内部列表（托盘气泡由壳层补发）。"""

    def __init__(
        self,
        rules: dict[str, dict[str, Any]] | None = None,
        *,
        aggregation_seconds: float = AGGREGATION_WINDOW_SECONDS,
        clock: Any = time.time,
    ) -> None:
        self._rules = {kind: dict(DEFAULT_RULES[kind]) for kind in EVENT_KINDS}
        for kind, rule in (rules or {}).items():
            if kind in self._rules and isinstance(rule, dict):
                merged = dict(self._rules[kind])
                merged.update({key: rule[key] for key in merged if key in rule})
                self._rules[kind] = merged
        self._aggregation_seconds = float(aggregation_seconds)
        self._clock = clock
        self.history: list[Notification] = []
        self._recent: dict[str, list[float]] = {}
        self._sender = self._windows_toasts_sender()

    # ── 发送通道 ────────────────────────────────────────

    @staticmethod
    def _windows_toasts_sender() -> Any:
        """Return a toast sender, or None when windows-toasts is unusable."""

        try:
            from windows_toasts import InteractableWindowsToaster, Toast

            toaster = InteractableWindowsToaster("塔菲桌面工作台")
        except Exception:  # noqa: BLE001 - optional dependency on non-Windows
            return None

        def sender(notification: Notification) -> bool:
            toast = Toast(text_fields=[notification.title, notification.body])
            toaster.show_toast(toast)
            return True

        return sender

    def _send(self, notification: Notification) -> bool:
        sender = self._sender
        if sender is None:
            return False
        try:
            return bool(sender(notification))
        except Exception as exc:  # noqa: BLE001 - any toast failure degrades silently
            _log.warning("Toast send failed error=%s", type(exc).__name__)
            return False

    # ── 规则 ────────────────────────────────────────────

    def update_rules(self, rules: dict[str, dict[str, Any]]) -> None:
        for kind, rule in rules.items():
            if kind in self._rules and isinstance(rule, dict):
                merged = dict(self._rules[kind])
                merged.update({key: rule[key] for key in merged if key in rule})
                self._rules[kind] = merged

    def rules(self) -> dict[str, dict[str, Any]]:
        return {kind: dict(rule) for kind, rule in self._rules.items()}

    def _in_dnd(self, kind: str, epoch: int) -> bool:
        rule = self._rules.get(kind, {})
        start = _parse_hhmm(rule.get("dnd_start"))
        end = _parse_hhmm(rule.get("dnd_end"))
        if start is None or end is None:
            return False
        minutes = _minutes_now(epoch)
        start_minutes = start[0] * 60 + start[1]
        end_minutes = end[0] * 60 + end[1]
        if start_minutes <= end_minutes:
            return start_minutes <= minutes < end_minutes
        # 跨午夜窗口（如 23:00-08:00）。
        return minutes >= start_minutes or minutes < end_minutes

    def _aggregate(self, kind: str, epoch: float) -> int:
        """Return how many earlier events this one merges (>=0 means merge)."""

        window = [
            stamp
            for stamp in self._recent.get(kind, [])
            if epoch - stamp < self._aggregation_seconds
        ]
        window.append(epoch)
        self._recent[kind] = window[-50:]
        return len(window) - 1

    def publish(self, kind: str, title: str, body: str) -> Notification:
        epoch = int(self._clock())
        if kind not in EVENT_KINDS:
            raise ValueError(f"unknown event kind: {kind}")
        rule = self._rules.get(kind, {})
        pending = Notification(kind=kind, title=title, body=body, epoch=epoch)
        merged_count = self._aggregate(kind, float(epoch))
        if merged_count > 0:
            pending.body = f"{pending.body}（等 {merged_count} 条同类通知）"
        if not rule.get("toast", True) or self._in_dnd(kind, epoch):
            pending.suppressed = True
            self.history.append(pending)
            return pending
        pending.delivered = self._send(pending)
        self.history.append(pending)
        del self.history[:-200]
        return pending
