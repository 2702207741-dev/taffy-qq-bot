"""Process-level test isolation contract (Python layer only).

This module is deliberately explicit about what it is **not**: it is a
Python-level guard rail for the test suite, not an operating-system
security sandbox.  It cannot contain native extensions, already-running
processes, or the operator's own shell.  What it does:

* prove that application modules are imported from a controlled source
  copy (``PROJECT_ROOT`` relocation), never the production worktree;
* scrub inherited credentials / proxies / path overrides before any
  application import happens;
* deny unexpected filesystem writes, protected-config reads, raw SQLite
  opens, outbound sockets, and child processes outside an explicit
  registry — every denial is recorded in a claimable audit log so a
  swallowed violation still fails the owning test.

Both the pytest parent (via ``tests/conftest.py``) and controlled
children (via :func:`verify_child_contract`) enforce the same contract.
"""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import io
import json
import os
import secrets
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterator

ISOLATION_ROOT_ENV = "TAFFY_ISOLATED_ROOT"
ISOLATION_TOKEN_ENV = "TAFFY_ISOLATION_TOKEN"
ISOLATION_WORK_ENV = "TAFFY_ISOLATION_WORK"
ISOLATION_ORIGIN_ENV = "TAFFY_ISOLATION_ORIGIN"
TOKEN_FILENAME = ".isolation-token"
SAFE_ENTRY_COMMAND = "uv run --frozen python scripts/run_isolated_tests.py"

#: Dead sinkhole for inherited proxy configuration.  Pre-registered so the
#: offline probe path keeps its real fail-fast behaviour (connection
#: refused) instead of tripping the network guard.
SINKHOLE_HOST = "127.0.0.1"
SINKHOLE_PORT = 1

#: Basenames that are never readable outside the controlled trees, even
#: when they are not inside a registered sentinel directory.  Inside the
#: controlled trees synthetic fixtures (``test_config.py`` et al) keep
#: working through the real dotenv parser — parsing is relocated, never
#: stubbed out.
_PROTECTED_BASENAMES = (".env",)
_PROTECTED_PREFIXES = (".env.",)


class IsolationViolationError(RuntimeError):
    """A test attempted to cross the isolation boundary."""


# ─── audit log ──────────────────────────────────────────────────────────

_audit_lock = threading.Lock()
_audit_events: list[dict[str, Any]] = []
_restrictions: list[str] = []
_allowed_counters: dict[str, int] = {}
_ALLOWED_SAMPLE_LIMIT = 3
_thread_state = threading.local()


def _record(kind: str, detail: str, outcome: str) -> dict[str, Any]:
    event = {
        "ts": time.time(),
        "kind": kind,
        "detail": detail,
        "outcome": outcome,  # "blocked" | "allowed"
        "claimed": False,
    }
    with _audit_lock:
        _audit_events.append(event)
    return event


def _record_allowed_sample(kind: str, detail: str) -> None:
    """Record the first few allowed events per kind; count the rest."""
    with _audit_lock:
        seen = _allowed_counters.get(kind, 0)
        _allowed_counters[kind] = seen + 1
        if seen < _ALLOWED_SAMPLE_LIMIT:
            _audit_events.append(
                {
                    "ts": time.time(),
                    "kind": kind,
                    "detail": detail,
                    "outcome": "allowed",
                    "claimed": True,
                }
            )


def allowed_counters() -> dict[str, int]:
    with _audit_lock:
        return dict(_allowed_counters)


def audit_events() -> list[dict[str, Any]]:
    with _audit_lock:
        return [dict(event) for event in _audit_events]


def audit_snapshot_size() -> int:
    with _audit_lock:
        return len(_audit_events)


def claim_events_since(mark: int, kind: str | None = None) -> list[dict[str, Any]]:
    """Mark events recorded since ``mark`` as claimed; return them.

    When ``kind`` is given only matching blocked events are claimed —
    anything else stays unclaimed so the session enforcer still fails.
    """
    with _audit_lock:
        fresh = _audit_events[mark:]
        collected: list[dict[str, Any]] = []
        for event in fresh:
            if event["outcome"] == "blocked" and (kind is None or event["kind"] == kind):
                event["claimed"] = True
                collected.append(dict(event))
        return collected


def unclaimed_blocked_since(mark: int) -> list[dict[str, Any]]:
    with _audit_lock:
        return [
            dict(event)
            for event in _audit_events[mark:]
            if event["outcome"] == "blocked" and not event["claimed"]
        ]


@contextlib.contextmanager
def expect_blocked(kind: str) -> Iterator[list[dict[str, Any]]]:
    """Claim boundary denials of exactly ``kind`` raised inside the block.

    The kind is mandatory: an unexpected denial of any other kind stays
    unclaimed and fails the test at teardown.  The test must still assert
    the raise itself (``pytest.raises``); this only handles bookkeeping.
    """
    mark = audit_snapshot_size()
    collected: list[dict[str, Any]] = []
    try:
        yield collected
    finally:
        collected.extend(claim_events_since(mark, kind))


def session_audit_failures() -> list[dict[str, Any]]:
    """Every unclaimed blocked event process-wide (the session gate input)."""
    with _audit_lock:
        return [
            dict(event)
            for event in _audit_events
            if event["outcome"] == "blocked" and not event["claimed"]
        ]


def session_audit_check() -> None:
    """Fail when any boundary denial was never claimed by its test.

    Called from a session-scoped teardown fixture so collection-time or
    swallowed violations fail the run instead of printing a summary.
    """
    pending = session_audit_failures()
    if pending:
        preview = "; ".join(f"{item['kind']}:{item['detail']}" for item in pending[:5])
        raise IsolationViolationError(
            f"isolation: {len(pending)} unclaimed boundary denials: {preview}"
        )


def note_restriction(text: str) -> None:
    with _audit_lock:
        _restrictions.append(text)


def restrictions() -> list[str]:
    with _audit_lock:
        return list(_restrictions)


# ─── path boundary ──────────────────────────────────────────────────────

_allowed_dirs: list[Path] = []
_sentinels: list[Path] = []
#: Production worktree areas that are denied for private reads without any
#: per-test registration: the origin ``data/`` tree, origin dotenv files,
#: and origin SQLite files.  Writes there were already denied (the origin
#: is never an allowed tree); this closes the read side automatically.
_protected_roots: list[Path] = []
_state_lock = threading.Lock()
_installed = False


def _norm(path: Path | str) -> str:
    return os.path.normcase(str(path))


def _real_path(candidate: Path | str) -> Path:
    """Resolve ``..`` / symlinks of the nearest existing ancestor.

    ``Path.resolve(strict=False)`` normalises ``..`` lexically but does
    not chase symlinks through non-existent leaves; chasing the nearest
    existing ancestor closes symlink escapes for paths that exist while
    staying total for paths that do not.
    """
    path = Path(candidate)
    try:
        return path.resolve(strict=False)
    except (OSError, ValueError):
        return Path(os.path.abspath(str(path)))


def _nearest_real(path: Path) -> Path:
    current = path
    while True:
        try:
            if current.exists() or current.is_symlink():
                try:
                    return current.resolve(strict=True)
                except OSError:
                    return _real_path(current)
        except (OSError, ValueError):
            pass
        try:
            parent = current.parent
        except (OSError, ValueError):
            return _real_path(path)
        if parent == current:
            return _real_path(path)
        current = parent


def _effective_path(candidate: Path | str) -> Path:
    try:
        path = _real_path(candidate)
        try:
            if path.exists() or path.is_symlink():
                return _nearest_real(path)
        except OSError:
            pass
        try:
            # Nearest existing ancestor plus the *full* lexical tail.  A
            # naive ``ancestor / path.name`` would drop intermediate
            # segments of not-yet-existing directories, so
            # ``.../protected/null`` inside a sentinel could resolve to
            # ``.../null`` and be wrongly allowed (R2).
            existing = _nearest_real(path.parent)
            tail = os.path.relpath(str(path), str(existing))
            return existing / tail
        except OSError:
            return path
    except Exception as exc:
        # Fail closed: a path we cannot resolve is denied, never bypassed.
        _record("path-resolve", str(candidate)[:200], "blocked")
        raise IsolationViolationError(
            f"isolation: unresolvable path denied: {str(candidate)[:120]}"
        ) from exc


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _boundary_roots() -> list[Path]:
    with _state_lock:
        return list(_allowed_dirs)


def _is_sentinel(path: Path) -> bool:
    with _state_lock:
        sentinels = list(_sentinels)
    return any(_is_within(path, entry) or _norm(path) == _norm(entry) for entry in sentinels)


def _is_allowed(path: Path) -> bool:
    if _is_sentinel(path):
        return False
    return any(_is_within(path, root) for root in _boundary_roots())


_PROTECTED_DB_SUFFIXES = (".db", ".db-wal", ".db-shm", ".db-journal")


def _protected_name(name: str) -> bool:
    lowered = name.lower()
    return lowered in _PROTECTED_BASENAMES or lowered.startswith(_PROTECTED_PREFIXES)


def protect_origin_tree(origin: Path | str) -> Path:
    """Auto-deny private reads under the production worktree (R2d).

    No per-test sentinel needed: ``<origin>/data/**``, ``<origin>/.env*``
    and ``<origin>/**/*.db*`` reads are refused process-wide.  Ordinary
    source reads under the origin stay allowed (nothing in the suite
    needs them, but denying them is not required to prove isolation and
    would risk false positives).
    """
    resolved = _real_path(origin)
    with _state_lock:
        if resolved not in _protected_roots:
            _protected_roots.append(resolved)
    return resolved


def _is_under_protected_root(path: Path) -> bool:
    with _state_lock:
        roots = list(_protected_roots)
    return any(_is_within(path, root) or _norm(path) == _norm(root) for root in roots)


def _origin_protected_read(path: Path, leaf_name: str) -> bool:
    with _state_lock:
        roots = list(_protected_roots)
    for root in roots:
        if _is_within(path, root) or _norm(path) == _norm(root):
            if _protected_name(leaf_name):
                return True
            if _is_within(path, root / "data"):
                return True
            if leaf_name.lower().endswith(_PROTECTED_DB_SUFFIXES):
                return True
    return False


