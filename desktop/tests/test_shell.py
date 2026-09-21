"""桌面壳测试：config/credentials/bot_api/supervisor/notify/tray/single_instance/app.self-test。"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest

from taffy_desktop import config as config_mod
from taffy_desktop import credentials as credentials_mod
from taffy_desktop.bot_api import BotApiClient
from taffy_desktop.config import (
    AppSettings,
    appdata_dir,
    find_repo_root,
    load_settings,
    normalize_base_url,
    resolve_base_url,
    save_settings,
)
from taffy_desktop.notify import NotificationCenter
from taffy_desktop.single_instance import SingleInstanceLock
from taffy_desktop.supervisor import (
    BOT_STATE_FOREIGN_PORT,
    BOT_STATE_RUNNING,
    BOT_STATE_STARTING,
    BOT_STATE_STOPPED,
    ProcInfo,
    Supervisor,
)
from taffy_desktop.tray import TrayIcon, build_tray_actions


# 隔离宿主机真实 %APPDATA% 设置：用户已把 remote_base_url 指向隧道时，
# BotApiClient 默认 base 会随之变化，URL 断言必须跑在空设置环境上。
@pytest.fixture(autouse=True)
def _isolate_appdata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))


# ─── config ─────────────────────────────────────────────


def test_settings_roundtrip(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    settings = AppSettings(theme="dark", refresh_interval_seconds=10)
    assert save_settings(settings, target) is True
    loaded = load_settings(target)
    assert loaded.theme == "dark"
    assert loaded.refresh_interval_seconds == 10


def test_load_settings_corrupt_falls_back_to_defaults(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    target.write_text("{not json", encoding="utf-8")
    loaded = load_settings(target)
    assert loaded.theme == "light"
    assert isinstance(loaded.dashboard_tiles, list)


def test_find_repo_root_walks_up(tmp_path: Path) -> None:
    (tmp_path / "bot.py").write_text("", encoding="utf-8")
    (tmp_path / "src").mkdir()
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert find_repo_root(nested) == tmp_path


def test_find_repo_root_none_when_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 隔离 cwd 与解释器路径，否则打包/venv 布局会向上命中真实仓库。
    fake_exe = tmp_path / "venv" / "Scripts" / "python.exe"
    fake_exe.parent.mkdir(parents=True)
    fake_exe.write_text("", encoding="utf-8")
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.chdir(sandbox)
    monkeypatch.setattr(config_mod.sys, "executable", str(fake_exe))
    assert find_repo_root(sandbox) is None


def test_appdata_dir_uses_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APPDATA", str(Path.cwd()))
    assert config_mod.APP_DIR_NAME in str(appdata_dir())


# ─── 远程地址（V0.3） ────────────────────────────────────


def test_remote_base_url_persists_roundtrip(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    settings = AppSettings(remote_base_url="http://127.0.0.1:18080")
    assert save_settings(settings, target) is True
    assert load_settings(target).remote_base_url == "http://127.0.0.1:18080"
    # 旧设置文件缺该键 → 解析为默认空串（回退本地）
    target.write_text('{"theme":"dark"}', encoding="utf-8")
    assert load_settings(target).remote_base_url == ""


def test_normalize_base_url_accepts_valid() -> None:
    assert normalize_base_url("") is None
    assert normalize_base_url("   ") is None
    assert normalize_base_url("http://127.0.0.1:18080") == "http://127.0.0.1:18080"
    assert normalize_base_url("http://127.0.0.1:18080/") == "http://127.0.0.1:18080"
    assert normalize_base_url("https://bot.example.com:8443") == "https://bot.example.com:8443"
    assert normalize_base_url("https://192.0.2.10") == "https://192.0.2.10"
    assert normalize_base_url("http://[::1]:18080") == "http://[::1]:18080"


def test_normalize_base_url_rejects_invalid() -> None:
    invalid = [
        "http://192.0.2.10",
        "http://10.0.0.5:9999",
        "http://bot.example.com",
        "http://localhost:8080",
        "https://host\n.example.com",
        "https://host\\evil.example.com",
        "ftp://x.com",
        "127.0.0.1:8080",  # 无协议
        "http://",  # 空 host
        "http:///path",
        "http://host/path",  # 含路径
        "http://host?q=1",  # 含查询
        "http://host#frag",  # 含片段
        "http://user:pass@host",  # 内嵌凭据
        "http://host:badport",  # 非法端口
        "http://host:0",  # 端口 0 无意义
        "//host",
    ]
    for bad in invalid:
        assert normalize_base_url(bad) is None, bad


def test_resolve_base_url_fallback_and_custom(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert resolve_base_url() == "http://127.0.0.1:8080"  # 无设置 → 本地默认
    save_settings(AppSettings(remote_base_url="bad url"))
    assert resolve_base_url() == "http://127.0.0.1:8080"  # 非法值 → 回退本地
    save_settings(AppSettings(remote_base_url="https://bot.example.com:8443/"))
    assert resolve_base_url() == "https://bot.example.com:8443"


# ─── credentials ────────────────────────────────────────


class FakeKeyring:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, account: str, value: str) -> None:
        self.store[(service, account)] = value

    def get_password(self, service: str, account: str) -> str | None:
        return self.store.get((service, account))

    def delete_password(self, service: str, account: str) -> None:
        self.store.pop((service, account), None)


@pytest.fixture(autouse=True)
def _reset_credentials_memory() -> Any:
    credentials_mod._memory_fallback = None
    yield
    credentials_mod._memory_fallback = None


def test_token_save_and_load_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeKeyring()
    monkeypatch.setattr(credentials_mod, "_keyring", lambda: fake)
    assert credentials_mod.save_token("abc") == "keyring"
    assert credentials_mod.load_token() == "abc"
    assert credentials_mod.clear_token() is True
    assert credentials_mod.load_token() is None


def test_token_memory_fallback_when_keyring_broken(monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken:
        def set_password(self, *_: Any) -> None:
            raise OSError("no dpapi")

        def get_password(self, *_: Any) -> str | None:
            raise OSError("no dpapi")

    monkeypatch.setattr(credentials_mod, "_keyring", lambda: Broken())
    assert credentials_mod.save_token("secret") == "memory"
    assert credentials_mod.load_token() == "secret"


def test_token_rotation_fallback_wins_over_stale_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    class ReadOnlyKeyring:
        def set_password(self, *_: Any) -> None:
            raise OSError("write unavailable")

        def get_password(self, *_: Any) -> str:
            return "old-test-credential"

    monkeypatch.setattr(credentials_mod, "_keyring", lambda: ReadOnlyKeyring())
    assert credentials_mod.save_token("new-test-credential") == "memory"
    assert credentials_mod.load_token() == "new-test-credential"


def test_tokens_are_scoped_to_destination(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeKeyring()
    monkeypatch.setattr(credentials_mod, "_keyring", lambda: fake)
    monkeypatch.setattr(credentials_mod, "resolve_base_url", lambda: "https://first.example.com")
    credentials_mod.save_token("first-test-token")
    monkeypatch.setattr(credentials_mod, "resolve_base_url", lambda: "https://second.example.com")
    assert credentials_mod.load_token() is None
    credentials_mod.save_token("second-test-token")
    credentials_mod.clear_token()
    assert credentials_mod.load_token() is None
    monkeypatch.setattr(credentials_mod, "resolve_base_url", lambda: "https://first.example.com")
    assert credentials_mod.load_token() == "first-test-token"


def test_save_token_rejects_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(credentials_mod, "_keyring", lambda: FakeKeyring())
    with pytest.raises(ValueError):
        credentials_mod.save_token("   ")


# ─── bot_api ────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    @property
    def text(self) -> str:
        return "<raw>"


def test_health_ready_maps_non200_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> _FakeResponse:
        seen["url"] = url
        seen["headers"] = kwargs.get("headers")
        return _FakeResponse(503, {"status": "not_ready"})

    monkeypatch.setattr("taffy_desktop.bot_api.httpx.request", fake_request)
    client = BotApiClient()
    assert client.health_ready() is None
    assert seen["url"] == "http://127.0.0.1:8080/health/ready"
    assert "Authorization" not in seen["headers"]

    def ok_request(method: str, url: str, **kwargs: Any) -> _FakeResponse:
        return _FakeResponse(200, {"status": "ready"})

    monkeypatch.setattr("taffy_desktop.bot_api.httpx.request", ok_request)
    assert client.health_ready() == {"status": "ready"}


def test_verify_token_and_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def ok(method: str, url: str, **kwargs: Any) -> _FakeResponse:
        return _FakeResponse(200, {})

    monkeypatch.setattr("taffy_desktop.bot_api.httpx.request", ok)
    assert BotApiClient().verify_token("tok") is True

    import httpx as httpx_mod

    def refused(method: str, url: str, **kwargs: Any) -> Any:
        raise httpx_mod.ConnectError("refused")

    monkeypatch.setattr("taffy_desktop.bot_api.httpx.request", refused)
    with pytest.raises(ConnectionError):
        BotApiClient().verify_token("tok")


def test_health_ready_returns_none_when_connection_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 离线（连接拒绝）时 health_ready 必须按 docstring 返回 None，
    # 否则自检④与 get_state 会直接崩溃。
    import httpx as httpx_mod

    def refused(method: str, url: str, **kwargs: Any) -> Any:
        raise httpx_mod.ConnectError("refused")

    monkeypatch.setattr("taffy_desktop.bot_api.httpx.request", refused)
    assert BotApiClient().health_ready() is None


def test_tail_logs_returns_content(monkeypatch: pytest.MonkeyPatch) -> None:
    def ok(method: str, url: str, **kwargs: Any) -> _FakeResponse:
        assert "lines=30" in url
        return _FakeResponse(200, {"lines": 30, "content": "log line"})

    monkeypatch.setattr("taffy_desktop.bot_api.httpx.request", ok)
    assert BotApiClient().tail_logs("t") == "log line"


@pytest.mark.parametrize("url", ["http://192.0.2.10", "http://example.com", "http://10.0.0.5"])
def test_bot_api_client_rejects_cleartext_remote_before_io(url: str) -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        BotApiClient(base_url=url)


def test_bot_api_client_custom_base_with_mock_transport() -> None:
    import httpx as httpx_mod

    seen: list[str] = []

    def handler(request: httpx_mod.Request) -> httpx_mod.Response:
        seen.append(str(request.url))
        return httpx_mod.Response(200, json={"status": "ready"})

    client = BotApiClient(
        base_url="https://remote.example.com:8443",
        transport=httpx_mod.MockTransport(handler),
    )
    assert client.health_ready() == {"status": "ready"}
    assert seen == ["https://remote.example.com:8443/health/ready"]


def test_bot_api_client_resolves_settings_per_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 设置变化即时生效：同一 client 连续两次请求走不同 base，无缓存。
    import httpx as httpx_mod

    monkeypatch.setenv("APPDATA", str(tmp_path))
    seen: list[str] = []

    def handler(request: httpx_mod.Request) -> httpx_mod.Response:
        seen.append(str(request.url))
        return httpx_mod.Response(200, json={"ok": True})

    client = BotApiClient(transport=httpx_mod.MockTransport(handler))
    save_settings(AppSettings(remote_base_url="http://127.0.0.1:18080"))
    assert client.verify_token("tok") is True
    save_settings(AppSettings(remote_base_url="https://bot.example.com:8443"))
    assert client.verify_token("tok") is True
    assert seen == [
        "http://127.0.0.1:18080/api/admin/status",
        "https://bot.example.com:8443/api/admin/status",
    ]
    # 恢复为空 → 回到本地默认
    save_settings(AppSettings(remote_base_url=""))
    assert client.base_url == "http://127.0.0.1:8080"


# ─── supervisor ─────────────────────────────────────────


class SupervisorHarness:
    """Deterministic process/port world for supervisor tests."""

    def __init__(self, tmp_path: Path) -> None:
        self.processes: list[ProcInfo] = []
        self.port_open = False
        self.port_owners: set[int] = set()
        self.started: list[list[str]] = []
        self.terminated: list[int] = []
        self.repo_root = tmp_path
        (tmp_path / "data" / "logs").mkdir(parents=True, exist_ok=True)

        def iter_processes() -> list[ProcInfo]:
            return list(self.processes)

        def port_is_open(port: int, host: str = "127.0.0.1") -> bool:
            return port == 8080 and self.port_open

        def owners(port: int) -> set[int]:
            return set(self.port_owners) if port == 8080 else set()

        def popen(args: list[str], cwd: Path, *, new_console: bool, log_file: Path | None) -> int:
            self.started.append(list(args))
            self.processes.append(
                ProcInfo(pid=9999 + len(self.started), name="python.exe", cmdline=args)
            )
            return 9999 + len(self.started)

        def terminate_tree(pid: int, graceful: float) -> None:
            self.terminated.append(pid)
            self.processes = [proc for proc in self.processes if proc.pid != pid]

        self.supervisor = Supervisor(
            tmp_path,
            iter_processes=iter_processes,
            port_is_open=port_is_open,
            port_owner_pids=owners,
            popen_detached=popen,
            terminate_tree=terminate_tree,
        )


def test_discover_matches_bot_and_napcat(tmp_path: Path) -> None:
    harness = SupervisorHarness(tmp_path)
    harness.processes = [
        ProcInfo(1, "python.exe", ["D:\\repo\\.venv\\Scripts\\python.exe", "bot.py"]),
        ProcInfo(2, "node.exe", ["node", "index.js", "-q", "123"]),
        ProcInfo(3, "explorer.exe", ["explorer"]),
        ProcInfo(4, "node.exe", ["node", "D:\\repo\\napcat\\NapCat.Shell\\index.js"]),
    ]
    bots, napcats = harness.supervisor.discover()
    assert [proc.pid for proc in bots] == [1]
    assert [proc.pid for proc in napcats] == [4]


def test_refresh_states_matrix(tmp_path: Path) -> None:
    harness = SupervisorHarness(tmp_path)

    assert harness.supervisor.refresh().as_dict()["bot_state"] == BOT_STATE_STOPPED

    harness.processes.append(ProcInfo(11, "python.exe", ["python", "bot.py"]))
    snapshot = harness.supervisor.refresh().as_dict()
    assert snapshot["bot_state"] == BOT_STATE_STARTING  # 冷启动端口未开

    harness.port_open = True
    harness.port_owners = {4242}
    assert harness.supervisor.refresh().as_dict()["bot_state"] == BOT_STATE_RUNNING

    harness.processes.clear()
    foreign = harness.supervisor.refresh().as_dict()
    assert foreign["bot_state"] == BOT_STATE_FOREIGN_PORT
    assert "占用" in foreign["note"]


def test_start_bot_refuses_foreign_port(tmp_path: Path) -> None:
    harness = SupervisorHarness(tmp_path)
    harness.port_open = True
    assert harness.supervisor.start_bot() == "port_foreign"
    assert harness.started == []


def test_start_stop_bot_lifecycle(tmp_path: Path) -> None:
    harness = SupervisorHarness(tmp_path)
    assert harness.supervisor.start_bot() == "started"
    assert harness.started[0][:4] == ["uv", "run", "--frozen", "python"]
    # 冷启动期端口未开 → starting；端口开放后 → already_running
    assert harness.supervisor.start_bot() == "already_starting"
    harness.port_open = True
    harness.port_owners = {10000}
    assert harness.supervisor.start_bot() == "already_running"

    # 停止：温和→树杀，进程消失后端口仍被他人持有也按已停止处理
    result = harness.supervisor.stop_bot()
    assert result == "stopped"
    assert harness.terminated, "进程树应收到两段式终止"


def test_stop_bot_timeout_when_process_survives(tmp_path: Path) -> None:
    harness = SupervisorHarness(tmp_path)
    harness.processes.append(ProcInfo(7777, "python.exe", ["python", "bot.py"]))

    def stubborn_terminate(pid: int, graceful: float) -> None:
        harness.terminated.append(pid)  # 进程杀不死

    supervisor = Supervisor(
        tmp_path,
        iter_processes=lambda: list(harness.processes),
        port_is_open=lambda port, host="127.0.0.1": port == 8080 and harness.port_open,
        port_owner_pids=lambda port: set(),
        popen_detached=lambda *a, **k: 1,
        terminate_tree=stubborn_terminate,
    )
    harness.port_open = False  # starting 态即可停止尝试
    assert supervisor.stop_bot() == "timeout"


def test_watchdog_backoff_restart(tmp_path: Path) -> None:
    harness = SupervisorHarness(tmp_path)
    supervisor = harness.supervisor
    supervisor.set_watchdog(True)
    # 初始在线
    harness.processes.append(ProcInfo(21, "python.exe", ["python", "bot.py"]))
    harness.port_open = True
    first = supervisor.run_once(now=100.0)
    assert first.as_dict()["next_restart_allowed_at"] == 0.0

    # bot 消失 → 安排退避
    harness.processes.clear()
    harness.port_open = False
    pending = supervisor.run_once(now=101.0).as_dict()
    assert pending["next_restart_allowed_at"] > 0

    # 退避未到 → 不启动
    supervisor.run_once(now=pending["next_restart_allowed_at"] - 1)
    assert harness.started == []

    # 退避到点 → 自动拉起并清除标记
    due = supervisor.run_once(now=pending["next_restart_allowed_at"] + 1).as_dict()
    assert len(harness.started) == 1
    assert due["next_restart_allowed_at"] == 0.0


def test_watchdog_disabled_does_not_restart(tmp_path: Path) -> None:
    harness = SupervisorHarness(tmp_path)
    harness.supervisor.set_watchdog(False)
    harness.processes.append(ProcInfo(31, "python.exe", ["python", "bot.py"]))
    harness.port_open = True
    harness.supervisor.run_once(now=10.0)
    harness.processes.clear()
    harness.port_open = False
    after = harness.supervisor.run_once(now=11.0).as_dict()
    assert after["next_restart_allowed_at"] == 0.0
    assert harness.started == []


def test_background_thread_starts_and_stops(tmp_path: Path) -> None:
    harness = SupervisorHarness(tmp_path)
    harness.supervisor.start()
    deadline = time.time() + 5
    while harness.supervisor._thread is None and time.time() < deadline:
        time.sleep(0.01)
    assert harness.supervisor._thread is not None and harness.supervisor._thread.is_alive()
    harness.supervisor.shutdown()
    assert harness.supervisor._thread is None


def test_start_bot_error_path(tmp_path: Path) -> None:
    def broken_popen(*args: Any, **kwargs: Any) -> int:
        raise OSError("denied")

    supervisor = Supervisor(
        tmp_path,
        iter_processes=lambda: [],
        port_is_open=lambda port, host="127.0.0.1": False,
        popen_detached=broken_popen,
    )
    assert supervisor.start_bot() == "error"


# ─── notify ─────────────────────────────────────────────


def test_notify_rules_toggle_and_dnd() -> None:
    # 固定"当前时刻"为 12:00，避免真实时钟落在免打扰窗口内。
    noon = time.mktime(time.strptime("2026-08-23 12:00:00", "%Y-%m-%d %H:%M:%S"))
    center = NotificationCenter(rules={"alert_failed": {"toast": False}}, clock=lambda: float(noon))
    note = center.publish("alert_failed", "预警失败", "AK-47 触发失败")
    assert note.suppressed is True  # toast=false

    dnd_center = NotificationCenter(
        rules={"alert_delivered": {"dnd_start": "02:00", "dnd_end": "08:00"}},
        clock=lambda: time.mktime(time.strptime("2026-08-23 03:00:00", "%Y-%m-%d %H:%M:%S")),
    )
    quiet = dnd_center.publish("alert_delivered", "预警触发", "夜间静默")
    assert quiet.suppressed is True

    day_center = NotificationCenter(
        rules={"alert_delivered": {"dnd_start": "02:00", "dnd_end": "08:00"}},
        clock=lambda: float(noon),
    )
    awake = day_center.publish("alert_delivered", "预警触发", "白天放行")
    assert awake.suppressed is False

    # 跨午夜窗口（23:00-08:00）：08:30 不在静默内
    overnight = NotificationCenter(
        rules={"job_failed": {"dnd_start": "23:00", "dnd_end": "08:00"}},
        clock=lambda: time.mktime(time.strptime("2026-08-23 08:30:00", "%Y-%m-%d %H:%M:%S")),
    )
    assert overnight.publish("job_failed", "任务", "x").suppressed is False


def test_notify_aggregation_window() -> None:
    base = 1_750_000_000
    clock = {"now": float(base)}
    center = NotificationCenter(aggregation_seconds=300.0, clock=lambda: clock["now"])
    center._sender = lambda notification: True  # type: ignore[method-assign]
    first = center.publish("alert_delivered", "预警", "第一条")
    second = center.publish("alert_delivered", "预警", "第二条")
    assert first.delivered is True
    assert "等 1 条同类通知" in second.body
    clock["now"] = base + 301
    third = center.publish("alert_delivered", "预警", "窗口外新事件")
    assert "等" not in third.body


def test_notify_unknown_kind_rejected() -> None:
    center = NotificationCenter()
    with pytest.raises(ValueError):
        center.publish("unknown_kind", "t", "b")


def test_notify_sender_failure_degrades_to_history() -> None:
    center = NotificationCenter()
    center._sender = None  # type: ignore[method-assign]
    note = center.publish("job_failed", "任务失败", "维护任务异常")
    assert note.delivered is False
    assert note.suppressed is False
    assert center.history[-1].kind == "job_failed"


# ─── tray / single_instance ─────────────────────────────


def test_tray_state_transitions_and_dispatch() -> None:
    calls: list[str] = []
    actions = build_tray_actions(
        open_window=lambda: calls.append("open"),
        start_bot=lambda: calls.append("start"),
        stop_bot=lambda: calls.append("stop"),
        restart_bot=lambda: calls.append("restart"),
        quit_app=lambda: calls.append("quit"),
    )
    tray = TrayIcon(None, actions=actions)
    assert tray.available is False  # 测试环境无 shell32
    assert tray.set_state(BOT_STATE_RUNNING, "运行中") is True
    assert tray.set_state(BOT_STATE_RUNNING, "运行中") is False  # 未变化
    assert tray.set_state("bogus") is False
    assert tray.dispatch("start_bot") is True
    assert tray.dispatch("missing") is False
    assert calls == ["start"]


def test_tray_callback_menu_actions() -> None:
    """托盘回调：动作派发、四态颜色映射、菜单标签对应。"""

    calls: list[str] = []
    actions = {
        "open": lambda: calls.append("open"),
        "start_bot": lambda: calls.append("start"),
        "stop_bot": lambda: calls.append("stop"),
        "restart_bot": lambda: calls.append("restart"),
        "quit": lambda: calls.append("quit"),
    }
    tray = TrayIcon(None, actions=actions)
    # 测试环境不可用 → dispatch 仍走动作表（不依赖原生图标）
    assert tray.dispatch("open") is True
    assert calls == ["open"]

    from taffy_desktop.tray import STATE_COLORS

    for state in (BOT_STATE_RUNNING, BOT_STATE_STARTING, BOT_STATE_STOPPED, BOT_STATE_FOREIGN_PORT):
        assert state in STATE_COLORS

    labels = dict(tray.menu_labels())
    assert labels["open"] == "打开工作台"
    assert labels["quit"] == "退出桌面工作台"


def test_tray_close_to_tray_setting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """关窗进托盘：minimized_to_tray_on_close=true 时隐藏，默认 false 保持退出。"""

    from taffy_desktop.config import AppSettings, save_settings

    settings_path = tmp_path / "settings.json"
    save_settings(AppSettings(minimized_to_tray_on_close=True), settings_path)
    loaded = load_settings(settings_path)
    assert loaded.minimized_to_tray_on_close is True
    # 默认 false 保持现行为（关窗即退出）
    default = load_settings(tmp_path / "missing.json")
    assert default.minimized_to_tray_on_close is False


def test_single_instance_flagfile_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import taffy_desktop.single_instance as si

    def flagfile_only(self: SingleInstanceLock) -> bool:
        return self._acquire_flagfile()

    monkeypatch.setattr(si.SingleInstanceLock, "acquire", flagfile_only)
    first = SingleInstanceLock(tmp_path)
    assert first.acquire() is True
    second = SingleInstanceLock(tmp_path)
    assert second.acquire() is False

    # 陈旧锁（>1h）可被接管
    lockfile = tmp_path / "single-instance.lock"
    stale = time.time() - 7200
    os.utime(lockfile, (stale, stale))
    third = SingleInstanceLock(tmp_path)
    assert third.acquire() is True
    third.release()


def test_activation_request_roundtrip(tmp_path: Path) -> None:
    owner = SingleInstanceLock(tmp_path)
    second = SingleInstanceLock(tmp_path)
    assert second.activate_existing() is True
    assert owner.consume_activation() is True
    assert owner.consume_activation() is False  # 已消费


# ─── app / bridge ───────────────────────────────────────


def test_bridge_http_proxy_and_guards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from taffy_desktop.app import Bridge

    lock = SingleInstanceLock(tmp_path)
    bridge = Bridge(supervisor=None, client=BotApiClient(), lock=lock)

    def ok(method: str, url: str, **kwargs: Any) -> _FakeResponse:
        headers = kwargs.get("headers") or {}
        assert headers.get("Authorization") == "Bearer tok-from-keyring"
        assert "/api/admin/status" in url
        return _FakeResponse(200, {"ok": True})

    monkeypatch.setattr("taffy_desktop.app.httpx.request", ok)
    monkeypatch.setattr("taffy_desktop.app.load_token", lambda **kwargs: "tok-from-keyring")
    result = bridge.http_request("GET", "/api/admin/status")
    assert result == {"status": 200, "body": {"ok": True}}

    blocked = bridge.http_request("GET", "http://evil.example.com/x")
    assert blocked["status"] == 0
    assert blocked["body"]["error"] == "ValueError"


def test_bridge_open_external_https_only() -> None:
    from taffy_desktop.app import Bridge

    bridge = Bridge(supervisor=None, client=BotApiClient(), lock=SingleInstanceLock(Path.cwd()))
    assert bridge.open_external("javascript:alert(1)") == "ValueError"
    assert bridge.open_external("http://insecure.example.com") == "ValueError"


def test_bridge_remote_base_url_get_set_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from taffy_desktop.app import Bridge

    monkeypatch.setenv("APPDATA", str(tmp_path))
    bridge = Bridge(supervisor=None, client=BotApiClient(), lock=SingleInstanceLock(tmp_path))
    assert bridge.get_remote_base_url() == "http://127.0.0.1:8080"  # 默认回退

    ok_set = bridge.set_remote_base_url("http://127.0.0.1:18080/")
    assert ok_set == {"ok": True, "reason": "set", "base_url": "http://127.0.0.1:18080"}
    assert bridge.get_remote_base_url() == "http://127.0.0.1:18080"
    assert load_settings().remote_base_url == "http://127.0.0.1:18080"

    bad = bridge.set_remote_base_url("ftp://x.com")
    assert bad["ok"] is False and bad["reason"] == "invalid"
    assert load_settings().remote_base_url == "http://127.0.0.1:18080"  # 非法值不破坏已存

    cleared = bridge.set_remote_base_url("")
    assert cleared["ok"] is True and cleared["reason"] == "cleared"
    assert bridge.get_remote_base_url() == "http://127.0.0.1:8080"


def test_bridge_http_request_follows_remote_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from taffy_desktop.app import Bridge

    monkeypatch.setenv("APPDATA", str(tmp_path))
    seen: dict[str, Any] = {}

    def ok(method: str, url: str, **kwargs: Any) -> _FakeResponse:
        seen["url"] = url
        return _FakeResponse(200, {"ok": True})

    monkeypatch.setattr("taffy_desktop.app.httpx.request", ok)
    monkeypatch.setattr("taffy_desktop.app.load_token", lambda **kwargs: None)
    bridge = Bridge(supervisor=None, client=BotApiClient(), lock=SingleInstanceLock(tmp_path))
    assert bridge.http_request("GET", "/api/admin/status")["status"] == 200
    assert seen["url"] == "http://127.0.0.1:8080/api/admin/status"

    assert bridge.set_remote_base_url("http://127.0.0.1:18080")["ok"] is True
    bridge.http_request("GET", "/api/admin/status")
    assert seen["url"] == "http://127.0.0.1:18080/api/admin/status"  # 无需重启即时生效


def test_save_settings_sanitizes_remote_base_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from taffy_desktop.app import Bridge

    monkeypatch.setenv("APPDATA", str(tmp_path))
    bridge = Bridge(supervisor=None, client=BotApiClient(), lock=SingleInstanceLock(tmp_path))
    assert bridge.save_settings('{"remote_base_url":"not-a-url"}') is True
    assert load_settings().remote_base_url == ""  # 非法值收敛为默认
    assert bridge.save_settings('{"remote_base_url":"https://10.0.0.5:9999/"}') is True
    assert load_settings().remote_base_url == "https://10.0.0.5:9999"


def test_self_test_runs_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from taffy_desktop.app import run_self_test

    (tmp_path / "bot.py").write_text("", encoding="utf-8")
    (tmp_path / "src").mkdir()

    def closed(port: int, host: str = "127.0.0.1") -> bool:
        return False

    monkeypatch.setattr("taffy_desktop.app._default_port_is_open", closed)
    monkeypatch.setattr("taffy_desktop.credentials._memory_fallback", None, raising=False)
    # 隔离宿主机真实凭据与线上 bot（keyring 已有令牌 / .env 已开启开关）。
    monkeypatch.setattr("taffy_desktop.app.load_token", lambda **kwargs: None)
    monkeypatch.setattr("taffy_desktop.bot_api.BotApiClient.health_ready", lambda self: None)
    monkeypatch.setattr(
        "taffy_desktop.bot_api.BotApiClient.desktop_summary",
        lambda self, token, **kwargs: (404, {}),
    )
    exit_code = run_self_test(tmp_path)
    output = capsys.readouterr().out
    assert exit_code == 0
    assert "①仓库定位" in output
    assert "⑥vendor 库完整性" in output
    assert "⑦单实例锁" in output
    assert "[WARN]" in output  # 离线状态如实标注
