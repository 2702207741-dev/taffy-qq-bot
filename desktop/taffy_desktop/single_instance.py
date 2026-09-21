"""单实例锁：Windows 命名互斥体 + 激活标志文件（二次启动唤起已有窗口）。"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

_log = logging.getLogger("taffy.desktop.singleinstance")

MUTEX_NAME = "TaffyDesktopStudio_SingleInstance"
ACTIVATE_FILE_NAME = "activate.flag"


class SingleInstanceLock:
    """Acquire once per machine; ``activate_existing`` pokes the first window."""

    def __init__(self, appdata: Path) -> None:
        self._appdata = appdata
        self._handle = None
        self._owned = False

    def acquire(self) -> bool:
        try:
            import ctypes

            self._handle = ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
            if not self._handle:
                return False
            # ERROR_ALREADY_EXISTS == 183
            already = ctypes.windll.kernel32.GetLastError() == 183
            self._owned = not already
            return self._owned
        except Exception as exc:  # noqa: BLE001 - non-Windows fallback below
            _log.info("Mutex unavailable (%s); using flag-file lock", type(exc).__name__)
            return self._acquire_flagfile()

    def _flagfile_path(self) -> Path:
        return self._appdata / "single-instance.lock"

    def _acquire_flagfile(self) -> bool:
        path = self._flagfile_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                created = path.stat().st_mtime
                # Stale lock from a crashed instance older than one hour.
                if time.time() - created < 3600:
                    return False
                path.unlink()
            path.write_text(str(os.getpid()), encoding="utf-8")
            self._owned = True
            return True
        except OSError as exc:
            _log.warning("Flag-file lock failed error=%s", type(exc).__name__)
            return False

    def activate_existing(self) -> bool:
        """Ask the running instance to raise its window; True when delivered."""

        try:
            self._appdata.mkdir(parents=True, exist_ok=True)
            (self._appdata / ACTIVATE_FILE_NAME).write_text(f"{time.time():.6f}", encoding="utf-8")
        except OSError:
            return False
        return True

    def consume_activation(self, *, max_age_seconds: float = 10.0) -> bool:
        """Return True once per fresh activation request (polled by the owner)."""

        path = self._appdata / ACTIVATE_FILE_NAME
        try:
            stamp = float(path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return False
        if stamp <= 0 or time.time() - stamp > max_age_seconds:
            try:
                path.unlink()
            except OSError:
                pass
            return False
        try:
            path.unlink()
        except OSError:
            pass
        return True

    def release(self) -> None:
        if self._handle is not None:
            try:
                import ctypes

                ctypes.windll.kernel32.ReleaseMutex(self._handle)
                ctypes.windll.kernel32.CloseHandle(self._handle)
            except Exception:  # noqa: BLE001 - best effort cleanup
                pass
            self._handle = None
        self._owned = False
        try:
            self._flagfile_path().unlink(missing_ok=True)
        except OSError:
            pass

    @property
    def owned(self) -> bool:
        return self._owned