def register_allowed_dir(path: Path | str) -> Path:
    resolved = _real_path(path)
    with _state_lock:
        if resolved not in _allowed_dirs:
            _allowed_dirs.append(resolved)
    return resolved


@contextlib.contextmanager
def register_sentinel(path: Path | str) -> Iterator[Path]:
    """Forbid every guarded operation under ``path`` (takes precedence)."""
    resolved = add_sentinel(path)
    try:
        yield resolved
    finally:
        with _state_lock:
            _sentinels[:] = [entry for entry in _sentinels if entry != resolved]


def add_sentinel(path: Path | str) -> Path:
    """Register a sentinel without a context (for controlled children)."""
    resolved = _real_path(path)
    with _state_lock:
        if resolved not in _sentinels:
            _sentinels.append(resolved)
    return resolved


def _is_null_device(candidate: Path | str) -> bool:
    """Only the platform's real null device bypasses the boundary.

    Windows reserves the three-letter name ``NUL`` (bare, NT device-path
    prefixed, or at a drive root): a drive-root ``nul`` names the device
    while ``null`` is an ordinary file.  On POSIX the device lives at
    exactly ``/dev/null``; a bare ``null`` or ``/null`` is an ordinary
    file and stays subject to the boundary.  Reserved-looking nested
    leaves are still treated as ordinary paths (denied outside the
    trees): fail-closed beats device-semantics fidelity for nested
    spellings.
    """
    if os.name == "nt":
        sep = chr(92)
        text = os.path.normcase(str(candidate)).replace("/", sep)
        text = text.rstrip(sep)
        for prefix in (sep + sep + "." + sep, sep + sep + "?" + sep):
            if text.startswith(prefix):
                text = text[len(prefix) :]
                break
        if text == "nul":
            return True
        # Drive-root form: letter, colon, separator, then ``nul``.
        if len(text) == 6 and text[1] == ":" and text[2] == sep and text[3:] == "nul":
            return True
        return False
    # POSIX: compare the candidate directly (never after a Windows
    # separator conversion); the device is exactly ``/dev/null``.
    return os.path.normcase(str(candidate)) == os.path.normcase("/dev/null")


def check_read(path: Path | str, *, kind: str = "file-read") -> Path:
    if _is_null_device(path):
        return Path(str(path))
    effective = _effective_path(path)
    if _is_sentinel(effective):
        _record(kind, str(effective), "blocked")
        raise IsolationViolationError(f"isolation: read denied under sentinel: {effective}")
    leaf = Path(str(path)).name
    if _origin_protected_read(effective, leaf):
        _record(kind, str(effective), "blocked")
        raise IsolationViolationError(
            f"isolation: production-tree private read denied: {effective}"
        )
    if _is_under_protected_root(effective):
        # Ordinary production-source reads stay allowed (byte-compare
        # evidence) but are sampled so the audit names every touched
        # production file (R2).
        _record_allowed_sample("origin-source-read", str(effective))
    if _protected_name(leaf) and not _is_allowed(effective):
        _record(kind, str(effective), "blocked")
        raise IsolationViolationError(f"isolation: protected config read denied: {effective}")
    return effective


def check_write(path: Path | str, *, kind: str = "file-write") -> Path:
    if _is_null_device(path):
        return Path(str(path))
    effective = _effective_path(path)
    if not _is_allowed(effective):
        _record(kind, str(effective), "blocked")
        raise IsolationViolationError(
            f"isolation: write denied outside controlled trees: {effective}"
        )
    return effective


# ─── network registry ───────────────────────────────────────────────────

_allowed_endpoints: set[tuple[str, int]] = set()


def _endpoint_key(host: str, port: int) -> tuple[str, int]:
    return (str(host).strip().lower(), int(port))


def register_local_endpoint(host: str, port: int) -> tuple[str, int]:
    key = _endpoint_key(host, port)
    with _state_lock:
        _allowed_endpoints.add(key)
    return key


def registered_endpoints() -> list[tuple[str, int]]:
    with _state_lock:
        return sorted(_allowed_endpoints)


@contextlib.contextmanager
def allow_local_port(port: int, host: str = "127.0.0.1") -> Iterator[tuple[str, int]]:
    """Lease one test-owned ephemeral port for real loopback TCP.

    Registration is eager (effective on call, not on first use) and
    revoked on scope exit, so ``with`` blocks have precise allow windows.
    """
    key = register_local_endpoint(host, port)
    try:
        yield key
    finally:
        with _state_lock:
            _allowed_endpoints.discard(key)
        _record("network", f"{host}:{port}", "revoked")


def check_connect(host: str, port: int) -> None:
    key = _endpoint_key(host, port)
    with _state_lock:
        allowed = key in _allowed_endpoints
    if allowed:
        _record_allowed_sample("network", f"{host}:{port}")
        return
    _record("network", f"{host}:{port}", "blocked")
    raise IsolationViolationError(f"isolation: outbound connection denied: {host}:{port}")


# ─── subprocess registry ────────────────────────────────────────────────

_allowed_argv: set[tuple[str, ...]] = set()


