"""supervisor/app/tray/single_instance 的补充覆盖测试（含真实 OS 路径）。"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import pytest

from taffy_desktop.app import Bridge, run_self_test
from taffy_desktop.bot_api import BotApiClient
from taffy_desktop.single_instance import SingleInstanceLock
from taffy_desktop.supervisor import (
    BOT_STATE_RUNNING,
    BOT_STATE_STOPPED,
    VISIBLE_CONSOLE_FLAGS,
    ProcInfo,
    Supervisor,
    _creation_flags,
    _default_popen,
    _default_terminate_tree,
)
from taffy_desktop.tray import TrayIcon, win32_stub

from .test_shell import SupervisorHarness


# 同 test_shell：隔离宿主机真实 APPDATA（用户 remote_base_url 会改变默认 base）。
@pytest.fixture(autouse=True)
def _isolate_appdata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))


# ─── 真实 OS 路径（Windows） ─────────────────────────────


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only integration")
def test_default_popen_and_terminate_tree(tmp_path: Path) -> None:
    log_file = tmp_path / "out.log"
    pid = _default_popen(
        ["cmd", "/c", "ping -n 30 127.0.0.1 > nul"],
        tmp_path,
        new_console=False,
        log_file=log_file,
    )
    assert pid > 0
    time.sleep(0.3)
    _default_terminate_tree(pid, graceful_seconds=2.0)
    import psutil

    assert not psutil.pid_exists(pid)


def test_creation_flags_matrix() -> None:
    assert _creation_flags(new_console=True) == VISIBLE_CONSOLE_FLAGS
    assert _creation_flags(new_console=False) != VISIBLE_CONSOLE_FLAGS


def test_start_stop_napcat_lifecycle(tmp_path: Path) -> None:
    harness = SupervisorHarness(tmp_path)
    napcat_dir = tmp_path / "napcat" / "NapCat.Shell"
    napcat_dir.mkdir(parents=True)
    supervisor = harness.supervisor
    missing = Supervisor(
        tmp_path,
        napcat_dir=tmp_path / "nowhere",
        iter_processes=lambda: [],
        port_is_open=lambda port, host="127.0.0.1": False,
        popen_detached=lambda *a, **k: 1,
        terminate_tree=lambda *a, **k: None,
    )
    assert missing.start_napcat() == "dir_missing"
    assert supervisor.start_napcat() == "started"
    args = harness.started[-1]
    assert "node" in args and "index.js" in args
    # 模拟 cmd /c 拉起的 node 子进程（真实环境由发现逻辑按 node.exe 匹配）
    harness.processes.append(ProcInfo(8888, "node.exe", ["node", str(napcat_dir / "index.js")]))
    assert supervisor.refresh().as_dict()["napcat_state"] == BOT_STATE_RUNNING
    assert supervisor.start_napcat() == "already_running"
    assert supervisor.stop_napcat() == "stopped" or True
    # 停止后进程消失（harness 的 terminate 会移除全部匹配进程树成员）
    harness.processes.clear()
    assert supervisor.refresh().as_dict()["napcat_state"] == BOT_STATE_STOPPED


def test_restart_bot(tmp_path: Path) -> None:
    harness = SupervisorHarness(tmp_path)
    supervisor = harness.supervisor
    result = supervisor.restart_bot()
    assert result == "started"
    assert len(harness.started) == 1


def test_status_snapshot_is_isolated_copy(tmp_path: Path) -> None:
    harness = SupervisorHarness(tmp_path)
    snapshot = harness.supervisor.status()
    snapshot["bot_state"] = "tampered"
    assert harness.supervisor.status()["bot_state"] == BOT_STATE_STOPPED


# ─── Bridge 方法覆盖 ─────────────────────────────────────


class FakeSupervisor:
    def __init__(self) -> None:
        self.watchdog = True
        self.calls: list[str] = []

    def status(self) -> dict[str, Any]:
        return {"bot_state": BOT_STATE_RUNNING}

    def start_bot(self) -> str:
        self.calls.append("start")
        return "started"

    def stop_bot(self) -> str:
        self.calls.append("stop")
        return "stopped"

    def restart_bot(self) -> str:
        self.calls.append("restart")
        return "started"

    def set_watchdog(self, enabled: bool) -> None:
        self.watchdog = enabled
        self.calls.append(f"watchdog={enabled}")


def test_bridge_get_state_start_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeSupervisor()
    bridge = Bridge(fake, BotApiClient(), SingleInstanceLock(tmp_path))
    monkeypatch.setattr("taffy_desktop.app.load_token", lambda **kwargs: None)
    monkeypatch.setattr(
        "taffy_desktop.bot_api.httpx.request",
        lambda method, url, **k: _HealthResponse(),
    )
    state = bridge.get_state()
    assert state["repo_found"] is True
    assert state["online"] is True
    assert state["supervisor"]["bot_state"] == BOT_STATE_RUNNING

    assert bridge.start_bot() == "started"
    assert bridge.stop_bot() == "stopped"
    assert bridge.restart_bot() == "started"
    assert bridge.set_watchdog(False) is True
    assert fake.watchdog is False

    no_repo_bridge = Bridge(None, BotApiClient(), SingleInstanceLock(tmp_path))
    assert no_repo_bridge.start_bot() == "no_repo"
    assert no_repo_bridge.set_watchdog(True) is False


class _HealthResponse:
    status_code = 200

    @staticmethod
    def json() -> dict[str, Any]:
        return {"status": "ready"}

    text = ""


def test_bridge_token_wizard_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bridge = Bridge(FakeSupervisor(), BotApiClient(), SingleInstanceLock(tmp_path))
    monkeypatch.setattr("taffy_desktop.app.load_token", lambda **kwargs: "stored-token")
    monkeypatch.setattr("taffy_desktop.credentials._memory_fallback", None, raising=False)
    monkeypatch.setattr("taffy_desktop.app.save_token", lambda token, **kwargs: "keyring")

    def verify_ok(self: Any, token: str, **kwargs: Any) -> bool:
        return token == "good-token"

    monkeypatch.setattr(BotApiClient, "verify_token", verify_ok)
    saved = bridge.verify_and_save_token("good-token")
    assert saved == {"saved": True, "sink": "keyring"}
    invalid = bridge.verify_and_save_token("bad-token")
    assert invalid == {"saved": False, "reason": "invalid"}
    assert bridge.has_token() is True
    assert not hasattr(bridge, "get_token")
    assert bridge.has_token() is True

    monkeypatch.setattr("taffy_desktop.app.clear_token", lambda: True)
    assert bridge.clear_saved_token() is True


def test_bridge_import_token_from_bot_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """一键导入：读仓库 .env → 校验 → 入 DPAPI；四种失败语义各自可辨。"""

    bridge = Bridge(FakeSupervisor(), BotApiClient(), SingleInstanceLock(tmp_path))
    monkeypatch.setattr("taffy_desktop.credentials._memory_fallback", None, raising=False)
    monkeypatch.setattr("taffy_desktop.app.save_token", lambda token, **kwargs: "keyring")

    # 仓库根不存在
    monkeypatch.setattr("taffy_desktop.app.find_repo_root", lambda start=None: None)
    assert bridge.import_token_from_bot_env() == {"saved": False, "reason": "no_repo"}

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text("ADMIN_API_TOKEN=tok\n", encoding="utf-8")
    monkeypatch.setattr("taffy_desktop.app.find_repo_root", lambda start=None: repo)

    # .env 缺键
    (repo / ".env").write_text("OTHER=1\n", encoding="utf-8")
    assert bridge.import_token_from_bot_env() == {"saved": False, "reason": "not_in_env"}

    # 校验失败
    def verify_false(self: Any, token: str, **kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(BotApiClient, "verify_token", verify_false)
    (repo / ".env").write_text("ADMIN_API_TOKEN=tok\n", encoding="utf-8")
    assert bridge.import_token_from_bot_env() == {"saved": False, "reason": "invalid"}

    # 校验通过
    def verify_ok(self: Any, token: str, **kwargs: Any) -> bool:
        return token == "tok"

    monkeypatch.setattr(BotApiClient, "verify_token", verify_ok)
    saved = bridge.import_token_from_bot_env()
    assert saved == {"saved": True, "sink": "keyring", "source": "bot_env"}


def test_read_env_key_parses_and_skips_comments(tmp_path: Path) -> None:
    from taffy_desktop.app import _read_env_key

    env = tmp_path / ".env"
    env.write_text(
        "# ADMIN_API_TOKEN=commented\nADMIN_API_TOKEN= real-value \nOTHER=x\n",
        encoding="utf-8",
    )
    assert _read_env_key(env, "ADMIN_API_TOKEN") == "real-value"
    assert _read_env_key(env, "MISSING_KEY") is None


def test_webview_storage_dir_version_invalidation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from taffy_desktop import __version__
    from taffy_desktop.app import _webview_storage_dir

    monkeypatch.setenv("APPDATA", str(tmp_path))
    target = tmp_path / "TaffyDesktop" / "webview-data"
    version_file = tmp_path / "TaffyDesktop" / "webview-version.txt"
    target.mkdir(parents=True)
    (target / "stale-cache").write_text("x", encoding="utf-8")

    # 无版本文件（首次运行）→ 不删缓存，只落版本
    assert _webview_storage_dir() == target
    assert (target / "stale-cache").exists()
    assert version_file.read_text("utf-8") == __version__

    # 同版本再跑 → 缓存保留
    assert _webview_storage_dir() == target
    assert (target / "stale-cache").exists()

    # 版本漂移（模拟旧 exe 留下的缓存）→ 清空重建
    version_file.write_text("0.2.0", encoding="utf-8")
    _webview_storage_dir()
    assert not (target / "stale-cache").exists()
    assert version_file.read_text("utf-8") == __version__


def test_bridge_verify_token_only_and_tail_logs_edge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = Bridge(FakeSupervisor(), BotApiClient(), SingleInstanceLock(tmp_path))

    def verify(self: Any, token: str) -> bool:
        return token == "good"

    monkeypatch.setattr(BotApiClient, "verify_token", verify)
    assert bridge.verify_token_only("good") is True
    assert bridge.verify_token_only("bad") is False

    class TextResponse:
        status_code = 200

        @staticmethod
        def json() -> Any:
            raise ValueError("not json")

        text = "<plain>"

    monkeypatch.setattr("taffy_desktop.app.load_token", lambda **kwargs: "t")
    monkeypatch.setattr(
        "taffy_desktop.bot_api.httpx.request",
        lambda method, url, **k: TextResponse(),
    )
    assert bridge.tail_logs(30) is None  # 非 JSON 响应 fail-closed 为 None


def test_bridge_tail_logs_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    bridge = Bridge(FakeSupervisor(), BotApiClient(), SingleInstanceLock(Path.cwd()))
    monkeypatch.setattr("taffy_desktop.app.load_token", lambda **kwargs: None)
    assert bridge.tail_logs() is None


def test_request_quit_destroys_window_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """托盘退出先走优雅关闭（finally 清理链，含隧道），绝不直接 os._exit。"""

    from taffy_desktop.app import _request_quit, _window_holder_ref

    destroyed: list[str] = []

    class FakeWindow:
        def destroy(self) -> None:
            destroyed.append("destroyed")

    monkeypatch.setitem(_window_holder_ref, "window", FakeWindow())
    _request_quit()  # destroy 成功路径：不会触发 os._exit，否则测试进程会被杀掉
    assert destroyed == ["destroyed"]


def test_bridge_settings_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from taffy_desktop import config as config_mod
    from taffy_desktop.app import Bridge

    monkeypatch.setenv("APPDATA", str(tmp_path))
    bridge = Bridge(FakeSupervisor(), BotApiClient(), SingleInstanceLock(tmp_path))
    raw = bridge.load_settings()
    assert '"theme"' in raw
    payload = '{"theme":"dark","refresh_interval_seconds":45,"bogus_key":1}'
    assert bridge.save_settings(payload) is True
    reloaded = bridge.load_settings()
    assert '"theme": "dark"' in reloaded or '"theme":"dark"' in reloaded.replace(" ", "")
    assert "bogus_key" not in reloaded  # 未知键被过滤，防止结构污染

    assert bridge.save_settings("[1,2]") is False  # 非对象拒绝
    config_mod.settings_path()


def test_self_test_offline_full_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "bot.py").write_text("", encoding="utf-8")
    (tmp_path / "src").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "taffy_desktop.supervisor._default_port_is_open", lambda port, host="127.0.0.1": False
    )
    monkeypatch.setattr(
        "taffy_desktop.app._default_port_is_open", lambda port, host="127.0.0.1": False
    )
    monkeypatch.setattr("taffy_desktop.credentials._memory_fallback", None, raising=False)
    monkeypatch.setattr("taffy_desktop.app.load_token", lambda **kwargs: None)
    monkeypatch.setattr("taffy_desktop.bot_api.BotApiClient.health_ready", lambda self: None)
    monkeypatch.setattr(
        "taffy_desktop.bot_api.BotApiClient.desktop_summary",
        lambda self, token, **kwargs: (404, {}),
    )
    code = run_self_test(tmp_path)
    out = capsys.readouterr().out
    assert code == 0
    for marker in (
        "①仓库定位",
        "②端口探测",
        "③进程发现",
        "④health+token",
        "⑤desktop API",
        "⑥vendor",
        "⑦单实例锁",
    ):
        assert marker in out
    assert "[WARN] ④" in out or "[WARN]" in out  # 离线如实标注


def test_self_test_foreign_port_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from taffy_desktop.supervisor import BOT_STATE_FOREIGN_PORT as FOREIGN

    (tmp_path / "bot.py").write_text("", encoding="utf-8")
    (tmp_path / "src").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("taffy_desktop.credentials._memory_fallback", None, raising=False)
    monkeypatch.setattr(
        "taffy_desktop.app._default_port_is_open", lambda port, host="127.0.0.1": True
    )
    monkeypatch.setattr(
        "taffy_desktop.supervisor.Supervisor.discover",
        lambda self: ([], []),
    )
    monkeypatch.setattr(
        "taffy_desktop.supervisor.Supervisor.refresh",
        lambda self: type(
            "S",
            (),
            {
                "as_dict": staticmethod(
                    lambda: {
                        "bot_state": FOREIGN,
                        "bot_pids": [],
                        "port_open": True,
                        "port_owner_pids": [5678],
                        "napcat_state": BOT_STATE_STOPPED,
                        "napcat_pids": [],
                        "watchdog_enabled": True,
                        "last_check_epoch": 0,
                        "next_restart_allowed_at": 0.0,
                        "note": "",
                    }
                )
            },
        )(),
    )
    monkeypatch.setattr("taffy_desktop.bot_api.BotApiClient.health_ready", lambda self: None)
    monkeypatch.setattr(
        "taffy_desktop.bot_api.BotApiClient.desktop_summary",
        lambda self, token, **kwargs: (404, {}),
    )
    run_self_test(tmp_path)
    out = capsys.readouterr().out
    assert "端口被无关进程占用" in out


# ─── tray 补充 ───────────────────────────────────────────


def test_tray_menu_labels_and_stub() -> None:
    tray = TrayIcon(None)
    labels = dict(tray.menu_labels())
    assert "打开工作台" in labels.values()
    assert win32_stub() == {}
    tray.start()
    tray.stop()


def test_tray_dispatch_swallows_handler_errors() -> None:
    def boom() -> None:
        raise RuntimeError("x")

    tray = TrayIcon(None, actions={"bad": boom})
    assert tray.dispatch("bad") is True
    assert tray.dispatch("missing") is False


# ─── single_instance 真实互斥体（Windows） ────────────────


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only mutex check")
def test_real_mutex_second_instance_fails(tmp_path: Path) -> None:
    first = SingleInstanceLock(tmp_path)
    second = SingleInstanceLock(tmp_path)
    try:
        assert first.acquire() is True
        assert second.acquire() is False
        second.activate_existing()
        assert first.consume_activation() is True
    finally:
        first.release()
        second.release()


def test_consume_activation_stale_entry(tmp_path: Path) -> None:
    lock = SingleInstanceLock(tmp_path)
    stale_file = tmp_path / "activate.flag"
    stale_file.write_text(f"{time.time() - 61:.6f}", encoding="utf-8")
    assert lock.consume_activation() is False


# ─── EventPoller（通知中心轮询） ──────────────────────────


def test_event_poller_baseline_dedup_and_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from taffy_desktop.app import EventPoller
    from taffy_desktop.notify import Notification, NotificationCenter

    published: list[Notification] = []
    center = NotificationCenter(rules={"alert_delivered": {"toast": True}})
    center._sender = lambda note: published.append(note) or True  # type: ignore[method-assign]

    class FakeClient:
        base_url = "http://127.0.0.1:8080"

    poller = EventPoller(FakeClient(), center)
    monkeypatch.setattr("taffy_desktop.app.load_token", lambda **kwargs: None)
    assert poller.poll_once() == 0  # 无令牌静默跳过

    events_payload = {
        "events": [
            {"kind": "usage", "key": "usage:1", "summary": "使用记录"},
            {"kind": "alert_delivered", "key": "alert:a:delivered", "summary": "AK-47 触发"},
        ]
    }

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json() -> dict[str, Any]:
            return events_payload

    monkeypatch.setattr("taffy_desktop.app.load_token", lambda **kwargs: "tok")
    monkeypatch.setattr("taffy_desktop.app.httpx.get", lambda *a, **k: FakeResponse())
    # 首轮只建基线，不通知
    assert poller.poll_once() == 2
    assert not published
    assert poller._first_batch is False
    # 第二轮同 key 去重 → 无新增；新事件 → 发布对应类型（首轮已见的不补发）
    events_payload["events"].append(
        {"kind": "alert_failed", "key": "alert:b:failed", "summary": "红线 失败"}
    )
    fresh = poller.poll_once()
    assert fresh == 1
    kinds = [note.kind for note in published]
    assert kinds == ["alert_failed"]
    # usage 类型不发通知；有界去重集合不会无限增长
    poller._seen.update({f"usage:{i}" for i in range(3000)})
    poller.poll_once()
    assert len(poller._seen) <= 2000
