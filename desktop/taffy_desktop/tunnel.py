"""Opt-in SSH tunnel: explicit configuration, loopback binding, owned processes only."""

from __future__ import annotations

import logging
import shutil
import socket
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_log = logging.getLogger("taffy.desktop.tunnel")

TUNNEL_HOST = ""
TUNNEL_LOCAL_PORT = 18080
TUNNEL_REMOTE_PORT = 8080
PROBE_INTERVAL_SECONDS = 15.0


@dataclass
class TunnelManager:
    """Owns the panel SSH tunnel: spawn on start, reap on stop, reconnect."""

    local_port: int = TUNNEL_LOCAL_PORT
    remote_host: str = TUNNEL_HOST
    remote_user: str = ""
    remote_port: int = TUNNEL_REMOTE_PORT
    key_file: Path | None = None
    probe_interval: float = PROBE_INTERVAL_SECONDS
    _process: subprocess.Popen[Any] | None = field(default=None, init=False, repr=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.remote_host.startswith("-") or self.remote_user.startswith("-"):
            raise ValueError("Invalid SSH destination")

    def is_open(self) -> bool:
        """本地转发端口是否已有监听（无论谁开的）。"""

        try:
            with socket.create_connection(("127.0.0.1", self.local_port), timeout=1.0):
                return True
        except OSError:
            return False

    def start(self) -> str:
        """拉起隧道并开始守护；隧道已存在则接管，返回 'started'/'already'。"""

        if not self.remote_host or not self.remote_user or self.key_file is None:
            return "disabled"
        was_open = self.is_open()
        if not was_open:
            self._spawn()
        self._stop.clear()
        self._thread = threading.Thread(target=self._watch, name="taffy-tunnel", daemon=True)
        self._thread.start()
        return "already" if was_open else "started"

    def _watch(self) -> None:
        """15 秒一轮：转发端口消失（sshd 断/进程被杀）→ 重新拉起。"""

        while not self._stop.wait(self.probe_interval):
            if not self.is_open():
                self._terminate_owned()
                self._spawn()

    def _spawn(self) -> None:
        ssh = shutil.which("ssh")
        if not ssh:
            _log.warning("ssh executable not found; tunnel not started")
            return
        cmd = [
            ssh,
            "-i",
            str(self.key_file),
            "-N",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "BatchMode=yes",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=3",
            "-L",
            f"127.0.0.1:{self.local_port}:127.0.0.1:{self.remote_port}",
            f"{self.remote_user}@{self.remote_host}",
        ]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.Popen[str](
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags
            )
            self._process = proc
            _log.info("tunnel spawned pid=%s", proc.pid)
        except OSError as exc:
            _log.warning("Tunnel spawn failed error=%s", type(exc).__name__)

    def _terminate_owned(self) -> None:
        if self._process is not None:
            try:
                self._process.terminate()
            except OSError:
                pass
            self._process = None

    def stop(self) -> bool:
        """Stop the watcher and only the SSH process created by this instance."""

        self._stop.set()
        self._terminate_owned()
        return True
