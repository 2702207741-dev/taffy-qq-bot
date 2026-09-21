"""Fail-closed SMTP side channel for inventory alerts (best-effort delivery).

QQ delivery semantics are untouched: the mail path is an asynchronous bypass
that never blocks or fails a QQ send.  Every knob comes from the environment;
when ``SMTP_HOST`` is empty the whole module disables itself, and repeated
failures back off after at most two attempts so a dead relay is never bombed.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import Message as MimeMessage
from email.utils import formatdate
from typing import Callable

from .config import runtime_setting

logger = logging.getLogger("taffy.services.notify_email")

SMTP_HOST_ENV = "SMTP_HOST"
SMTP_PORT_ENV = "SMTP_PORT"
SMTP_USER_ENV = "SMTP_USER"
SMTP_PASS_ENV = "SMTP_PASS"
MAIL_TO_ENV = "MAIL_TO"

DEFAULT_SMTP_PORT = 587
MAX_ATTEMPTS = 2
#: Linear backoff between attempts; short on purpose — the next monitor tick
#: re-queues anything still pending and doubling here adds little.
RETRY_BACKOFF_SECONDS = 5.0

_TIMEOUT_SECONDS = 15.0


def _setting(name: str) -> str:
    value = runtime_setting(name)
    return value.strip() if value else ""


@dataclass(frozen=True, slots=True)
class EmailSettings:
    """Snapshot of the env-configured relay; ``enabled`` gates everything."""

    enabled: bool
    host: str = ""
    port: int = DEFAULT_SMTP_PORT
    username: str = ""
    password: str = ""
    recipient: str = ""

    @classmethod
    def from_env(cls) -> EmailSettings:
        host = _setting(SMTP_HOST_ENV)
        if not host:
            return cls(enabled=False)
        port_raw = _setting(SMTP_PORT_ENV)
        try:
            port = int(port_raw) if port_raw else DEFAULT_SMTP_PORT
        except ValueError:
            logger.warning("Ignoring invalid %s; using %d", SMTP_PORT_ENV, DEFAULT_SMTP_PORT)
            port = DEFAULT_SMTP_PORT
        if not 1 <= port <= 65_535:
            logger.warning("Ignoring out-of-range %s=%s", SMTP_PORT_ENV, port_raw)
            port = DEFAULT_SMTP_PORT
        recipient = _setting(MAIL_TO_ENV)
        return cls(
            enabled=bool(recipient),
            host=host,
            port=port,
            username=_setting(SMTP_USER_ENV),
            password=_setting(SMTP_PASS_ENV),
            recipient=recipient,
        )


def build_mime_message(
    sender: str,
    recipient: str,
    subject: str,
    body: str,
) -> MimeMessage:
    message = MimeMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=False, usegmt=True)
    # UTF-8 with quoted-printable keeps CJK bodies intact without sending raw
    # 8bit payloads through strict relays.
    message["Content-Type"] = "text/plain; charset=utf-8"
    message["Content-Transfer-Encoding"] = "quoted-printable"
    import quopri

    message.set_payload(quopri.encodestring(body[:3_800].encode("utf-8")))
    return message


class EmailNotifier:
    """Asynchronous best-effort sender around :mod:`smtplib` in a worker thread."""

    def __init__(
        self,
        settings: EmailSettings | None = None,
        *,
        smtp_factory: Callable[..., smtplib.SMTP] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._settings = settings if settings is not None else EmailSettings.from_env()
        self._smtp_factory = smtp_factory or smtplib.SMTP
        self._sleep = asyncio.sleep
        if clock is not None:  # pragma: no cover - reserved for tests
            self._clock = clock

    @property
    def enabled(self) -> bool:
        return self._settings.enabled

    async def send_text(self, subject: str, body: str) -> bool:
        """Deliver one message with at most :data:`MAX_ATTEMPTS` tries.

        Returns whether some attempt succeeded.  Disabled or misconfigured
        channels fail immediately without touching the network.
        """

        if not self._settings.enabled:
            return False
        last_error = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                await asyncio.to_thread(self._deliver_blocking, subject, body)
                return True
            except Exception as exc:
                # Log the exception class only: bodies/tokens must never leak.
                last_error = type(exc).__name__
                logger.warning(
                    "Email notify attempt failed error=%s attempt=%d",
                    last_error,
                    attempt,
                )
            if attempt < MAX_ATTEMPTS:
                await self._sleep(RETRY_BACKOFF_SECONDS)
        logger.error("Email notify gave up after attempts=%d (%s)", MAX_ATTEMPTS, last_error)
        return False

    def _deliver_blocking(self, subject: str, body: str) -> None:
        settings = self._settings
        sender = settings.username or f"taffy@{settings.host}"
        message = build_mime_message(sender, settings.recipient, subject, body[:3_800])
        client = self._smtp_factory(settings.host, settings.port, timeout=_TIMEOUT_SECONDS)
        try:
            client.ehlo()
            if settings.port == 465:
                context = ssl.create_default_context()
                client.starttls(context=context)
            else:
                client.starttls()
            client.ehlo()
            if settings.username:
                client.login(settings.username, settings.password)
            client.sendmail(sender, [settings.recipient], message.as_string())
        finally:
            try:
                client.quit()
            except Exception:  # pragma: no cover - best-effort teardown
                pass


_notifier: EmailNotifier | None = None


def shared_email_notifier() -> EmailNotifier:
    """Process-wide notifier built once from the environment."""

    global _notifier
    if _notifier is None:
        _notifier = EmailNotifier()
    return _notifier


MAX_BODY_CHARS = 3_800


def inventory_alert_subject(nickname: str) -> str:
    return f"[囤货雷达] {nickname} 库存变化"


__all__ = [
    "EmailNotifier",
    "EmailSettings",
    "MAIL_TO_ENV",
    "MAX_ATTEMPTS",
    "SMTP_HOST_ENV",
    "shared_email_notifier",
]
