"""tunnel.py：软件托管 SSH 隧道（起/停/接管/断线重连），全注入式测试。"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from taffy_desktop.tunnel import TunnelManager


class FakeSshProcess:
    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True


class TunnelHarness:
    """可控的端口/进程世界，不触真实 ssh 与网络。"""

    def __init__(self, tmp_path: Path) -> None:
        self.open = False
        self.spawn_count = 0
        self.manager = TunnelManager(
            local_port=18080,
            remote_host="192.0.2.10",
            remote_user="example",
            remote_port=8080,
            key_file=tmp_path / "key",
            probe_interval=0.02,
        )
        self.manager.is_open = lambda: self.open  # type: ignore[method-assign]
        self.manager._spawn = self._fake_spawn  # type: ignore[method-assign]

    def _fake_spawn(self) -> None:
        self.spawn_count += 1
        self.manager._process = FakeSshProcess(4242)  # type: ignore[assignment]


def _wait_until(cond: Any, timeout: float = 2.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met in time")


def test_default_is_disabled_without_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Default configuration must not access network or start SSH")

    monkeypatch.setattr("taffy_desktop.tunnel.socket.create_connection", forbidden)
    monkeypatch.setattr("taffy_desktop.tunnel.subprocess.Popen", forbidden)
    manager = TunnelManager()
    assert manager.start() == "disabled"
    assert manager._thread is None
    assert manager.stop() is True


def test_start_spawns_when_port_closed(tmp_path: Path) -> None:
    harness = TunnelHarness(tmp_path)
    assert harness.manager.start() == "started"
    assert harness.spawn_count == 1
    assert harness.manager._process is not None
    harness.manager.stop()


def test_start_takes_over_existing_tunnel(tmp_path: Path) -> None:
    harness = TunnelHarness(tmp_path)
    harness.open = True
    # 已有监听（外部手开等）→ 不重复 spawn，仅接管统一托管
    assert harness.manager.start() == "already"
    assert harness.spawn_count == 0
    assert harness.manager._process is None
    harness.manager.stop()


def test_stop_terminates_managed_ssh(tmp_path: Path) -> None:
    harness = TunnelHarness(tmp_path)
    harness.manager.start()
    proc = harness.manager._process
    assert proc is not None and proc.terminated is False
    harness.manager.stop()
    assert proc.terminated is True


def test_watch_reconnects_after_drop(tmp_path: Path) -> None:
    harness = TunnelHarness(tmp_path)
    harness.open = True
    assert harness.manager.start() == "already"
    harness.open = False  # 模拟隧道断开
    _wait_until(lambda: harness.spawn_count >= 1)
    assert harness.manager._process is not None
    harness.manager.stop()


def test_watch_respawns_repeatedly_while_down(tmp_path: Path) -> None:
    harness = TunnelHarness(tmp_path)
    harness.manager.start()
    _wait_until(lambda: harness.spawn_count >= 2)
    assert harness.spawn_count >= 2
    harness.manager.stop()


def test_stop_stops_watch_thread(tmp_path: Path) -> None:
    harness = TunnelHarness(tmp_path)
    harness.manager.start()
    thread = harness.manager._thread
    assert thread is not None and thread.is_alive()
    assert harness.manager.stop() is True
    thread.join(timeout=1.0)
    assert thread.is_alive() is False


def test_missing_ssh_logs_warning_not_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("taffy_desktop.tunnel.shutil.which", lambda name: None)
    # 不走 fake spawn：which 返回 None 时应安全告警，不抛异常、不产生进程
    TunnelManager._spawn(TunnelHarness(tmp_path).manager)
    assert True
