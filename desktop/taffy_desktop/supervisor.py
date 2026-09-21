"""进程看护：发现/启停/端口归属交叉验证/退避重启。

事实基线（任务单第九节）：
- F2 启动 = ``uv run --frozen python bot.py``，冷启动 30-90 秒；
- F4 uv 跳板：父 ``.venv\\Scripts\\python.exe``（cmdline 含 bot.py）→ 子 uv cpython；
  发现与查杀都按进程树处理；
- F6 停止 = 温和 5s → taskkill /T /F 两段式；
- F7 看护语义 = 10s 巡检、30s 重启退避、关软件不停 bot（DETACHED 启动）。
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import DEFAULT_BOT_PORT, DEFAULT_NAPCAT_PORT

_log = logging.getLogger("taffy.desktop.supervisor")

DETACHED_START_FLAGS = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
VISIBLE_CONSOLE_FLAGS = 0x00000010  # CREATE_NEW_CONSOLE（NapCat 扫码必须可见窗口）
_GRACEFUL_STOP_SECONDS = 5.0


@dataclass(frozen=True)
class ProcInfo:
    pid: int
    name: str
    cmdline: list[str]
    # 0 means unknown; a bot whose ppid is outside the bot set is a tree root,
    # i.e. one logical bot instance (uv launcher → venv trampoline → real
    # interpreter all belong to the same tree).
    ppid: int = 0


def _creation_flags(new_console: bool) -> int:
    return VISIBLE_CONSOLE_FLAGS if new_console else DETACHED_START_FLAGS


def _default_iter_processes() -> list[ProcInfo]:
    import psutil

    result: list[ProcInfo] = []
    for process in psutil.process_iter(["pid", "ppid", "name", "cmdline"]):
        try:
            info = process.info
            cmdline = [part for part in (info.get("cmdline") or []) if part]
            result.append(
                ProcInfo(
                    int(info["pid"]),
                    str(info.get("name") or ""),
                    cmdline,
                    ppid=int(info.get("ppid") or 0),
                )
            )
        except Exception:  # noqa: BLE001 - a vanishing process must not kill the scan
            continue
    return result


def _default_port_is_open(port: int, host: str = "127.0.0.1") -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        return sock.connect_ex((host, int(port))) == 0


def _default_port_owner_pids(port: int) -> set[int]:
    import psutil

    owners: set[int] = set()
    try:
        connections = psutil.net_connections(kind="inet")
    except Exception:  # noqa: BLE001 - permission denied keeps ownership unknown
        return owners
    target = int(port)
    for connection in connections:
        laddr = getattr(connection, "laddr", None)
        if laddr is not None and getattr(laddr, "port", 0) == target and connection.pid:
            owners.add(int(connection.pid))
    return owners


def _default_popen(args: list[str], cwd: Path, *, new_console: bool, log_file: Path | None) -> int:
    flags = _creation_flags(new_console)
    if new_console:
        creation = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            args,
            cwd=str(cwd),
            creationflags=flags,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        handle = open(log_file, "ab") if log_file else subprocess.DEVNULL  # noqa: SIM115
        try:
            creation = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                args,
                cwd=str(cwd),
                creationflags=flags,
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
            )
        finally:
            if hasattr(handle, "close"):
                handle.close()
    return creation.pid


def _default_terminate_tree(pid: int, graceful_seconds: float) -> None:
    """温和 N 秒 → taskkill /T /F 两段式（F6）。"""

    import psutil

    try:
        parent = psutil.Process(pid)
    except psutil.Error:
        return
    try:
        children = parent.children(recursive=True)
        for child in (*children, parent):
            try:
                child.terminate()
            except psutil.Error:
                continue
        _, alive = psutil.wait_procs([*children, parent], timeout=graceful_seconds)
        for survivor in alive:
            subprocess.run(  # noqa: S603 - fixed argv
                ["taskkill", "/PID", str(survivor.pid), "/T", "/F"],
                check=False,
                capture_output=True,
            )
    except psutil.Error as exc:
        _log.warning("Terminate tree %s failed error=%s", pid, type(exc).__name__)


BOT_STATE_STOPPED = "stopped"
BOT_STATE_STARTING = "starting"
BOT_STATE_RUNNING = "running"
BOT_STATE_FOREIGN_PORT = "foreign_port"
_BOT_STATES = (
    BOT_STATE_STOPPED,
    BOT_STATE_STARTING,
    BOT_STATE_RUNNING,
    BOT_STATE_FOREIGN_PORT,
)


@dataclass
class SupervisorStatus:
    bot_state: str = BOT_STATE_STOPPED
    bot_pids: list[int] = field(default_factory=list)
    # 进程树根数量 = 逻辑 bot 实例数（uv 启动器→跳板→真实解释器只算 1）。
    bot_instances: int = 0
    port_open: bool = False
    port_owner_pids: list[int] = field(default_factory=list)
    napcat_state: str = BOT_STATE_STOPPED
    napcat_pids: list[int] = field(default_factory=list)
    watchdog_enabled: bool = True
    last_check_epoch: int = 0
    next_restart_allowed_at: float = 0.0
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "bot_state": self.bot_state,
            "bot_pids": self.bot_pids,
            "bot_instances": self.bot_instances,
            "port_open": self.port_open,
            "port_owner_pids": self.port_owner_pids,
            "napcat_state": self.napcat_state,
            "napcat_pids": self.napcat_pids,
            "watchdog_enabled": self.watchdog_enabled,
            "last_check_epoch": self.last_check_epoch,
            "next_restart_allowed_at": self.next_restart_allowed_at,
            "note": self.note,
        }


class Supervisor:
    """Bot/NapCat 生命周期看护。所有 OS 调用经可注入钩子，便于测试。"""

    def __init__(
        self,
        repo_root: Path,
        *,
        bot_port: int = DEFAULT_BOT_PORT,
        napcat_port: int = DEFAULT_NAPCAT_PORT,
        napcat_dir: Path | None = None,
        napcat_qq: str | None = None,
        poll_seconds: float = 10.0,
        restart_backoff_seconds: float = 30.0,
        bot_log_path: Path | None = None,
        iter_processes: Any = None,
        port_is_open: Any = None,
        port_owner_pids: Any = None,
        popen_detached: Any = None,
        terminate_tree: Any = None,
    ) -> None:
        self._repo_root = repo_root
        self._bot_port = int(bot_port)
        self._napcat_port = int(napcat_port)
        self._napcat_dir = napcat_dir or (repo_root / "napcat" / "NapCat.Shell")
        self._napcat_qq = napcat_qq
        self._poll_seconds = max(2.0, float(poll_seconds))
        self._restart_backoff_seconds = max(5.0, float(restart_backoff_seconds))
        self._bot_log_path = bot_log_path or (repo_root / "data" / "logs" / "desktop-bot.log")
        self._iter_processes = iter_processes or _default_iter_processes
        self._port_is_open = port_is_open or _default_port_is_open
        self._port_owner_pids = port_owner_pids or _default_port_owner_pids
        self._popen = popen_detached or _default_popen
        self._terminate_tree = terminate_tree or _default_terminate_tree

        self._lock = threading.Lock()
        self._status = SupervisorStatus(watchdog_enabled=True)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # ── 发现 ────────────────────────────────────────────

    @staticmethod
    def _match_cmdline(proc: ProcInfo, *needles: str) -> bool:
        joined = " ".join(proc.cmdline).casefold()
        return all(needle.casefold() in joined for needle in needles)

    def discover(self) -> tuple[list[ProcInfo], list[ProcInfo]]:
        processes = self._iter_processes()
        bots = [
            proc
            for proc in processes
            if proc.name.casefold().startswith(("python", "uv"))
            and self._match_cmdline(proc, "bot.py")
        ]
        napcats = [
            proc
            for proc in processes
            if proc.name.casefold().startswith("node")
            and self._match_cmdline(proc, "index.js")
            and any("napcat" in part.casefold() for part in proc.cmdline)
        ]
        return bots, napcats

    def refresh(self) -> SupervisorStatus:
        bots, napcats = self.discover()
        port_open = self._port_is_open(self._bot_port)
        owner_pids = sorted(self._port_owner_pids(self._bot_port)) if port_open else []
        if bots and port_open:
            state = BOT_STATE_RUNNING
        elif bots and not port_open:
            state = BOT_STATE_STARTING
        elif not bots and port_open:
            state = BOT_STATE_FOREIGN_PORT
        else:
            state = BOT_STATE_STOPPED
        napcat_state = BOT_STATE_RUNNING if napcats else BOT_STATE_STOPPED
        bot_pid_set = {proc.pid for proc in bots}
        # ppid 未知(0)或父进程在 bot 集合之外 → 树根 → 一个逻辑实例。
        bot_instances = sum(1 for proc in bots if proc.ppid not in bot_pid_set)
        with self._lock:
            previous_note = self._status.note
            self._status.bot_state = state
            self._status.bot_pids = [proc.pid for proc in bots]
            self._status.bot_instances = bot_instances
            self._status.port_open = port_open
            self._status.port_owner_pids = owner_pids
            self._status.napcat_state = napcat_state
            self._status.napcat_pids = [proc.pid for proc in napcats]
            self._status.last_check_epoch = int(time.time())
            if state == BOT_STATE_FOREIGN_PORT:
                self._status.note = (
                    f"{self._bot_port} 端口被其他进程占用（pid {owner_pids}），已禁止启动塔菲"
                )
            elif previous_note.startswith("端口被"):
                self._status.note = ""
            return SupervisorStatus(**self._status.as_dict())

    def status(self) -> dict[str, Any]:
        with self._lock:
            snapshot = dict(self._status.as_dict())
        return snapshot

    # ── 启停 ────────────────────────────────────────────

    def start_bot(self) -> str:
        fresh = self.refresh()
        if fresh.bot_state == BOT_STATE_RUNNING:
            return "already_running"
        if fresh.bot_state == BOT_STATE_FOREIGN_PORT:
            _log.warning("Refusing to start bot: port %s is owned by others", self._bot_port)
            return "port_foreign"
        if fresh.bot_state == BOT_STATE_STARTING:
            return "already_starting"
        log_file = self._bot_log_path
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            self._popen(
                ["uv", "run", "--frozen", "python", "bot.py"],
                self._repo_root,
                new_console=False,
                log_file=log_file,
            )
        except OSError as exc:
            _log.error("Start bot failed error=%s", type(exc).__name__)
            return "error"
        _log.info("Bot start requested (detached)")
        return "started"

    def stop_bot(self) -> str:
        fresh = self.refresh()
        if fresh.bot_state in (BOT_STATE_STOPPED, BOT_STATE_FOREIGN_PORT):
            return "not_running"
        for pid in fresh.bot_pids:
            self._terminate_tree(pid, _GRACEFUL_STOP_SECONDS)
        after = self.refresh()
        if after.bot_state == BOT_STATE_STOPPED:
            return "stopped"
        if after.bot_state == BOT_STATE_FOREIGN_PORT:
            return "stopped"
        return "timeout"

    def restart_bot(self) -> str:
        self.stop_bot()
        time.sleep(min(_GRACEFUL_STOP_SECONDS, 1.0))
        return self.start_bot()

    def start_napcat(self) -> str:
        fresh = self.refresh()
        if fresh.napcat_state == BOT_STATE_RUNNING:
            return "already_running"
        if not self._napcat_dir.is_dir():
            return "dir_missing"
        args = ["cmd", "/c", "node", "index.js"]
        if self._napcat_qq:
            args.extend(["-q", str(self._napcat_qq)])
        try:
            self._popen(args, self._napcat_dir, new_console=True, log_file=None)
        except OSError as exc:
            _log.error("Start napcat failed error=%s", type(exc).__name__)
            return "error"
        return "started"

    def stop_napcat(self) -> str:
        fresh = self.refresh()
        if fresh.napcat_state != BOT_STATE_RUNNING:
            return "not_running"
        for pid in fresh.napcat_pids:
            self._terminate_tree(pid, _GRACEFUL_STOP_SECONDS)
        return "stopped" if self.refresh().napcat_state != BOT_STATE_RUNNING else "timeout"

    # ── 看护 ────────────────────────────────────────────

    def set_watchdog(self, enabled: bool) -> None:
        with self._lock:
            self._status.watchdog_enabled = bool(enabled)

    def run_once(self, *, now: float | None = None) -> SupervisorStatus:
        """One watchdog tick: refresh, then backoff-restart a vanished bot."""

        moment = time.monotonic() if now is None else now
        before = self.status()
        fresh = self.refresh().as_dict()
        if fresh["watchdog_enabled"]:
            had_live_bot = bool(before["bot_pids"]) or before["bot_state"] in (
                BOT_STATE_RUNNING,
                BOT_STATE_STARTING,
            )
            bot_missing = fresh["bot_state"] in (BOT_STATE_STOPPED,)
            if had_live_bot and bot_missing and fresh["next_restart_allowed_at"] == 0:
                _log.warning("Bot vanished; scheduling restart with backoff")
                with self._lock:
                    self._status.next_restart_allowed_at = moment + self._restart_backoff_seconds
                    self._status.note = (
                        f"塔菲进程消失，{self._restart_backoff_seconds:.0f}s 后自动拉起"
                    )
                fresh = dict(self.refresh().as_dict())
            pending_restart = fresh["next_restart_allowed_at"] > 0
            due = fresh["next_restart_allowed_at"] <= moment
            if pending_restart and due:
                with self._lock:
                    self._status.next_restart_allowed_at = 0.0
                if fresh["bot_state"] == BOT_STATE_STOPPED:
                    result = self.start_bot()
                    _log.info("Watchdog restart result=%s", result)
                    fresh = dict(self.refresh().as_dict())
        return SupervisorStatus(**fresh)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()

        def loop() -> None:
            while not self._stop_event.is_set():
                try:
                    self.run_once()
                except Exception as exc:  # noqa: BLE001 - watchdog must survive anything
                    _log.error("Watchdog tick failed error=%s", type(exc).__name__)
                self._stop_event.wait(self._poll_seconds)

        self._thread = threading.Thread(target=loop, name="taffy-supervisor", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        """Stop watching. Per F7 this never stops the bot itself."""

        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._poll_seconds)
            self._thread = None
