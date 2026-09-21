"""配置与路径：仓库根定位、%APPDATA% 设置文件、默认值。"""

from __future__ import annotations

import ipaddress
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

APP_DIR_NAME = "TaffyDesktop"
SETTINGS_FILE_NAME = "settings.json"
REPO_MARKER = "bot.py"
DEFAULT_BOT_PORT = 8080
DEFAULT_NAPCAT_PORT = 3001


@dataclass
class AppSettings:
    """客户端个性化数据（A2：绝不进 bot 数据库）。"""

    theme: str = "light"
    accent: str = "warm"
    refresh_interval_seconds: int = 30
    dashboard_tiles: list[str] = field(
        default_factory=lambda: ["index", "temperature", "watchlist", "alerts", "events"]
    )
    scenes: dict[str, dict[str, Any]] = field(default_factory=dict)
    active_scene: str = "盘中盯盘"
    quick_actions: list[dict[str, Any]] = field(default_factory=list)
    notify_rules: dict[str, dict[str, Any]] = field(default_factory=dict)
    watchlist_groups: dict[str, list[str]] = field(default_factory=dict)
    nav_collapsed: bool = False
    window: dict[str, int] = field(default_factory=dict)
    bot_repo_path: str = ""
    autostart: bool = False
    minimized_to_tray_on_close: bool = False
    remote_base_url: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AppSettings":
        known = cls()
        merged: dict[str, Any] = {}
        for current_field in known.__dataclass_fields__:
            if current_field in raw:
                merged[current_field] = raw[current_field]
            else:
                merged[current_field] = getattr(known, current_field)
        return cls(**merged)


def appdata_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    path = Path(base) / APP_DIR_NAME
    return path


def settings_path() -> Path:
    return appdata_dir() / SETTINGS_FILE_NAME


def load_settings(path: Path | None = None) -> AppSettings:
    target = path or settings_path()
    try:
        raw = json.loads(target.read_text("utf-8"))
    except (OSError, ValueError):
        return AppSettings()
    if not isinstance(raw, dict):
        return AppSettings()
    return AppSettings.from_dict(raw)


def save_settings(settings: AppSettings, path: Path | None = None) -> bool:
    target = path or settings_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(asdict(settings), ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        return False
    return True


def _is_repo_root(candidate: Path) -> bool:
    return (candidate / REPO_MARKER).is_file() and (candidate / "src").is_dir()


def find_repo_root(start: Path | None = None) -> Path | None:
    """Locate the bot repository by walking up from start/cwd/executable."""

    candidates: list[Path] = []
    if start is not None:
        candidates.append(start)
    candidates.append(Path.cwd())
    try:
        candidates.append(Path(sys.executable).resolve().parent)
        candidates.append(Path(sys.executable).resolve().parent.parent)
        if getattr(sys, "frozen", False):  # pragma: no cover - PyInstaller only
            candidates.append(Path(sys.executable).resolve().parent.parent)
    except (OSError, ValueError):
        pass
    seen: set[Path] = set()
    for origin in candidates:
        current = origin.resolve()
        while True:
            if current in seen:
                break
            seen.add(current)
            if _is_repo_root(current):
                return current
            parent = current.parent
            if parent == current:
                break
            current = parent
    return None


def normalize_base_url(value: str) -> str | None:
    """Validate/clean a user-supplied base URL.

    Accept HTTPS, or HTTP with a literal loopback IP for local/SSH use.
    URLs must have a bare host[:port] network location
    (no path/query/fragment/embedded credentials). Returns the normalized URL
    without trailing slash, or None when empty or invalid — callers fall back
    to the local default.
    """

    raw = (value or "").strip()
    if not raw:
        return None
    if any(ord(char) < 33 for char in raw) or "\\" in raw:
        return None
    try:
        parts = urlsplit(raw)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https"):
        return None
    if not parts.hostname or parts.username is not None:
        return None
    if parts.path not in ("", "/"):
        return None
    if parts.query or parts.fragment:
        return None
    try:
        port = parts.port  # raises ValueError on malformed port
    except ValueError:
        return None
    if port == 0:
        return None
    if parts.scheme == "http":
        # Never send the administrator credential over an unencrypted network.
        # Literal loopback addresses support local services and SSH tunnels;
        # hostnames are not accepted here because their DNS answers can change.
        try:
            if not ipaddress.ip_address(parts.hostname).is_loopback:
                return None
        except ValueError:
            return None
    return f"{parts.scheme}://{parts.netloc}"


def resolve_base_url(settings: AppSettings | None = None) -> str:
    """Effective API base: remote_base_url when valid, local default otherwise.

    Re-resolved on every call (no caching), so a settings change takes effect
    on the very next request.
    """

    current = settings if settings is not None else load_settings()
    if current.remote_base_url:
        normalized = normalize_base_url(current.remote_base_url)
        if normalized:
            return normalized
    return f"http://127.0.0.1:{DEFAULT_BOT_PORT}"