def register_subprocess(argv: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    key = tuple(str(part) for part in argv)
    with _state_lock:
        _allowed_argv.add(key)
    return key


def register_subprocess_run(
    argv: list[str] | tuple[str, ...], *, cwd: Path | str, **kwargs: Any
) -> subprocess.CompletedProcess[str]:
    """Run one pre-registered git command inside a synthetic repo.

    Unlike :func:`controlled_git` (setup-only ``init``/``add``), this
    helper also permits the read-only ``hash-object`` object-store write
    that the history-scanner fixtures need.  Both helpers share the same
    argv policy (:func:`_validate_controlled_git_argv`): fixed option
    forms only, path arguments inside ``cwd``, and a ``GIT_*``-scrubbed
    environment merged *before* stripping.
    """
    argv_list = [str(part) for part in argv]
    if not argv_list or argv_list[0] != "git":
        _record("subprocess", " ".join(argv_list[:2]), "blocked")
        raise IsolationViolationError("isolation: registered run must be a git command")
    effective = _effective_path(cwd)
    if not _is_allowed(effective):
        _record("subprocess", f"cwd={effective}", "blocked")
        raise IsolationViolationError(f"isolation: subprocess cwd denied: {effective}")
    try:
        _validate_controlled_git_argv(argv_list, effective)
    except IsolationViolationError:
        _record("subprocess", " ".join(argv_list[:3]), "blocked")
        raise
    caller_env = kwargs.pop("env", None)
    env = _git_guard_env(_scrubbed_git_env(caller_env))
    # Register only after every check (argv, cwd, env) succeeded: a
    # refused command must never leave an entry in the allow-list that a
    # later guard check could re-admit.
    key = register_subprocess(argv_list)
    _record("subprocess", " ".join(key[:4]), "allowed")
    return _persistent_run()(
        [str(part) for part in key],
        cwd=str(effective),
        env=env,
        capture_output=True,
        **kwargs,
    )


def registered_subprocesses() -> list[tuple[str, ...]]:
    with _state_lock:
        return sorted(_allowed_argv)


def check_subprocess(argv: list[str] | tuple[str, ...], *, cwd: Path | str | None) -> None:
    key = tuple(str(part) for part in argv)
    active_repo = _authorized_history_fixture()
    fixture_form = _is_history_fixture_commit(list(key)) or _is_history_fixture_scan(list(key))
    if fixture_form:
        # The fixed commit/scan forms exist *only* inside an active
        # history-fixture scope whose initialisation fully succeeded, and
        # only with an explicit cwd that is exactly the scoped repo.  A
        # bare allow-list hit must never widen this: cwd=None (inheriting
        # the runner's cwd) is refused here before the caller reaches any
        # executor (R8-1).  Occupancy alone (init still running or failed)
        # authorises nothing (R10-2).
        if active_repo is None:
            _record("subprocess", " ".join(key[:4]), "blocked")
            raise IsolationViolationError(
                "isolation: fixture commit/scan requires an active history-fixture scope"
            )
        if cwd is None:
            _record("subprocess", " ".join(key[:4]), "blocked")
            raise IsolationViolationError(
                "isolation: fixture commit/scan requires the fixture repo as explicit cwd"
            )
        effective = _effective_path(cwd)
        if _norm(effective) != _norm(active_repo):
            _record("subprocess", f"fixture git cwd={effective}", "blocked")
            raise IsolationViolationError(
                "isolation: fixture git cwd must be the scoped fixture repo"
            )
        _record("subprocess", " ".join(key[:4]), "allowed")
        return
    with _state_lock:
        allowed = key in _allowed_argv
    if not allowed:
        _record("subprocess", " ".join(key[:4]), "blocked")
        raise IsolationViolationError("isolation: unregistered subprocess denied")
    if cwd is not None:
        effective = _effective_path(cwd)
        if not _is_allowed(effective):
            _record("subprocess", f"cwd={effective}", "blocked")
            raise IsolationViolationError(f"isolation: subprocess cwd denied: {effective}")


#: Synthetic-temp-repo git fixture (R4): only repository *setup* reads are
#: ever allowed here.  ``commit`` (and everything else) stays denied so
#: the batch commit ban is enforced by the guard, not by convention.
CONTROLLED_GIT_SUBCOMMANDS = ("init", "add")

#: git commands permitted through register_subprocess_run (no commit).
CONTROLLED_GIT_ALLOWLIST = ("init", "add", "hash-object")

#: Per-subcommand option allow-lists — only the exact fixed forms the
#: fixtures use.  Any other option (e.g. ``--separate-git-dir=...``) is
#: refused; no general git argument parser is built here.
GIT_ALLOWED_OPTIONS = {
    "init": frozenset({"-q"}),
    "add": frozenset(),
    "hash-object": frozenset({"-w", "--stdin"}),
}
#: Subcommands that take one free path argument (the file to stage).
GIT_PATH_ARG_SUBCOMMANDS = frozenset({"add"})

#: Fixed commit form for the original history-scanner E2E fixtures.  The
#: ``-c key=value`` identity options sit between ``git`` and the
#: ``commit`` subcommand (git's own ``commit -c`` would mean "reuse a
#: commit message", which is not what the fixtures do).  The trailing
#: ``-c`` keys explicitly disable signing and fsmonitor so no external
#: user/system configuration can add side effects to the commit even if
#: a configuration source were ever reached.  This form is NOT part of
#: CONTROLLED_GIT_SUBCOMMANDS / CONTROLLED_GIT_ALLOWLIST: it is only
#: executable inside an explicit ``history_fixture_scope`` for the one
#: synthetic repository the scope itself created, and the scope revokes
#: it on exit.
_HISTORY_FIXTURE_COMMIT_ARGV = (
    "git",
    "-c",
    "user.name=Secret Scan Test",
    "-c",
    "user.email=secret-scan@example.invalid",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "tag.gpgsign=false",
    "-c",
    "core.fsmonitor=false",
    "commit",
    "-qm",
    "fixture",
)

#: Fixed scanner git forms the original E2E run through the real
#: ``check_secrets`` chain (ls-files -> rev-list -> cat-file --batch).
#: Like the commit form they are scoped, temporary registrations only.
_HISTORY_FIXTURE_SCAN_ARGVS = (
    ("git", "ls-files", "-z"),
    ("git", "rev-list", "--objects", "--all"),
    ("git", "cat-file", "--batch"),
)

#: Active synthetic-repo scope for the original history E2E (or None).
#: Guarded by _state_lock; set/cleared by history_fixture_scope.
#: This is the *occupancy* slot: it is claimed atomically before init and
#: released on every exit path.  It alone does NOT authorise execution.
_active_history_fixture_repo: Path | None = None

#: The *authorised* repository: set only after init succeeded and every
#: ``.git`` check passed (and together with the fixed-form registrations).
#: The fixture forms execute only against this path.  Guarded by
#: _state_lock.
_authorized_history_fixture_repo: Path | None = None

#: Serialises the "check and claim" of the fixture slot.  Distinct from
#: _state_lock (which guards the data itself): the claim lock is held
#: across the pre-occupation so two threads cannot both observe an empty
#: slot, and it must never be held while calling helpers that take
#: _state_lock or spawn processes.
_fixture_claim_lock = threading.Lock()


def _active_history_fixture() -> Path | None:
    """The occupied (claimed) repo — occupancy only, not authorisation."""
    with _state_lock:
        return _active_history_fixture_repo


def _set_active_history_fixture(repo: Path | None) -> None:
    with _state_lock:
        global _active_history_fixture_repo
        _active_history_fixture_repo = repo


def _authorized_history_fixture() -> Path | None:
    """The repo whose fixture forms may execute, or None."""
    with _state_lock:
        return _authorized_history_fixture_repo


def _set_authorized_history_fixture(repo: Path | None) -> None:
    with _state_lock:
        global _authorized_history_fixture_repo
        _authorized_history_fixture_repo = repo


def _is_history_fixture_commit(argv: list[str]) -> bool:
    return tuple(argv) == _HISTORY_FIXTURE_COMMIT_ARGV


def _is_history_fixture_scan(argv: list[str]) -> bool:
    return tuple(argv) in _HISTORY_FIXTURE_SCAN_ARGVS


def _first_link_component(path: Path | str) -> Path | None:
    """Return the first component of the ORIGINAL path spelling that is a
    symlink, junction or any other reparse point, else ``None``.

    Resolution-based link checks lose the original link: by the time a
    path is resolved, a symlink/junction ancestor has already been folded
    into an ordinary-looking target, so ``resolved.is_symlink()`` is
    ``False`` even though the caller walked through a link (R8 fix).  This
    check therefore inspects the *unresolved* components with lstat
    semantics — ``st_reparse_tag`` covers symlinks and junctions on
    Windows, ``is_symlink()`` covers POSIX links.
    """
    probe = Path(path)
    for candidate in (probe, *probe.parents):
        try:
            if candidate.is_symlink():
                return candidate
            stat_result = os.lstat(candidate)
        except (OSError, ValueError):
            continue
        if int(getattr(stat_result, "st_reparse_tag", 0) or 0):
            return candidate
    return None


def history_fixture_stage(relative: str) -> Path:
    """Stage one in-repo file for the active history fixture.

    The add phase goes through the same controlled entry as the init
    phase (:func:`controlled_git`: fixed ``git add <one path in cwd>``
    form, scrubbed + hardened child environment, cwd bound to the
    created repo) — fixture setup is never allowed around the guards.
    The relative name must be a plain in-repo path: absolute spellings
    and any ``..`` component are refused before the controlled entry is
    consulted, and :func:`controlled_git` re-checks the resolved path
    against the repo afterwards.
    """
    repo = _authorized_history_fixture()
    if repo is None:
        _record("subprocess", "history-fixture stage without scope", "blocked")
        raise IsolationViolationError("isolation: staging requires an active history-fixture scope")
    name = str(relative).replace("\\", "/")
    components = name.split("/")
    if name.startswith("/") or (len(name) > 1 and name[1] == ":") or ".." in components:
        _record("subprocess", f"history-fixture stage path: {name}", "blocked")
        raise IsolationViolationError(
            f"isolation: history-fixture stage path must stay inside the repo: {name}"
        )
    controlled_git("add", name, cwd=repo)
    return repo / name


@contextlib.contextmanager
def history_fixture_scope() -> Iterator[Path]:
    """Create, authorise and clean up one synthetic history-scanner repo.

    The scope does **not** accept a caller-named repository: it creates
    the repository itself (fresh temp directory inside the controlled
    work tree, ``git init`` through the controlled entry) so the repo's
    origin is proven, not assumed.  Every path the scope touches is
    checked on its original spelling for links/junctions *before* any
    resolution, and the created ``.git`` must be a real directory (a
    ``.git`` *file* would be a gitdir redirect to a shared worktree).
    Nested or overlapping scopes are refused outright — no general
    permission framework is built here.

    The created directory is deliberately left in place inside the
    controlled work tree when the scope ends (the runner archives or
    discards that tree as a whole): no directory deletion happens in
    test code, so a cleanup bug could never escape the controlled tree.

    While active, only these exact argv forms run, and only with an
    explicit ``cwd`` exactly equal to the created repo:

    * ``git -c user.name=Secret Scan Test -c user.email=secret-scan@example.invalid
      -c commit.gpgsign=false -c tag.gpgsign=false -c core.fsmonitor=false commit -qm fixture``
    * ``git ls-files -z`` / ``git rev-list --objects --all`` /
      ``git cat-file --batch``

    The child environment is scrubbed of ``GIT_*`` (case-insensitively)
    and then pointed at controlled empty config/template/home assets, so
    external git configuration, hooks, templates, signing and filters
    cannot add behaviour to the fixture commands.  Every registration is
    revoked on normal exit and on exception, so the permission cannot be
    replayed after the scope ends; the commit form is never added to the
    long-lived allow-lists.

    The fixture slot is claimed atomically: under a dedicated claim lock
    the scope checks for an existing occupant and pre-occupies the slot
    *before* any initialisation, so a concurrent contender is refused
    instead of racing past the check.  Initialisation failure, normal
    exit and exception all release the claim.  Occupancy is *not*
    authorisation: the fixed forms only execute once init succeeded and
    every ``.git`` check passed.  The ORIGINAL ``work_text`` spelling is
    inspected for links before any resolution or boundary decision.
    """
    with _fixture_claim_lock:
        if _active_history_fixture() is not None:
            _record("subprocess", "history-fixture scope overlap", "blocked")
            raise IsolationViolationError(
                "isolation: nested or overlapping history-fixture scopes refused"
            )
        work_text = os.environ.get(ISOLATION_WORK_ENV, "").strip()
        if not work_text:
            raise IsolationViolationError(
                "isolation: history fixture requires the controlled work tree"
            )
        # Original-spelling link check FIRST: resolving before this check
        # would fold a link ancestor into an ordinary-looking path.
        link = _first_link_component(work_text)
        if link is not None:
            _record("subprocess", f"history-fixture work-tree link: {link}", "blocked")
            raise IsolationViolationError(
                f"isolation: history-fixture work tree must not walk a link: {link}"
            )
        work_root = _real_path(work_text)
        if not _is_allowed(work_root):
            _record("subprocess", f"history-fixture work tree denied: {work_root}", "blocked")
            raise IsolationViolationError(
                "isolation: history-fixture work tree must be a controlled tree"
            )
        root_text = os.environ.get(ISOLATION_ROOT_ENV, "").strip()
        origin_text = os.environ.get(ISOLATION_ORIGIN_ENV, "").strip()
        if root_text and (
            _is_within(work_root, _real_path(root_text))
            or _norm(work_root) == _norm(_real_path(root_text))
        ):
            raise IsolationViolationError(
                "isolation: history fixture must not live in the isolated root"
            )
        if origin_text and (
            _is_within(work_root, _real_path(origin_text))
            or _norm(work_root) == _norm(_real_path(origin_text))
        ):
            raise IsolationViolationError("isolation: history fixture must not live in the origin")
        link = _first_link_component(work_root)
        if link is not None:
            _record("subprocess", f"history-fixture work-tree link: {link}", "blocked")
            raise IsolationViolationError(
                f"isolation: history-fixture work tree must not walk a link: {link}"
            )
        # Pre-occupy the slot *before* initialisation: the claim lock makes
        # the check-and-occupy pair atomic against concurrent contenders.
        repo = Path(tempfile.mkdtemp(dir=str(work_root), prefix="history-fixture-"))
        _set_active_history_fixture(repo)

    registered: list[tuple[str, ...]] = []
    try:
        link = _first_link_component(repo)
        if link is not None:
            _record("subprocess", f"history-fixture repo link: {link}", "blocked")
            raise IsolationViolationError(
                f"isolation: history-fixture repo path walks a link: {link}"
            )
        if os.path.normcase(str(_real_path(repo))) != os.path.normcase(str(repo)):
            raise IsolationViolationError(
                "isolation: history-fixture repo path resolves elsewhere (reparse ancestor?)"
            )
        if not _is_allowed(repo):
            raise IsolationViolationError(
                "isolation: history-fixture repo must live in the controlled temp tree"
            )
        completed = controlled_git("init", "-q", cwd=repo)
        if completed.returncode != 0:
            raise IsolationViolationError(
                f"isolation: fixture git init failed: {completed.stderr[-200:]}"
            )
        git_dir = repo / ".git"
        if (
            not git_dir.is_dir()
            or git_dir.is_symlink()
            or git_dir.is_file()
            or _first_link_component(git_dir) is not None
        ):
            raise IsolationViolationError(
                "isolation: fixture git init must produce a real .git directory "
                "(no missing .git, no gitdir redirects, no links)"
            )
        for argv in (*_HISTORY_FIXTURE_SCAN_ARGVS, _HISTORY_FIXTURE_COMMIT_ARGV):
            key = register_subprocess(list(argv))
            registered.append(key)
            _record("subprocess", " ".join(key[:4]), "allowed")
        # Authorisation is published only now: init succeeded and every
        # .git check passed.  Occupancy alone never authorises execution.
        _set_authorized_history_fixture(repo)
        yield repo
    finally:
        with _state_lock:
            for key in registered:
                _allowed_argv.discard(key)
            _record("subprocess", "history-fixture scope revoked", "revoked")
        _set_authorized_history_fixture(None)
        _set_active_history_fixture(None)


def _scrubbed_git_env(caller_env: dict[str, str] | None = None) -> dict[str, str]:
    """Merge caller env first, then strip every ``GIT_*`` variable.

    Stripping *after* the merge guarantees a caller-supplied
    ``GIT_OBJECT_DIRECTORY`` (or any other redirection variable) can
    never reach the underlying executor.  The match is **case
    insensitive**: on Windows the process environment is case-preserving
    but case-insensitive, so a lower-case ``git_dir`` reaches git.exe as
    ``GIT_DIR``; stripping only the exact upper-case spelling leaves that
    redirection live (R8-4).
    """
    env = dict(os.environ)
    if caller_env:
        env.update(caller_env)
    for env_key in list(env):
        if env_key.upper().startswith("GIT_"):
            del env[env_key]
    return env


def _git_guard_assets() -> Path:
    """Create (once per process) the guard assets under the work tree.

    Deleting ``GIT_*`` does not stop git from reading *system* and *user*
    configuration (hooks, templates, signing, filters).  The guard env
    therefore points every configuration source git honours at controlled
    empty assets inside the work tree: an empty file for global/system
    config, an empty template directory, an empty HOME/USERPROFILE.

    Every boundary decision runs **before** any write: the work tree must
    be an allowed tree (not the root, not the origin) whose original
    spelling walks no link, and each asset is created through the original
    primitives only after its path has been verified.  Pre-existing assets
    are *never* trusted by ``exists()`` alone: directories must be real,
    non-reparse directories, the config file must be a regular file of
    exactly zero bytes, and the template/home directories must be empty —
    a tampered asset is refused instead of reused.
    """
    work_text = os.environ.get(ISOLATION_WORK_ENV, "").strip()
    if not work_text:
        raise IsolationViolationError("isolation: git guard env requires the controlled work tree")
    # Original-spelling link check FIRST (before resolving): a junctioned or
    # symlinked work tree must be refused before any directory or config
    # write happens.
    if _first_link_component(work_text) is not None:
        _record("subprocess", f"git-guard work tree link: {work_text}", "blocked")
        raise IsolationViolationError(
            f"isolation: git guard work tree must not walk a link: {work_text}"
        )
    work_root = _real_path(work_text)
    if not _is_allowed(work_root):
        _record("subprocess", f"git-guard work tree denied: {work_root}", "blocked")
        raise IsolationViolationError(
            f"isolation: git guard assets must live in a controlled tree: {work_root}"
        )
    root_text = os.environ.get(ISOLATION_ROOT_ENV, "").strip()
    origin_text = os.environ.get(ISOLATION_ORIGIN_ENV, "").strip()
    if root_text and (
        _is_within(work_root, _real_path(root_text))
        or _norm(work_root) == _norm(_real_path(root_text))
    ):
        raise IsolationViolationError("isolation: git guard assets must not live in the root")
    if origin_text and (
        _is_within(work_root, _real_path(origin_text))
        or _norm(work_root) == _norm(_real_path(origin_text))
    ):
        raise IsolationViolationError("isolation: git guard assets must not live in the origin")
    if _first_link_component(work_root) is not None:
        _record("subprocess", f"git-guard work tree link: {work_root}", "blocked")
        raise IsolationViolationError(
            f"isolation: git guard work tree must not walk a link: {work_root}"
        )

    guard = work_root / "git-guard"
    home = guard / "home"
    template = guard / "empty-template"
    empty_config = guard / "empty.gitconfig"

    def _ensure_real_dir(directory: Path, label: str, *, require_empty: bool) -> None:
        if _first_link_component(directory) is not None:
            _record("subprocess", f"git-guard {label} link: {directory}", "blocked")
            raise IsolationViolationError(
                f"isolation: git guard {label} must not walk a link: {directory}"
            )
        if not directory.exists():
            _orig_mkdir(str(directory))
            return
        # Pre-existing: verify it is a real directory, not a reparse point.
        if directory.is_symlink() or not directory.is_dir():
            _record("subprocess", f"git-guard {label} not a directory: {directory}", "blocked")
            raise IsolationViolationError(
                f"isolation: git guard {label} must be a real directory: {directory}"
            )
        if _first_link_component(directory) is not None:
            _record("subprocess", f"git-guard {label} reparse: {directory}", "blocked")
            raise IsolationViolationError(
                f"isolation: git guard {label} must not be a reparse point: {directory}"
            )
        if require_empty and _orig_listdir(str(directory)):
            _record("subprocess", f"git-guard {label} not empty: {directory}", "blocked")
            raise IsolationViolationError(
                f"isolation: git guard {label} must stay empty: {directory}"
            )

    def _ensure_real_empty_file(config: Path) -> None:
        if _first_link_component(config) is not None:
            _record("subprocess", f"git-guard config link: {config}", "blocked")
            raise IsolationViolationError(
                f"isolation: git guard config must not walk a link: {config}"
            )
        if not config.exists():
            _orig_open(str(config), "w").close()
            return
        if config.is_symlink() or not config.is_file():
            _record("subprocess", f"git-guard config not a regular file: {config}", "blocked")
            raise IsolationViolationError(
                f"isolation: git guard config must be a regular file: {config}"
            )
        if int(config.stat().st_size) != 0:
            _record("subprocess", f"git-guard config not empty: {config}", "blocked")
            raise IsolationViolationError(f"isolation: git guard config must stay empty: {config}")

    _ensure_real_dir(guard, "directory", require_empty=False)
    _ensure_real_dir(home, "home", require_empty=True)
    _ensure_real_dir(template, "template", require_empty=True)
    _ensure_real_empty_file(empty_config)
    return guard


def _git_guard_env(scrubbed: dict[str, str]) -> dict[str, str]:
    """Layer the explicit git hardening keys onto a scrubbed environment.

    Applied by *every* execution path (controlled entries and the bare
    ``subprocess`` guards alike), so no single entry can be blamed for
    the environment contract and none can be bypassed around it.
    """
    guard = _git_guard_assets()
    home = str(guard / "home")
    empty_config = str(guard / "empty.gitconfig")
    scrubbed.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": empty_config,
            "GIT_CONFIG_SYSTEM": empty_config,
            "GIT_TEMPLATE_DIR": str(guard / "empty-template"),
            "HOME": home,
            "USERPROFILE": home,
            "XDG_CONFIG_HOME": home,
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return scrubbed


def _validate_controlled_git_argv(argv: list[str], effective_cwd: Path) -> None:
    """Shared argv policy for both git helpers (no execution side effects).

    Rules: subcommand must be allow-listed; every ``-`` option must be in
    the subcommand's fixed allow-list (so ``--separate-git-dir=...`` and
    friends are refused, not silently skipped); free path arguments are
    allowed only for ``add`` (exactly one, resolving inside cwd).
    """
    subcommand = argv[1] if len(argv) > 1 else ""
    if subcommand not in CONTROLLED_GIT_ALLOWLIST:
        raise IsolationViolationError(f"isolation: git {subcommand or '<none>'} not in allow-list")
    allowed_options = GIT_ALLOWED_OPTIONS.get(subcommand, frozenset())
    free_args: list[str] = []
    for part in argv[2:]:
        if part.startswith("-"):
            if part not in allowed_options:
                raise IsolationViolationError(
                    f"isolation: git {subcommand} option not allowed: {part}"
                )
        else:
            free_args.append(part)
    if subcommand in GIT_PATH_ARG_SUBCOMMANDS:
        if len(free_args) != 1:
            raise IsolationViolationError(
                f"isolation: git {subcommand} requires exactly one path argument"
            )
        candidate = _real_path(effective_cwd / free_args[0])
        if not _is_within(candidate, effective_cwd):
            raise IsolationViolationError(
                f"isolation: git {subcommand} path escapes repo: {free_args[0]}"
            )
    elif free_args:
        raise IsolationViolationError(f"isolation: git {subcommand} takes no path arguments")


def controlled_git(
    *args: str, cwd: Path | str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run a scoped git setup command inside a synthetic temp repository.

    Allowed form only: ``git init -q`` / ``git add <path-in-cwd>`` with
    ``cwd`` under the controlled work tree (never the isolated root,
    never the origin).  Everything else — notably ``git commit`` and any
    option outside the fixed per-subcommand allow-list — is refused with
    an audited denial, keeping every historical scan assertion intact
    while no commit can execute under this batch's ban.  The child
    environment is scrubbed of ``GIT_*`` after merging the caller's.
    """
    argv = ["git", *(str(part) for part in args)]
    work_text = os.environ.get(ISOLATION_WORK_ENV, "").strip()
    root_text = os.environ.get(ISOLATION_ROOT_ENV, "").strip()
    origin_text = os.environ.get(ISOLATION_ORIGIN_ENV, "").strip()
    try:
        effective_cwd = _effective_path(cwd)
    except IsolationViolationError:
        raise
    except Exception as exc:
        _record("subprocess", f"git cwd={str(cwd)[:120]}", "blocked")
        raise IsolationViolationError("isolation: git cwd denied") from exc
    scoped = bool(work_text) and _is_within(effective_cwd, _real_path(work_text))
    if root_text and (
        _is_within(effective_cwd, _real_path(root_text))
        or _norm(effective_cwd) == _norm(_real_path(root_text))
    ):
        scoped = False
    if origin_text and (
        _is_within(effective_cwd, _real_path(origin_text))
        or _norm(effective_cwd) == _norm(_real_path(origin_text))
    ):
        scoped = False
    subcommand = str(args[0]) if args else ""
    if not scoped or subcommand not in CONTROLLED_GIT_SUBCOMMANDS:
        # Narrow scoped exception: the original history E2E commit form is
        # allowed only for the *authorised* fixture repository (init fully
        # succeeded and every .git check passed).  Occupancy alone — init
        # still paused or failed — authorises nothing (R11-1).  The commit
        # form is never added to the long-lived allow-lists and the scope
        # revokes itself on exit.
        authorized_repo = _authorized_history_fixture()
        if (
            authorized_repo is not None
            and _norm(effective_cwd) == _norm(authorized_repo)
            and _is_history_fixture_commit(argv)
        ):
            env = _git_guard_env(_scrubbed_git_env())
            _record("subprocess", "git commit (history-fixture scope)", "allowed")
            return _persistent_run()(
                list(_HISTORY_FIXTURE_COMMIT_ARGV),
                cwd=str(effective_cwd),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=check,
            )
        _record("subprocess", f"git {subcommand}".strip() or "git", "blocked")
        raise IsolationViolationError(
            "isolation: git refused (synthetic-temp init/add only, no commit)"
        )
    try:
        _validate_controlled_git_argv(argv, effective_cwd)
    except IsolationViolationError:
        _record("subprocess", f"git {subcommand}".strip() or "git", "blocked")
        raise
    key = register_subprocess(argv)
    env = _git_guard_env(_scrubbed_git_env())
    _record("subprocess", " ".join(key[:4]), "allowed")
    return _persistent_run()(
        [str(part) for part in key],
        cwd=str(effective_cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=check,
    )


# ─── guard installation ───────────────────────────────────────────────────────

_orig_open = builtins.open
_orig_io_open = io.open
_orig_os_open = os.open
_orig_unlink = os.unlink
_orig_remove = os.remove
_orig_rename = os.rename
_orig_replace = os.replace
_orig_rmdir = os.rmdir
_orig_mkdir = os.mkdir
_orig_symlink = os.symlink
_orig_link = getattr(os, "link", None)
_orig_truncate = os.truncate
_orig_scandir = os.scandir
_orig_listdir = os.listdir
_orig_sqlite_connect = sqlite3.connect
_orig_socket_connect = socket.socket.connect
_orig_socketpair = socket.socketpair
_orig_getaddrinfo = socket.getaddrinfo
_orig_create_connection = socket.create_connection
_orig_popen = subprocess.Popen
_orig_subprocess_run = subprocess.run
_orig_subprocess_call = subprocess.call
_orig_check_call = subprocess.check_call
_orig_check_output = subprocess.check_output
_orig_async_exec = asyncio.create_subprocess_exec
_orig_async_shell = asyncio.create_subprocess_shell

# The original executors, stored on the subprocess/asyncio module objects
# themselves.  Those modules are process-wide singletons, so the reference
# survives a re-import of this module: a second import's module-level
# ``_orig_* = subprocess.run`` capture would otherwise pick up the *guard*
# installed by the first import, and every internal spawn would re-enter
# the guards — rewriting the carefully built child environment (and
# recursing on every call).
_RUN_KEY = "_taffy_original_run"
_POPEN_KEY = "_taffy_original_popen"
_CALL_KEY = "_taffy_original_call"
_CHECK_CALL_KEY = "_taffy_original_check_call"
_CHECK_OUTPUT_KEY = "_taffy_original_check_output"
_ASYNC_EXEC_KEY = "_taffy_original_async_exec"


def _persistent(name: str, fallback: Any) -> Any:
    saved = getattr(sys.modules["subprocess"], name, None)
    return saved if saved is not None else fallback


def _persistent_run() -> Any:
    return _persistent(_RUN_KEY, _orig_subprocess_run)


def _persistent_popen() -> Any:
    return _persistent(_POPEN_KEY, _orig_popen)


def _persistent_call() -> Any:
    return _persistent(_CALL_KEY, _orig_subprocess_call)


def _persistent_check_call() -> Any:
    return _persistent(_CHECK_CALL_KEY, _orig_check_call)


def _persistent_check_output() -> Any:
    return _persistent(_CHECK_OUTPUT_KEY, _orig_check_output)


def _persistent_async_exec() -> Any:
    saved = getattr(sys.modules["asyncio"], _ASYNC_EXEC_KEY, None)
    return saved if saved is not None else _orig_async_exec


def _open_guard(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
    if isinstance(file, int):
        return _orig_open(file, mode, *args, **kwargs)
    opener = kwargs.get("opener")
    if opener is not None:
        # Custom openers bypass descriptor checks; route through the guard.
        check_write(file, kind="file-write-opener")
        return _orig_open(file, mode, *args, **kwargs)
    text_mode = str(mode)
    if any(flag in text_mode for flag in ("w", "a", "x", "+")):
        check_write(file, kind="file-write")
    else:
        # One read policy for every real entry point: null-device
        # exemption, sentinels, protected-dotenv names, the origin
        # private tree (data/**, .env*, *.db*) and protected-root
        # sampling all live in check_read.  A separate sentinel-only
        # branch here would let plain ``open(.../origin/data/x.json)``
        # and ``Path.read_bytes`` bypass the origin rules.
        name = Path(str(file)).name
        kind = "file-read-dotenv" if _protected_name(name) else "file-read"
        check_read(file, kind=kind)
    return _orig_open(file, mode, *args, **kwargs)


def _directory_descriptor_path(fd: int) -> Path:
    """Resolve a live Linux directory descriptor without trusting its number.

    shutil's symlink-resistant cleanup uses scandir(fd) and dir_fd-relative
    operations. Check the descriptor's current target against the same path
    policy; never treat an integer descriptor as an unconditional exemption.
    Unsupported or stale descriptor targets fail closed.
    """
    try:
        if sys.platform != "linux":
            raise ValueError("directory descriptor resolution unavailable")
        descriptor_stat = os.fstat(fd)
        if not stat.S_ISDIR(descriptor_stat.st_mode):
            raise ValueError("descriptor is not a directory")
        target = Path(os.readlink(f"/proc/self/fd/{fd}")).resolve(strict=True)
        target_stat = target.stat()
        if (target_stat.st_dev, target_stat.st_ino) != (
            descriptor_stat.st_dev,
            descriptor_stat.st_ino,
        ):
            raise ValueError("descriptor target changed")
        return target
    except (OSError, ValueError, TypeError) as exc:
        _record("dir-fd", str(fd), "blocked")
        raise IsolationViolationError("isolation: unresolved directory descriptor denied") from exc


def _descriptor_relative_path(path: Any, dir_fd: int | None = None) -> Any:
    if isinstance(path, int):
        return _directory_descriptor_path(path)
    if dir_fd is not None:
        relative = Path(os.fsdecode(path))
        if not relative.is_absolute():
            return _directory_descriptor_path(dir_fd) / relative
    return path


def _os_open_guard(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
    if isinstance(path, (str, bytes, os.PathLike)) and _is_null_device(path):
        return _orig_os_open(path, flags, *args, **kwargs)
    write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
    effective = _descriptor_relative_path(path, kwargs.get("dir_fd"))
    if flags & write_flags:
        check_write(effective, kind="file-write")
    else:
        check_read(effective, kind="file-read")
    return _orig_os_open(path, flags, *args, **kwargs)


def _unlink_guard(path: Any, *args: Any, **kwargs: Any) -> None:
    check_write(_descriptor_relative_path(path, kwargs.get("dir_fd")), kind="file-delete")
    return _orig_unlink(path, *args, **kwargs)


def _rename_guard(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
    check_write(src, kind="file-replace")
    check_write(dst, kind="file-replace")
    return _orig_rename(src, dst, *args, **kwargs)


def _replace_guard(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
    check_write(src, kind="file-replace")
    check_write(dst, kind="file-replace")
    return _orig_replace(src, dst, *args, **kwargs)


def _rmdir_guard(path: Any, *args: Any, **kwargs: Any) -> None:
    check_write(_descriptor_relative_path(path, kwargs.get("dir_fd")), kind="file-delete")
    return _orig_rmdir(path, *args, **kwargs)


def _mkdir_guard(path: Any, *args: Any, **kwargs: Any) -> None:
    check_write(path, kind="file-write")
    return _orig_mkdir(path, *args, **kwargs)


def _symlink_guard(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
    check_write(dst, kind="file-write")
    # An existing protected source aliased into an allowed tree would let
    # later writes through the alias reach the original (R2).  Dangling
    # targets stay allowed; reads through a live alias are still checked.
    try:
        source = _effective_path(src)
    except IsolationViolationError:
        raise
    except Exception:
        source = None
    if source is not None and (
        _is_sentinel(source) or _origin_protected_read(source, Path(str(src)).name)
    ):
        _record("file-write", str(source), "blocked")
        raise IsolationViolationError(f"isolation: symlink from protected source denied: {source}")
    return _orig_symlink(src, dst, *args, **kwargs)


def _truncate_guard(path: Any, *args: Any, **kwargs: Any) -> int:
    check_write(path, kind="file-write")
    return _orig_truncate(path, *args, **kwargs)


def _scandir_guard(path: Any = ".", *args: Any, **kwargs: Any) -> Any:
    check_read(_descriptor_relative_path(path if path is not None else "."), kind="dir-list")
    return _orig_scandir(path, *args, **kwargs)


def _listdir_guard(path: Any = ".", *args: Any, **kwargs: Any) -> list[str]:
    check_read(_descriptor_relative_path(path if path is not None else "."), kind="dir-list")
    return _orig_listdir(path, *args, **kwargs)


def _sqlite_path_of(database: Any) -> str | None:
    if isinstance(database, bytes):
        try:
            database = database.decode("utf-8")
        except UnicodeDecodeError:
            return None
    text = str(database) if not isinstance(database, str) else database
    if text == ":memory:":
        return None
    if text.startswith("file:"):
        # SQLite decodes percent-escapes before opening (``%2e%2e`` is
        # ``..``); the guard must interpret the URI exactly the same way
        # or an encoded traversal would check a different path (R2).
        from urllib.parse import unquote

        text = unquote(text[len("file:") :].split("?", 1)[0])
    return text or None


def _sqlite_connect_guard(
    database: Any = ":memory:", *args: Any, **kwargs: Any
) -> sqlite3.Connection:
    candidate = _sqlite_path_of(database)
    if candidate is not None:
        effective = _effective_path(candidate)
        if not _is_allowed(effective):
            _record("sqlite", str(effective), "blocked")
            raise IsolationViolationError(
                f"isolation: sqlite open denied outside controlled trees: {effective}"
            )
        _record_allowed_sample("sqlite", str(effective))
    return _orig_sqlite_connect(database, *args, **kwargs)


def _in_socketpair() -> bool:
    return bool(getattr(_thread_state, "socketpair_open", False))


def _socketpair_guard(*args: Any, **kwargs: Any) -> tuple[socket.socket, socket.socket]:
    """Same-process IPC has no external endpoint; allow, loudly sampled."""
    _thread_state.socketpair_open = True
    try:
        pair = _orig_socketpair(*args, **kwargs)
    finally:
        _thread_state.socketpair_open = False
    _record_allowed_sample("network", "socketpair")
    return pair


_orig_loop_create_connection = asyncio.BaseEventLoop.create_connection
_orig_loop_create_datagram = asyncio.BaseEventLoop.create_datagram_endpoint


def _loop_tcp_target(args: Any, kwargs: Any) -> tuple[str, int] | None:
    """Extract the (host, port) of a loop.create_connection call, if TCP."""
    if kwargs.get("sock") is not None or "path" in kwargs:
        return None
    positional = list(args)
    factory = positional.pop(0) if positional else kwargs.get("protocol_factory")
    del factory
    if (
        positional
        and isinstance(positional[0], str)
        and "port" not in kwargs
        and len(positional) == 1
    ):
        return None  # unix-socket path form
    host = kwargs.get("host", positional[0] if len(positional) > 0 else None)
    port = kwargs.get("port", positional[1] if len(positional) > 1 else None)
    if host is None:
        host = "127.0.0.1"
    try:
        return (str(host), int(port)) if port is not None else ("", -1)
    except (TypeError, ValueError):
        return ("", -1)


async def _loop_create_connection_guard(self: Any, *args: Any, **kwargs: Any) -> Any:
    # Single choke point above both transports: the selector loop and the
    # Windows Proactor (ConnectEx) loop share BaseEventLoop.create_connection,
    # while neither path calls socket.connect (R1).
    target = _loop_tcp_target(args, kwargs)
    if target is None or target[1] < 0:
        _record("network", "non-tcp-loop-connection", "blocked")
        raise IsolationViolationError("isolation: non-TCP loop connection denied")
    check_connect(target[0], target[1])
    return await _orig_loop_create_connection(self, *args, **kwargs)


async def _loop_create_datagram_guard(self: Any, *args: Any, **kwargs: Any) -> Any:
    remote = kwargs.get("remote_addr")
    if remote is not None:
        try:
            check_connect(str(remote[0]), int(remote[1]))
        except (IndexError, TypeError, ValueError):
            _record("network", repr(remote)[:120], "blocked")
            raise IsolationViolationError("isolation: outbound datagram denied") from None
    return await _orig_loop_create_datagram(self, *args, **kwargs)


def _host_is_loopback(host: str) -> bool:
    """True only for real loopback addresses; never string prefixes.

    ``localhost`` is allowed by name; everything else must parse as an
    IP address and be a loopback per ``ipaddress`` — a prefix check such
    as ``startswith("127.")`` would wrongly admit ``127.example.invalid``
    into the real resolver.
    """
    import ipaddress

    lowered = host.strip().lower()
    if lowered == "localhost":
        return True
    try:
        return ipaddress.ip_address(lowered).is_loopback
    except ValueError:
        return False


def _getaddrinfo_guard(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
    """Refuse resolution of unauthorised non-loopback targets (R7/DNS).

    AnyIO (and httpx underneath it) resolves the hostname *before* the
    TCP guards run, so an unauthorised public target would otherwise
    complete a DNS query before the connect is refused.  Resolution is
    allowed only for loopback spellings and for hosts already registered
    as local endpoints; anything else is refused pre-resolution with an
    audited denial.
    """
    if host is None or host == "":
        return _orig_getaddrinfo(host, port, *args, **kwargs)
    text = str(host)
    if _host_is_loopback(text):
        return _orig_getaddrinfo(host, port, *args, **kwargs)
    with _state_lock:
        registered_hosts = {ep[0] for ep in _allowed_endpoints}
    if text.strip().lower() in {h.lower() for h in registered_hosts}:
        return _orig_getaddrinfo(host, port, *args, **kwargs)
    _record("network", f"dns:{text}", "blocked")
    raise IsolationViolationError(
        f"isolation: address resolution denied for unauthorised host: {text}"
    )


def _socket_connect_guard(self: socket.socket, address: Any) -> Any:
    if _in_socketpair():
        return _orig_socket_connect(self, address)
    try:
        host, port = address[0], int(address[1])
    except (IndexError, TypeError, ValueError):
        _record("network", repr(address)[:120], "blocked")
        raise IsolationViolationError("isolation: outbound connection denied") from None
    check_connect(str(host), port)
    return _orig_socket_connect(self, address)


def _create_connection_guard(address: Any, *args: Any, **kwargs: Any) -> socket.socket:
    try:
        host, port = address[0], int(address[1])
    except (IndexError, TypeError, ValueError):
        _record("network", repr(address)[:120], "blocked")
        raise IsolationViolationError("isolation: outbound connection denied") from None
    check_connect(str(host), port)
    return _orig_create_connection(address, *args, **kwargs)


def _argv_of(args: Any) -> tuple[str, ...]:
    if isinstance(args, (str, bytes)):
        raise IsolationViolationError("isolation: shell-form subprocess denied")
    return tuple(str(part) for part in args)


def _refuse_executor_override(kwargs: dict[str, Any]) -> None:
    """Refuse parameters that would swap the program behind an argv.

    ``executable=`` replaces the image the authorised argv names, so the
    allow-list check would be judging one program while another runs.
    Any override value (even an empty string) is refused before the
    executor is reached (R8-3).
    """
    if kwargs.get("executable") is not None:
        _record("subprocess", "executable-override", "blocked")
        raise IsolationViolationError("isolation: executable override denied")


def _prepare_guarded_execution(argv_key: tuple[str, ...], kwargs: dict[str, Any]) -> None:
    """Uniform pre-execution handling shared by every raw entry guard.

    Runs *after* :func:`check_subprocess` authorises the argv and *before*
    the underlying executor is called, so a bare ``subprocess.run``/
    ``Popen``/``check_output``/async-exec call gets exactly the same
    environment contract as the controlled entries: caller env merged,
    ``GIT_*`` stripped case-insensitively, and (for git commands) the
    configuration/template/home hardening layered on top (R8-2).
    """
    _refuse_executor_override(kwargs)
    caller_env = kwargs.pop("env", None)
    env = _scrubbed_git_env(caller_env if isinstance(caller_env, dict) else None)
    if argv_key and argv_key[0] == "git":
        env = _git_guard_env(env)
    kwargs["env"] = env


def _refuse_extra_positional(args: tuple[Any, ...]) -> None:
    """Refuse positional arguments beyond the argv.

    ``Popen(argv, bufsize, executable)`` can smuggle an executable
    override (and every other option) in through *positional* form, which
    a kwargs-only check would miss.  Every guarded entry takes exactly
    one positional argument — the argv sequence — so anything else is
    refused before any executor is reached.
    """
    if len(args) != 1:
        _record("subprocess", f"extra positional args ({len(args)})", "blocked")
        raise IsolationViolationError(
            "isolation: extra positional arguments refused (argv sequence only)"
        )


def _popen_guard(*args: Any, **kwargs: Any) -> subprocess.Popen[Any]:
    if getattr(_thread_state, "guard_depth", 0):
        # Inner call from a pristine ``run`` already checked by the guards:
        # pass the exact arguments (env included) to the pristine Popen.
        return _persistent_popen()(*args, **kwargs)
    if not args:
        raise IsolationViolationError("isolation: unregistered subprocess denied")
    _refuse_extra_positional(args)
    if kwargs.get("shell"):
        _record("subprocess", "shell=True", "blocked")
        raise IsolationViolationError("isolation: shell-form subprocess denied")
    argv = _argv_of(args[0])
    check_subprocess(argv, cwd=kwargs.get("cwd"))
    _prepare_guarded_execution(argv, kwargs)
    return _persistent_popen()(*args, **kwargs)


def _subprocess_run_guard(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
    if not args:
        raise IsolationViolationError("isolation: unregistered subprocess denied")
    _refuse_extra_positional(args)
    if kwargs.get("shell"):
        _record("subprocess", "shell=True", "blocked")
        raise IsolationViolationError("isolation: shell-form subprocess denied")
    argv = _argv_of(args[0])
    check_subprocess(argv, cwd=kwargs.get("cwd"))
    _prepare_guarded_execution(argv, kwargs)
    _thread_state.guard_depth = getattr(_thread_state, "guard_depth", 0) + 1
    try:
        return _persistent_run()(*args, **kwargs)
    finally:
        _thread_state.guard_depth -= 1


def _simple_guard(resolver: Any) -> Any:
    """Guard for the ``call``/``check_call``/``check_output`` family.

    ``resolver`` is a zero-argument function returning the pristine
    executor (persistent across re-imports of this module), so the guard
    body never calls back into an already-replaced module attribute.
    """

    def guarded(*args: Any, **kwargs: Any) -> Any:
        if not args:
            raise IsolationViolationError("isolation: unregistered subprocess denied")
        _refuse_extra_positional(args)
        if kwargs.get("shell"):
            _record("subprocess", "shell=True", "blocked")
            raise IsolationViolationError("isolation: shell-form subprocess denied")
        argv = _argv_of(args[0])
        check_subprocess(argv, cwd=kwargs.get("cwd"))
        _prepare_guarded_execution(argv, kwargs)
        _thread_state.guard_depth = getattr(_thread_state, "guard_depth", 0) + 1
        try:
            return resolver()(*args, **kwargs)
        finally:
            _thread_state.guard_depth -= 1

    return guarded


async def _async_exec_guard(*args: Any, **kwargs: Any) -> Any:
    if not args:
        raise IsolationViolationError("isolation: unregistered subprocess denied")
    _refuse_extra_positional(args)
    argv = _argv_of(args[0])
    check_subprocess(argv, cwd=kwargs.get("cwd"))
    _prepare_guarded_execution(argv, kwargs)
    return await _persistent_async_exec()(*args, **kwargs)


async def _async_shell_guard(*args: Any, **kwargs: Any) -> Any:
    _record("subprocess", "async-shell", "blocked")
    raise IsolationViolationError("isolation: shell-form subprocess denied")


def install_guards() -> None:
    """Install every Python-layer guard exactly once per process."""
    global _installed
    with _state_lock:
        if _installed:
            return
        _installed = True
    # Persist the pristine executors on the singleton modules BEFORE any
    # replacement (idempotent: never overwrite an existing reference).
    if getattr(subprocess, _RUN_KEY, None) is None:
        setattr(subprocess, _RUN_KEY, _orig_subprocess_run)
        setattr(subprocess, _POPEN_KEY, _orig_popen)
        setattr(subprocess, _CALL_KEY, _orig_subprocess_call)
        setattr(subprocess, _CHECK_CALL_KEY, _orig_check_call)
        setattr(subprocess, _CHECK_OUTPUT_KEY, _orig_check_output)
    if getattr(asyncio, _ASYNC_EXEC_KEY, None) is None:
        setattr(asyncio, _ASYNC_EXEC_KEY, _orig_async_exec)
    builtins.open = _open_guard  # type: ignore[assignment]
    io.open = _open_guard  # type: ignore[assignment]
    os.open = _os_open_guard  # type: ignore[assignment]
    os.unlink = _unlink_guard  # type: ignore[assignment]
    os.remove = _remove_guard  # type: ignore[assignment]
    os.rename = _rename_guard  # type: ignore[assignment]
    os.replace = _replace_guard  # type: ignore[assignment]
    os.rmdir = _rmdir_guard  # type: ignore[assignment]
    os.mkdir = _mkdir_guard  # type: ignore[assignment]
    os.symlink = _symlink_guard  # type: ignore[assignment]
    if _orig_link is not None:
        os.link = _link_guard  # type: ignore[assignment]
    os.truncate = _truncate_guard  # type: ignore[assignment]
    os.scandir = _scandir_guard  # type: ignore[assignment]
    os.listdir = _listdir_guard  # type: ignore[assignment]
    sqlite3.connect = _sqlite_connect_guard  # type: ignore[assignment]
    socket.socket.connect = _socket_connect_guard  # type: ignore[assignment]
    socket.socketpair = _socketpair_guard  # type: ignore[assignment]
    socket.create_connection = _create_connection_guard  # type: ignore[assignment]
    socket.getaddrinfo = _getaddrinfo_guard  # type: ignore[assignment]
    subprocess.Popen = _popen_guard  # type: ignore[assignment]
    subprocess.run = _subprocess_run_guard  # type: ignore[assignment]
    subprocess.call = _simple_guard(_persistent_call)  # type: ignore[assignment]
    subprocess.check_call = _simple_guard(_persistent_check_call)  # type: ignore[assignment]
    subprocess.check_output = _simple_guard(_persistent_check_output)  # type: ignore[assignment]
    asyncio.create_subprocess_exec = _async_exec_guard  # type: ignore[assignment]
    asyncio.create_subprocess_shell = _async_shell_guard  # type: ignore[assignment]
    asyncio.BaseEventLoop.create_connection = _loop_create_connection_guard  # type: ignore[assignment]
    asyncio.BaseEventLoop.create_datagram_endpoint = _loop_create_datagram_guard  # type: ignore[assignment]


def _remove_guard(path: Any, *args: Any, **kwargs: Any) -> None:
    check_write(_descriptor_relative_path(path, kwargs.get("dir_fd")), kind="file-delete")
    return _orig_remove(path, *args, **kwargs)


def _link_guard(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
    # A hard link has no "target" indirection the read guard could chase:
    # linking a protected source into an allowed tree would let writes
    # through the alias modify the original.  Both ends are checked (R2).
    try:
        source = _effective_path(src)
    except IsolationViolationError:
        raise
    except Exception:
        source = None
    if source is None or not _is_allowed(source) or _is_sentinel(source):
        _record("file-write", str(src)[:200], "blocked")
        raise IsolationViolationError(
            f"isolation: hard link from untrusted source denied: {str(src)[:120]}"
        )
    if _origin_protected_read(source, Path(str(src)).name):
        _record("file-write", str(source), "blocked")
        raise IsolationViolationError(
            f"isolation: hard link from protected source denied: {source}"
        )
    check_write(dst, kind="file-write")
    assert _orig_link is not None
    return _orig_link(src, dst, *args, **kwargs)


# ─── environment scrubbing ──────────────────────────────────────────────

_SECRET_SUBSTRINGS = (
    "API_KEY",
    "APIKEY",
    "TOKEN",
    "SECRET",
    "PASSW",
    "PASSWD",
    "SMTP",
    "ONEBOT",
    "DEEPSEEK",
    "ANTHROPIC",
    "STEAMDT",
    "PANDASCORE",
    "CSQAQ",
    "CREDENTIAL",
)
_EXACT_DROP_KEYS = {"MAIL_TO", "ENVIRONMENT"}
_PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
_PATH_KEYS = ("PYTHONPATH", "PYTHONHOME")


def scrubbed_child_env(
    *,
    root: Path | str,
    token: str,
    work: Path | str,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build a child environment without inherited credentials or paths."""
    root_text = str(Path(root).resolve())
    work_text = str(Path(work).resolve())
    tmp_text = str(Path(work_text) / "tmp")
    environment: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if key in (
            ISOLATION_ROOT_ENV,
            ISOLATION_TOKEN_ENV,
            ISOLATION_WORK_ENV,
            ISOLATION_ORIGIN_ENV,
        ):
            continue
        if key in _PATH_KEYS or key in _EXACT_DROP_KEYS:
            continue
        if key in _PROXY_KEYS:
            continue
        if any(marker in upper for marker in _SECRET_SUBSTRINGS):
            continue
        if upper.startswith("TAFFY_"):
            continue
        environment[key] = value
    environment.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONPATH": root_text,
            ISOLATION_ROOT_ENV: root_text,
            ISOLATION_TOKEN_ENV: token,
            ISOLATION_WORK_ENV: work_text,
            ISOLATION_ORIGIN_ENV: os.environ.get(ISOLATION_ORIGIN_ENV, ""),
            "TMPDIR": tmp_text,
            "TEMP": tmp_text,
            "TMP": tmp_text,
            "HTTP_PROXY": f"http://{SINKHOLE_HOST}:{SINKHOLE_PORT}",
            "HTTPS_PROXY": f"http://{SINKHOLE_HOST}:{SINKHOLE_PORT}",
            "ALL_PROXY": f"http://{SINKHOLE_HOST}:{SINKHOLE_PORT}",
        }
    )
    if extra:
        environment.update(extra)
    # Credential-shaped keys never reach a child, even when explicitly
    # requested: tests are offline and prove the same via secret_env_hits.
    for key in list(environment):
        if key in (
            ISOLATION_ROOT_ENV,
            ISOLATION_TOKEN_ENV,
            ISOLATION_WORK_ENV,
            ISOLATION_ORIGIN_ENV,
        ):
            continue
        if any(marker in key.upper() for marker in _SECRET_SUBSTRINGS):
            environment.pop(key, None)
    return environment


