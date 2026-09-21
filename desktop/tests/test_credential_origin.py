"""Changing settings mid-request must not mix credential and destination origins."""

from pathlib import Path

import httpx
import pytest

from taffy_desktop import credentials
from taffy_desktop.app import Bridge
from taffy_desktop.bot_api import BotApiClient
from taffy_desktop.config import AppSettings, save_settings
from taffy_desktop.single_instance import SingleInstanceLock

FIRST = "https://first.example.com"
SECOND = "https://second.example.com"


@pytest.fixture
def bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Bridge:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr(credentials, "_memory_fallback", None)
    store: dict[tuple[str, str], str] = {}

    class Keyring:
        def set_password(self, service: str, account: str, value: str) -> None:
            store[service, account] = value

        def get_password(self, service: str, account: str) -> str | None:
            return store.get((service, account))

    monkeypatch.setattr(credentials, "_keyring", Keyring)
    save_settings(AppSettings(remote_base_url=FIRST))
    return Bridge(None, BotApiClient(), SingleInstanceLock(tmp_path))


def test_proxy_uses_one_origin_snapshot(bridge: Bridge, monkeypatch: pytest.MonkeyPatch) -> None:
    credentials.save_token("first-test-value", origin=FIRST)
    credentials.save_token("second-test-value", origin=SECOND)
    real_load = credentials.load_token

    def switch_before_loading(*, origin: str) -> str | None:
        save_settings(AppSettings(remote_base_url=SECOND))
        return real_load(origin=origin)

    def request(method: str, url: str, **kwargs: object) -> httpx.Response:
        assert url == FIRST + "/api/admin/status"
        assert kwargs["headers"] == {"Authorization": "Bearer first-test-value"}
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr("taffy_desktop.app.load_token", switch_before_loading)
    monkeypatch.setattr("taffy_desktop.app.httpx.request", request)
    assert bridge.http_request("GET", "/api/admin/status")["status"] == 200


def test_verification_saves_to_verified_origin(
    bridge: Bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    def request(method: str, url: str, **kwargs: object) -> httpx.Response:
        assert url == FIRST + "/api/admin/status"
        save_settings(AppSettings(remote_base_url=SECOND))
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr("taffy_desktop.bot_api.httpx.request", request)
    assert bridge.verify_and_save_token("candidate-test-value")["saved"] is True
    assert credentials.load_token(origin=FIRST) == "candidate-test-value"
    assert credentials.load_token(origin=SECOND) is None
