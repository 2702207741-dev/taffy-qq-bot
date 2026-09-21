"""Controlled test entry: run pytest inside an isolated source copy.

Usage (from the project root)::

    uv run --frozen python scripts/run_isolated_tests.py [--self-check-only] [--cleanup] [-- <pytest args>]

What it does, in order:

1.  Copies an explicit whitelist of the **working tree** (including this
    batch's new files and uncommitted edits — never just the HEAD
    snapshot) into a fresh temporary root, excluding real ``.env`` /
    ``.env.*``, ``data/``, ``.git/``, ``.venv/``, logs, caches and
    credential-shaped files.  Every copied file is hashed into
    ``work/isolation-manifest.json``.
2.  Runs an isolation self-check: no protected dotenv files in the copy,
    one probe child proving application imports resolve under the copy
    with an empty credential snapshot, and no ``.git`` directory.
3.  Runs ``python -m pytest`` with the copy as CWD and ``PYTHONPATH``,
    a scrubbed environment (no inherited secrets / proxies / TAFFY_*
    overrides), and every cache / temp output redirected into the
    controlled work directory.

This is a Python-layer guard rail for tests, not an operating-system
security sandbox: it cannot contain native extensions, browsers, or
other processes.  Unprotectable paths are denied by the in-test guards
(``tests/support/isolation.py``) and reported as restricted, never
silently passed.

Exit code: ``2`` when the self-check fails, otherwise pytest's code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ORIGIN = Path(__file__).resolve().parents[1]

COPY_DIRS = ("src", "tests", "scripts")
COPY_FILES = (
    "bot.py",
    "scrape_steamdt.py",
    "pyproject.toml",
    ".env.example",
    Path("docs/mcp/taffy_market_tools.json"),
    Path("docs/mcp/MCP_REFERENCE.md"),
)
EXCLUDE_DIR_NAMES = {".git", ".venv", "data", "__pycache__", ".pytest_cache", ".mypy_cache"}
EXCLUDE_FILE_NAMES = {".env", ".env.prod", ".env.staging", ".env.test"}
EXCLUDE_SUFFIXES = (".pyc", ".pyo", ".log", ".pem", ".key")


def _is_protected_dotenv(name: str) -> bool:
    lowered = name.lower()
    return (lowered == ".env" or lowered.startswith(".env.")) and name != ".env.example"


def _check_link_boundary(path: Path, *, origin: Path, relative: Path) -> None:
    """Refuse symlinks, junctions, and escapes for one copy source (R5).

    ``rglob`` follows directory links, so the check must cover every
    visited entry — files and directories alike — plus the fixed
    ``COPY_FILES`` entries.  The canonical path has to stay inside the
    declared origin; anything else (link or ``..`` escape) aborts the
    whole copy before any test protection exists.
    """
    if path.is_symlink() or os.path.isjunction(path):
        raise RuntimeError(f"refusing to copy link: {relative}")
    canonical = path.resolve()
    try:
        canonical.relative_to(origin.resolve())
    except ValueError:
        raise RuntimeError(f"refusing to copy escape: {relative} -> {canonical}") from None


def _iter_worktree_sources(origin: Path = ORIGIN) -> list[Path]:
    sources: list[Path] = []
    for dirname in COPY_DIRS:
        root = origin / dirname
        if not root.is_dir() or root.is_symlink() or os.path.isjunction(root):
            raise RuntimeError(f"whitelist directory missing in worktree: {dirname}")
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(origin)
            if any(part in EXCLUDE_DIR_NAMES for part in relative.parts):
                continue
            if path.is_dir():
                _check_link_boundary(path, origin=origin, relative=relative)
                continue
            if path.name in EXCLUDE_FILE_NAMES or _is_protected_dotenv(path.name):
                continue
            if path.suffix.lower() in EXCLUDE_SUFFIXES:
                continue
            _check_link_boundary(path, origin=origin, relative=relative)
            sources.append(relative)
    for name in COPY_FILES:
        relative = Path(name)
        source = origin / relative
        if not source.is_file():
            raise RuntimeError(f"whitelist file missing in worktree: {relative}")
        _check_link_boundary(source, origin=origin, relative=relative)
        sources.append(relative)
    return sources


def _copy_sources(sources: list[Path], root: Path, origin: Path = ORIGIN) -> dict[str, str]:
    manifest: dict[str, str] = {}
    for relative in sources:
        source = origin / relative
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        # Single read: the hashed bytes are exactly the copied bytes, so a
        # source modified mid-copy cannot split manifest from replica (R5).
        content = source.read_bytes()
        manifest[relative.as_posix()] = hashlib.sha256(content).hexdigest()
        target.write_bytes(content)
    return manifest


def verify_copy_against_manifest(root: Path, manifest: dict[str, str]) -> list[str]:
    """Re-hash the replica; return mismatched relative paths (empty = OK)."""
    mismatched: list[str] = []
    for relative, digest in manifest.items():
        target = root / Path(relative)
        try:
            actual = hashlib.sha256(target.read_bytes()).hexdigest()
        except OSError:
            actual = "<missing>"
        if actual != digest:
            mismatched.append(relative)
    return mismatched


def _assert_no_protected_files(root: Path) -> list[str]:
    hits = [
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
        and _is_protected_dotenv(path.name)
        or (path.is_file() and path.name in EXCLUDE_FILE_NAMES)
    ]
    if hits:
        raise RuntimeError(f"protected files leaked into isolated copy: {sorted(hits)}")
    if (root / ".git").exists():
        raise RuntimeError("refusing copy containing a .git directory")
    return hits


def _build_env(root: Path, work: Path, token: str) -> dict[str, str]:
    if str(ORIGIN) not in sys.path:
        sys.path.insert(0, str(ORIGIN))
    from tests.support import isolation

    return isolation.scrubbed_child_env(root=root, token=token, work=work)


def _probe_command() -> list[str]:
    return [
        sys.executable,
        "-c",
        (
            "import json, sys; "
            "sys.path.insert(0, '.'); "
            "import src.services.config as c; "
            "print(json.dumps({"
            "'project_root': str(c.PROJECT_ROOT), "
            "'snapshot_keys': len(c._RUNTIME_SETTINGS)}))"
        ),
    ]


def _guard_selftest_command() -> list[str]:
    """Synthetic guard probe: must be denied before any pytest runs."""
    return [
        sys.executable,
        "-c",
        "\n".join(
            [
                "import os, socket, sys, tempfile",
                "sys.path.insert(0, '.')",
                "from tests.support import isolation as iso",
                "iso.install_child_guards(sentinels=[]) ",
                "refused = 0",
                "try:",
                "    socket.create_connection(('127.0.0.1', 8080), timeout=1)",
                "except iso.IsolationViolationError:",
                "    refused += 1",
                "sealed = tempfile.mkdtemp()",
                "iso.add_sentinel(sealed)",
                "try:",
                "    open(os.path.join(sealed, 'x'), 'w')",
                "except iso.IsolationViolationError:",
                "    refused += 1",
                "try:",
                "    iso.check_read('/definitely/outside/controlled-trees/.env')",
                "except iso.IsolationViolationError:",
                "    refused += 1",
                "print('GUARD-SELFTEST-OK' if refused == 3 else 'GUARD-SELFTEST-FAIL')",
            ]
        ),
    ]
    return [
        sys.executable,
        "-c",
        (
            "import json, sys; "
            "sys.path.insert(0, '.'); "
            "import src.services.config as c; "
            "print(json.dumps({"
            "'project_root': str(c.PROJECT_ROOT), "
            "'snapshot_keys': len(c._RUNTIME_SETTINGS)}))"
        ),
    ]


def _run_tee(argv: list[str], *, cwd: Path, env: dict[str, str], log_path: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        argv,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    assert process.stdout is not None
    with log_path.open("w", encoding="utf-8", errors="replace") as log:
        for line in process.stdout:
            sys.stdout.write(line)
            log.write(line)
    return process.wait()


def _worktree_differs_from_head(sources: list[Path]) -> list[str]:
    """Relative paths whose worktree bytes HEAD cannot provide (R6/G).

    Tracked modifications plus untracked-but-copied files.  The G test
    pins these so a HEAD-only copy could never satisfy the suite.
    """
    try:
        modified = subprocess.run(
            ["git", "diff", "--name-only", "HEAD", "--"],
            cwd=str(ORIGIN),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "--"],
            cwd=str(ORIGIN),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
    except OSError:
        return []
    if modified.returncode != 0 or untracked.returncode != 0:
        return []
    changed = {
        line.strip().replace("\\", "/")
        for line in (modified.stdout.splitlines() + untracked.stdout.splitlines())
        if line.strip()
    }
    return sorted(path.as_posix() for path in sources if path.as_posix() in changed)


def _head_rev() -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(ORIGIN),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
    except OSError:
        return "unknown"
    return completed.stdout.strip() or "unknown"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--self-check-only",
        action="store_true",
        help="build the copy and run the self-check without pytest",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="remove the temporary base directory on success (default: keep for evidence)",
    )
    parser.add_argument(
        "pytest_args",
        nargs=argparse.REMAINDER,
        help="arguments after '--' are forwarded verbatim to pytest",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    forwarded = args.pytest_args
    if forwarded and forwarded[0] == "--":
        forwarded = forwarded[1:]
    if not forwarded:
        forwarded = ["-q", "--cov=src", "--cov-report=term-missing"]

    head = _head_rev()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = Path(tempfile.mkdtemp(prefix=f"taffy-isolated-{stamp}-"))
    root = base / "root"
    work = base / "work"
    (work / "tmp").mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(16)

    print(f"[isolated-tests] origin={ORIGIN} head={head}")
    print(f"[isolated-tests] base={base}")
    print(f"[isolated-tests] python={sys.version.split()[0]} exe={sys.executable}")

    try:
        sources = _iter_worktree_sources()
        manifest = _copy_sources(sources, root)
        (root / ".isolation-token").write_text(token, encoding="utf-8")
        (work / "origin.json").write_text(
            json.dumps(
                {"origin": str(ORIGIN), "head": head, "copied_at": stamp},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        (work / "isolation-manifest.json").write_text(
            json.dumps(
                {
                    "files": manifest,
                    "count": len(manifest),
                    "worktree_differs_from_head": _worktree_differs_from_head(sources),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"[isolated-tests] copied {len(manifest)} files (whitelist, worktree content)")

        _assert_no_protected_files(root)
        print("[isolated-tests] self-check 1/4: no protected dotenv/.git in copy — OK")

        env = _build_env(root, work, token)
        if str(ORIGIN) not in sys.path:
            sys.path.insert(0, str(ORIGIN))
        from tests.support import isolation as _iso_entry

        env[_iso_entry.ISOLATION_ORIGIN_ENV] = str(ORIGIN)
        probe = subprocess.run(
            _probe_command(),
            cwd=str(root),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        if probe.returncode != 0:
            print(probe.stdout)
            print(probe.stderr)
            raise RuntimeError("self-check 2/4 failed: probe child did not start")
        payload = json.loads(probe.stdout.strip().splitlines()[-1])
        probe_root = Path(payload["project_root"]).resolve()
        if probe_root != root.resolve():
            raise RuntimeError(
                f"self-check 2/4 failed: imports resolve at {probe_root}, not the copy"
            )
        print(
            "[isolated-tests] self-check 2/4: import root is the copy "
            f"({probe_root}, snapshot_keys={payload['snapshot_keys']}) — OK"
        )

        mismatched = verify_copy_against_manifest(root, manifest)
        if mismatched:
            raise RuntimeError(f"self-check 3/4 failed: manifest mismatch: {mismatched[:5]}")
        print("[isolated-tests] self-check 3/4: manifest re-hash matches replica — OK")

        guard_probe = subprocess.run(
            _guard_selftest_command(),
            cwd=str(root),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        if guard_probe.returncode != 0 or "GUARD-SELFTEST-OK" not in guard_probe.stdout:
            print(guard_probe.stdout)
            print(guard_probe.stderr)
            raise RuntimeError("self-check 4/4 failed: synthetic guard probe refused")
        print("[isolated-tests] self-check 4/4: synthetic guard probe denied as designed — OK")

        if args.self_check_only:
            print("[isolated-tests] self-check-only requested; pytest skipped")
            return 0

        cache_dir = work / "caches" / "pytest"
        coverage_file = work / "caches" / "coverage-data"
        pytest_argv = [
            sys.executable,
            "-m",
            "pytest",
            "-o",
            f"cache_dir={cache_dir}",
            *forwarded,
        ]
        env["COVERAGE_FILE"] = str(coverage_file)
        print(f"[isolated-tests] running: python -m pytest {' '.join(forwarded)}")
        code = _run_tee(pytest_argv, cwd=root, env=env, log_path=work / "pytest.log")
        print(f"[isolated-tests] pytest exit={code} log={work / 'pytest.log'}")
        print(f"[isolated-tests] manifest={work / 'isolation-manifest.json'}")
        if code == 0 and args.cleanup:
            shutil.rmtree(base, ignore_errors=True)
            print("[isolated-tests] cleaned up temporary base")
        else:
            print(f"[isolated-tests] kept base for evidence: {base}")
        return code
    except Exception as exc:
        print(f"[isolated-tests] FAILED: {type(exc).__name__}: {exc}")
        print(f"[isolated-tests] kept base for evidence: {base}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