def run_isolated_child(
    task: str,
    payload: dict[str, Any],
    *,
    root: Path | str,
    token: str,
    work: Path | str,
    extra_env: dict[str, str] | None = None,
    timeout: float = 120.0,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run one fixed-entry child and merge its audit into the parent log.

    The child command is always ``python -m tests.support.childmain``;
    arbitrary ``-c`` snippets are refused (single fixed entry, R3).  The
    child's blocked events are re-recorded as ``child:<kind>`` unclaimed
    events, so a denial swallowed inside the child still fails the owning
    test unless it is precisely expected.  Returns (result, child_audit).
    """
    from tests.support import childmain

    if task not in childmain.TASKS:
        raise IsolationViolationError(f"isolation: unknown child task: {task}")
    root_path = Path(root).resolve()
    work_path = Path(work).resolve()
    payload_dir = work_path / "tmp" / "child-payloads"
    payload_dir.mkdir(parents=True, exist_ok=True)
    payload_file = payload_dir / f"{task}-{os.getpid()}-{time.time_ns()}.json"
    payload_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    argv = [sys.executable, "-m", "tests.support.childmain", task, str(payload_file)]
    key = register_subprocess(argv)
    environment = scrubbed_child_env(root=root_path, token=token, work=work_path, extra=extra_env)
    tmp_dir = Path(environment["TMPDIR"])
    tmp_dir.mkdir(parents=True, exist_ok=True)
    _thread_state.guard_depth = getattr(_thread_state, "guard_depth", 0) + 1
    try:
        completed = _persistent_run()(
            [str(part) for part in key],
            cwd=str(root_path),
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            check=False,
        )
    except OSError as exc:
        _record("subprocess", "childmain", "blocked")
        raise IsolationViolationError(
            f"isolation: controlled child failed to start: {exc}"
        ) from exc
    finally:
        _thread_state.guard_depth -= 1
    if completed.returncode != 0:
        _record("child:exit", f"exit={completed.returncode}", "blocked")
        raise IsolationViolationError(
            "isolation: child entry failed "
            f"(exit={completed.returncode}): {completed.stderr[-1000:]}"
        )
    try:
        envelope = json.loads(completed.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        _record("child:envelope", completed.stdout[-200:], "blocked")
        raise IsolationViolationError(
            f"isolation: child envelope unreadable: {completed.stdout[-500:]}"
        ) from exc
    if "error" in envelope:
        _record("child:error", str(envelope.get("error", ""))[:200], "blocked")
        raise IsolationViolationError(f"isolation: child error: {envelope['error']}")
    child_audit = envelope.get("audit", [])
    for event in child_audit:
        if event.get("outcome") == "blocked":
            _record(
                f"child:{event.get('kind', '?')}",
                str(event.get("detail", ""))[:200],
                "blocked",
            )
    _record_allowed_sample(
        "subprocess",
        f"childmain:{task} blocked={sum(1 for e in child_audit if e.get('outcome') == 'blocked')}",
    )
    try:
        payload_file.unlink()
    except OSError:
        pass
    return envelope.get("result", {}), child_audit


# ─── parent / child verification ────────────────────────────────────────

_SAFE_RUN_HINT = (
    "taffy tests refuse direct pytest without isolation. "
    f"Run the controlled entry instead: {SAFE_ENTRY_COMMAND}"
)


def _read_token(root: Path) -> str | None:
    try:
        return (root / TOKEN_FILENAME).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _dotenv_files_present(root: Path) -> list[str]:
    present: list[str] = []
    try:
        entries = _orig_listdir(root)
    except OSError:
        return present
    for name in entries:
        lowered = name.lower()
        if lowered in _PROTECTED_BASENAMES or lowered.startswith(_PROTECTED_PREFIXES):
            if name != ".env.example":
                present.append(name)
    return present


def establish_parent_isolation(caller_file: str) -> dict[str, Any]:
    """Verify multi-factor isolation, then install guards and boundaries."""
    root_text = os.environ.get(ISOLATION_ROOT_ENV, "").strip()
    token = os.environ.get(ISOLATION_TOKEN_ENV, "").strip()
    work_text = os.environ.get(ISOLATION_WORK_ENV, "").strip()
    if not root_text or not token or not work_text:
        raise RuntimeError(_SAFE_RUN_HINT)
    root = Path(root_text).resolve()
    work = Path(work_text).resolve()
    caller = Path(caller_file).resolve()
    if not _is_within(caller, root):
        raise RuntimeError(f"{_SAFE_RUN_HINT} (caller outside isolated root: {caller})")
    expected = _read_token(root)
    if expected is None or not secrets.compare_digest(expected, token):
        raise RuntimeError(f"{_SAFE_RUN_HINT} (isolation token mismatch)")
    dotenv_hits = _dotenv_files_present(root)
    if dotenv_hits:
        raise RuntimeError(f"{_SAFE_RUN_HINT} (protected dotenv files inside copy: {dotenv_hits})")
    register_allowed_dir(root)
    register_allowed_dir(work)
    register_local_endpoint(SINKHOLE_HOST, SINKHOLE_PORT)
    origin_text = os.environ.get(ISOLATION_ORIGIN_ENV, "").strip()
    if origin_text:
        protect_origin_tree(origin_text)
    install_guards()
    # Sandbox must live under a controlled tree; otherwise tmp artefacts
    # would trip the very guards installed above.
    sandbox_base = Path(
        os.environ.get("TMPDIR", "") or os.environ.get("TEMP", "") or str(work / "tmp")
    )
    info = {
        "root": str(root),
        "work": str(work),
        "caller": str(caller),
        "sandbox_base": str(sandbox_base),
        "python": sys.version.split()[0],
    }
    _record("isolation", f"parent-established root={root}", "allowed")
    return info


def install_child_guards(sentinels: list[Path | str] | tuple[Path | str, ...] = ()) -> Path:
    """Install the parent-equivalent guards inside a controlled child.

    Must run before the child imports any application module.  Returns
    the verified isolated root.
    """
    root_text = os.environ.get(ISOLATION_ROOT_ENV, "").strip()
    token = os.environ.get(ISOLATION_TOKEN_ENV, "").strip()
    work_text = os.environ.get(ISOLATION_WORK_ENV, "").strip()
    if not root_text or not token or not work_text:
        raise IsolationViolationError("isolation: child missing isolation markers")
    root = Path(root_text).resolve()
    work = Path(work_text).resolve()
    expected = _read_token(root)
    if expected is None or not secrets.compare_digest(expected, token):
        raise IsolationViolationError("isolation: child token mismatch")
    register_allowed_dir(root)
    register_allowed_dir(work)
    register_local_endpoint(SINKHOLE_HOST, SINKHOLE_PORT)
    origin_text = os.environ.get(ISOLATION_ORIGIN_ENV, "").strip()
    if origin_text:
        protect_origin_tree(origin_text)
    for entry in sentinels:
        add_sentinel(entry)
    install_guards()
    return root


def verify_child_contract() -> dict[str, Any]:
    """Child-side status: import roots relocated, no dotenv, token matches.

    Pure report — installation belongs to the fixed child entry
    (``tests/support/childmain.py``), never to individual tasks.
    """
    root_text = os.environ.get(ISOLATION_ROOT_ENV, "").strip()
    token = os.environ.get(ISOLATION_TOKEN_ENV, "").strip()
    if not root_text or not token:
        return {"isolated": False, "reason": "missing isolation markers"}
    root = Path(root_text).resolve()
    expected = _read_token(root)
    token_ok = expected is not None and secrets.compare_digest(expected, token)
    project_root = ""
    snapshot_keys = 0
    try:
        import src.services.config as app_config  # noqa: PLC0415

        project_root = str(Path(app_config.PROJECT_ROOT).resolve())
        snapshot_keys = len(getattr(app_config, "_RUNTIME_SETTINGS", {}))
    except Exception as exc:  # pragma: no cover - import failure is evidence too
        return {"isolated": False, "reason": f"config import failed: {type(exc).__name__}"}
    dotenv_hits = _dotenv_files_present(root)
    secret_hits = sorted(
        key
        for key in os.environ
        if key not in _CHILD_SAFE_TAFFY_KEYS
        and (
            any(marker in key.upper() for marker in _SECRET_SUBSTRINGS)
            or (key.upper().startswith("TAFFY_"))
        )
    )
    return {
        "isolated": bool(
            token_ok and not dotenv_hits and project_root and _is_within(Path(project_root), root)
        ),
        "reason": "",
        "import_root": str(Path(sys.path[0]).resolve()) if sys.path else "",
        "project_root": project_root,
        "token_ok": token_ok,
        "dotenv_hits": dotenv_hits,
        "snapshot_keys": snapshot_keys,
        "secret_env_hits": secret_hits,
    }


_CHILD_SAFE_TAFFY_KEYS = frozenset(
    {
        ISOLATION_ROOT_ENV,
        ISOLATION_TOKEN_ENV,
        ISOLATION_WORK_ENV,
        ISOLATION_ORIGIN_ENV,
        "TAFFY_TEST_SANDBOX",
        "TAFFY_STATE_DB",
        "TAFFY_TELEMETRY_DB",
        "TAFFY_MARKET_CATALOG_DB",
        "TAFFY_MARKET_HISTORY_DB",
        "TAFFY_CSQAQ_HISTORY_DB",
        "TAFFY_HOARD_INDEX_DB",
        "TAFFY_CSGO_META_DB",
        "TAFFY_AUTOMATIC_BACKUP_DIR",
        "TAFFY_CONFIG_BACKUP_DIR",
        "TAFFY_SCHEDULE_CONFIG",
        "TAFFY_STEAMDT_CACHE",
        "TAFFY_LOG_DIR",
        "TAFFY_ALLOW_AUTO_DB_MIGRATION",
    }
)


def child_status_payload() -> str:
    return json.dumps(verify_child_contract(), ensure_ascii=False)


def write_audit_report(directory: Path | str) -> Path:
    """Persist the audit trail (blocked/allowed/restrictions) for delivery."""
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    payload = {
        "events": audit_events(),
        "allowed_counters": allowed_counters(),
        "restrictions": restrictions(),
        "allowed_endpoints": [list(item) for item in registered_endpoints()],
        "allowed_subprocesses": [list(item) for item in registered_subprocesses()],
    }
    report = target / "isolation-audit.json"
    report.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


__all__ = [
    "ISOLATION_ORIGIN_ENV",
    "ISOLATION_ROOT_ENV",
    "ISOLATION_TOKEN_ENV",
    "ISOLATION_WORK_ENV",
    "SAFE_ENTRY_COMMAND",
    "TOKEN_FILENAME",
    "IsolationViolationError",
    "add_sentinel",
    "allow_local_port",
    "allowed_counters",
    "audit_events",
    "audit_snapshot_size",
    "check_connect",
    "check_read",
    "check_subprocess",
    "check_write",
    "child_status_payload",
    "claim_events_since",
    "controlled_git",
    "establish_parent_isolation",
    "expect_blocked",
    "history_fixture_scope",
    "history_fixture_stage",
    "install_child_guards",
    "install_guards",
    "note_restriction",
    "protect_origin_tree",
    "register_subprocess_run",
    "register_allowed_dir",
    "register_local_endpoint",
    "register_sentinel",
    "register_subprocess",
    "registered_endpoints",
    "registered_subprocesses",
    "restrictions",
    "run_isolated_child",
    "scrubbed_child_env",
    "session_audit_check",
    "session_audit_failures",
    "unclaimed_blocked_since",
    "verify_child_contract",
    "write_audit_report",
]
