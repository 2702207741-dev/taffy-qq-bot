"""Generate and optionally apply rotated credentials for the local deployment.

One-off rotation helper (mirrors ``migrate_state.py`` conventions):

- Default (dry run): prints fingerprints only (never the full secrets).
- ``--apply``: backs up ``.env`` into ``data/config-backups/``, rewrites only
  the requested credential keys, leaves every other line untouched, tightens
  file permissions on POSIX, and prunes old backups keeping the newest two.
  The bot must not be listening (or ``--confirm-bot-stopped`` must be given)
  because the running process keeps its old in-memory credentials and would
  silently diverge from the file.
- Values are only ever generated at runtime; nothing is hard-coded here.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import shutil
import socket
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = PROJECT_ROOT / ".env"
BACKUP_DIR = PROJECT_ROOT / "data" / "config-backups"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080
_BACKUPS_TO_KEEP = 2

KEY_ADMIN = "ADMIN_API_TOKEN"
KEY_ACCESS = "ONEBOT_V11_ACCESS_TOKEN"
KEY_SECRET = "ONEBOT_V11_SECRET"

KEY_GROUPS: dict[str, tuple[str, ...]] = {
    "admin": (KEY_ADMIN,),
    "onebot": (KEY_ACCESS, KEY_SECRET),
    "all": (KEY_ADMIN, KEY_ACCESS, KEY_SECRET),
}


def generate_values(keys: str) -> dict[str, str]:
    """Return fresh high-entropy values for the requested key group."""

    values = {}
    for key in KEY_GROUPS[keys]:
        values[key] = secrets.token_urlsafe(32)
    return values


def fingerprint(value: str) -> str:
    """Render a preview that cannot substitute for the secret itself."""

    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    return f"{value[:4]}...(sha256:{digest})"


def backup_env(env_path: Path, backup_dir: Path) -> Path:
    """Copy the current .env into the backup directory and return its path."""

    if not env_path.exists():
        raise FileNotFoundError(f"{env_path} does not exist")
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    destination = backup_dir / f"env-backup-{stamp}"
    shutil.copy2(env_path, destination)
    return destination


def _restrict_permissions(path: Path) -> None:
    """Limit credential-bearing files to their owner on POSIX systems."""

    if os.name == "posix":
        path.chmod(0o600)


def prune_backups(backup_dir: Path, keep: int = _BACKUPS_TO_KEEP) -> int:
    """Keep only the newest ``keep`` env backups; return the removed count."""

    if not backup_dir.exists():
        return 0
    backups = sorted(
        (path for path in backup_dir.glob("env-backup-*") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
    )
    stale = backups[:-keep] if len(backups) > keep else []
    for path in stale:
        path.unlink()
    return len(stale)


def rewrite_env(env_path: Path, values: dict[str, str]) -> int:
    """Rewrite only ``values`` keys in place; other lines are preserved.

    Keys missing from the file are appended at the end.  Returns the number
    of keys that ended up updated (existing replaced + missing appended).
    """

    if not env_path.exists():
        raise FileNotFoundError(f"{env_path} does not exist")
    text = env_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    pending = dict(values)
    changed = 0
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in pending:
            lines[index] = f"{key}={pending.pop(key)}"
            changed += 1
    for key, value in pending.items():
        if lines and lines[-1] != "":
            lines.append("")
        lines.append(f"{key}={value}")
        changed += 1
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return changed


def apply_rotation(env_path: Path, backup_dir: Path, values: dict[str, str]) -> Path:
    """Back up then rewrite the .env; returns the backup path."""

    backup = backup_env(env_path, backup_dir)
    changed = rewrite_env(env_path, values)
    if changed == 0:
        raise RuntimeError("no credential keys were written; .env is unchanged")
    _restrict_permissions(backup)
    _restrict_permissions(env_path)
    prune_backups(backup_dir)
    return backup


def _env_value(env_path: Path, key: str) -> str | None:
    if not env_path.exists():
        return None
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        if name.strip() == key:
            return value.strip().strip('"').strip("'")
    return None


def resolve_bot_endpoint(env_path: Path = ENV_FILE) -> tuple[str, int]:
    """Resolve the bot's listen address from its runtime configuration.

    Wildcard listen addresses are probed through loopback; invalid or missing
    configuration falls back to the historical ``127.0.0.1:8080`` default.
    """

    host = _env_value(env_path, "HOST") or DEFAULT_HOST
    port_raw = _env_value(env_path, "PORT") or str(DEFAULT_PORT)
    try:
        port = int(port_raw)
    except ValueError:
        port = DEFAULT_PORT
    if not 1 <= port <= 65_535:
        port = DEFAULT_PORT
    if host in {"0.0.0.0", "::", ""}:
        host = DEFAULT_HOST
    return host, port


def bot_is_listening(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> bool:
    """True when something is accepting connections on the given endpoint."""

    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Rotate local deployment credentials (admin API token / OneBot)."
    )
    parser.add_argument(
        "--keys",
        choices=sorted(KEY_GROUPS),
        default="all",
        help="which credential group to rotate (default: all)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="back up .env and write the new values (default: dry run)",
    )
    parser.add_argument(
        "--confirm-bot-stopped",
        action="store_true",
        help="skip the liveness check",
    )
    args = parser.parse_args(argv)

    values = generate_values(args.keys)
    if not args.apply:
        print("Dry run — value fingerprints only (use --apply to write them to .env):")
        for key, value in values.items():
            print(f"  {key}={fingerprint(value)}")
        print(
            "Restart the bot after applying; the running process keeps its "
            "old credentials until then."
        )
        return 0

    host, port = resolve_bot_endpoint()
    if bot_is_listening(host, port):
        if not args.confirm_bot_stopped:
            print(
                f"bot is still listening on {host}:{port}; stop it first "
                "or pass --confirm-bot-stopped",
                file=sys.stderr,
            )
            return 2
        print(f"warning: bot is still listening on {host}:{port}")

    try:
        backup = apply_rotation(ENV_FILE, BACKUP_DIR, values)
    except (FileNotFoundError, RuntimeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3

    print(f"rotated {len(values)} key(s); backup written to {backup}")
    print("restart the bot for the new credentials to take effect")
    return 0


if __name__ == "__main__":
    sys.exit(main())
