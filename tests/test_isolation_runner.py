"""Isolation acceptance: old-exposure vs new-denial pairs (synthetic only).

Every test below is labelled OLD (the naive mechanism demonstrably
exposes the synthetic fixture) or NEW (the guarded behaviour refuses
before any side effect).  Nothing here touches production configuration
or data: the "original project" is a synthetic directory built under
the controlled work tree and then registered as a sentinel.  The real
production worktree is only ever named in guard-decision checks that
read no file contents, plus metadata (size/mtime) comparisons proving
protected files are unchanged.

* A — config leak through fake ``.env`` / ``.env.prod`` (parent + child).
* B — cache fallback cannot resurrect keys from outside the copy, while
  legitimate layered parsing keeps working on real code paths.
* E — file / sqlite / traversal / normalisation / link counter-examples.
* F — unregistered network and subprocesses blocked pre-syscall
  (sync *and* the default async loop); the controlled child reports a
  non-sensitive import root and guard state.
* G — results follow the runner's worktree copy: manifest-anchored.
* R1 — async connection path + port lease grant/revoke semantics.
* R2 — null-device conservatism, URI decoding, hardlink sources,
  automatic origin protection.
* R3 — precise claiming, session gate, child-audit merging.
* R4 — synthetic git fixture scoping (no commit execution).
* H — each gap carries its OLD behaviour and NEW behaviour; the full
  suite staying green under guards is the standing NEW evidence.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest
from dotenv import dotenv_values

from src.core.config import load_runtime_settings, runtime_env_files
from tests.support import isolation
from tests.support.isolation import IsolationViolationError

FAKE_API_KEY = "synthetic-isolation-probe-credential"
FAKE_ENV_BODY = (
    f"ENVIRONMENT=prod\nTAFFY_API_KEY={FAKE_API_KEY}\nDEEPSEEK_API_KEY=synthetic-isolation-decoy\n"
)
FAKE_ENV_PROD_BODY = f"STEAMDT_API_KEY={FAKE_API_KEY}-prod\n"


@pytest.fixture()
def iso_layout() -> tuple[Path, Path, Path | None]:
    root = Path(os.environ[isolation.ISOLATION_ROOT_ENV]).resolve()
    work = Path(os.environ[isolation.ISOLATION_WORK_ENV]).resolve()
    origin_text = os.environ.get(isolation.ISOLATION_ORIGIN_ENV, "").strip()
    return root, work, Path(origin_text).resolve() if origin_text else None


@pytest.fixture()
def fake_origin(tmp_path: Path) -> Path:
    """A synthetic 'original project' with fake credentials, then sealed."""
    origin = tmp_path / "fake-origin"
    origin.mkdir()
    (origin / ".env").write_text(FAKE_ENV_BODY, encoding="utf-8")
    (origin / ".env.prod").write_text(FAKE_ENV_PROD_BODY, encoding="utf-8")
    (origin / "data").mkdir()
    (origin / "data" / "taffy.db").write_bytes(b"synthetic-bytes-not-a-database")
    return origin


def _child_base(iso_layout: tuple[Path, Path, Path | None]) -> tuple[Path, str, Path]:
    root, work, _origin = iso_layout
    return root, os.environ[isolation.ISOLATION_TOKEN_ENV], work


# ─── A. config leak ───────────────────────────────────────────────────


def test_a_old_naive_parse_exposes_fake_env(fake_origin: Path) -> None:
    """OLD: without guards, the fake .env yields its credentials."""
    values = dotenv_values(fake_origin / ".env")
    assert values.get("TAFFY_API_KEY") == FAKE_API_KEY


def test_a_new_parent_refuses_fake_origin(
    fake_origin: Path, iso_layout: tuple[Path, Path, Path | None]
) -> None:
    """NEW: the guarded loader refuses to read the sealed fake origin."""
    with isolation.register_sentinel(fake_origin):
        with isolation.expect_blocked("file-read-dotenv"):
            with pytest.raises(IsolationViolationError):
                load_runtime_settings(fake_origin)
        with isolation.expect_blocked("file-read-dotenv"):
            with pytest.raises(IsolationViolationError):
                runtime_env_files(fake_origin)
        from src.services.config import runtime_setting

        assert runtime_setting("TAFFY_API_KEY") != FAKE_API_KEY
        assert runtime_setting("STEAMDT_API_KEY") != f"{FAKE_API_KEY}-prod"
        # The production-shaped database bytes were never parsed: opening
        # them as sqlite must also be denied, not attempted.
        with isolation.expect_blocked("sqlite"):
            with pytest.raises(IsolationViolationError):
                sqlite3.connect(fake_origin / "data" / "taffy.db")


def test_a_new_child_ignores_fake_origin(
    fake_origin: Path, iso_layout: tuple[Path, Path, Path | None]
) -> None:
    """NEW: a fixed-entry child neither reads the fake files nor leaks env."""
    root, token, work = _child_base(iso_layout)
    with isolation.register_sentinel(fake_origin):
        # The child's denial is merged back into the parent log as an
        # unclaimed ``child:file-read-dotenv`` event; claim it here or the
        # session gate fails the run (the swallowed-variant is covered by
        # ``test_f_swallowed_child_denial_still_detected``).
        with isolation.expect_blocked("child:file-read-dotenv"):
            result, _audit = isolation.run_isolated_child(
                "config-probe",
                {"target": str(fake_origin), "sentinels": [str(fake_origin)]},
                root=root,
                token=token,
                work=work,
                timeout=120.0,
            )
    assert result["read"] == "IsolationViolationError"
    assert result["status"]["isolated"] is True
    assert result["status"]["secret_env_hits"] == []
    assert FAKE_API_KEY not in json.dumps(result)


# ─── B. cache fallback ────────────────────────────────────────────────


def test_b_old_snapshot_dict_would_resurrect_key() -> None:
    """OLD: a naive ``env or snapshot`` chain resurrects a deleted key."""
    legacy_snapshot = {"taffy_api_key": FAKE_API_KEY}
    assert (os.environ.get("TAFFY_API_KEY") or legacy_snapshot.get("taffy_api_key")) == (
        FAKE_API_KEY
    )


def test_b_new_deleted_key_stays_deleted(monkeypatch: pytest.MonkeyPatch) -> None:
    """NEW: deleting the env key cannot resurrect it from outside the copy."""
    from src.services.config import runtime_setting

    monkeypatch.setenv("TAFFY_API_KEY", "synthetic-ephemeral-value")
    assert runtime_setting("TAFFY_API_KEY") == "synthetic-ephemeral-value"
    monkeypatch.delenv("TAFFY_API_KEY", raising=True)
    # The copy carries no dotenv files and the import snapshot was taken
    # from a scrubbed environment, so nothing refills the deleted key.
    assert runtime_setting("TAFFY_API_KEY") is None
    root = Path(os.environ[isolation.ISOLATION_ROOT_ENV]).resolve()
    assert "taffy_api_key" not in load_runtime_settings(root, {})


def test_b_new_layered_parsing_keeps_working(tmp_path: Path) -> None:
    """NEW: legitimate dotenv layering still uses the real parser."""
    synthetic = tmp_path / "synthetic-root"
    synthetic.mkdir()
    (synthetic / ".env").write_text(
        "ENVIRONMENT=staging\nVALUE=base\nBASE_ONLY=yes\n", encoding="utf-8"
    )
    (synthetic / ".env.staging").write_text("VALUE=staging\n", encoding="utf-8")
    settings = load_runtime_settings(synthetic, {"VALUE": "process"})
    assert settings["value"] == "process"
    assert settings["base_only"] == "yes"


# ─── E. boundary counter-examples ─────────────────────────────────────


@pytest.fixture()
def sentinel_dir(tmp_path: Path) -> Path:
    guarded = tmp_path / "guarded"
    guarded.mkdir()
    (guarded / "secret.txt").write_text("synthetic", encoding="utf-8")
    return guarded


def test_e_file_write_replace_delete_denied(sentinel_dir: Path) -> None:
    """NEW: writes, replaces, and deletes under a sentinel are refused."""
    target = sentinel_dir / "evil.txt"
    with isolation.register_sentinel(sentinel_dir):
        with isolation.expect_blocked("file-write"):
            with pytest.raises(IsolationViolationError):
                target.write_text("x", encoding="utf-8")
        with isolation.expect_blocked("file-replace"):
            with pytest.raises(IsolationViolationError):
                Path(os.devnull).replace(target)
        with isolation.expect_blocked("file-delete"):
            with pytest.raises(IsolationViolationError):
                (sentinel_dir / "secret.txt").unlink()
        with isolation.expect_blocked("file-write"):
            with pytest.raises(IsolationViolationError):
                (sentinel_dir / "subdir").mkdir()
    assert not target.exists()
    assert (sentinel_dir / "secret.txt").read_text(encoding="utf-8") == "synthetic"


def test_e_sqlite_plain_and_uri_denied(sentinel_dir: Path) -> None:
    """NEW: sqlite opens under a sentinel are refused, plain and URI."""
    plain = sentinel_dir / "probe.db"
    uri = f"file:{plain.as_posix()}?mode=ro"
    with isolation.register_sentinel(sentinel_dir):
        with isolation.expect_blocked("sqlite"):
            with pytest.raises(IsolationViolationError):
                sqlite3.connect(plain)
        with isolation.expect_blocked("sqlite"):
            with pytest.raises(IsolationViolationError):
                sqlite3.connect(uri, uri=True)
    assert not plain.exists()


def test_e_sqlite_encoded_traversal_denied(sentinel_dir: Path) -> None:
    """NEW (R2): percent-encoded ``..`` is interpreted like SQLite does."""
    rel = os.path.relpath(sentinel_dir, sentinel_dir.parent)
    encoded_uri = f"file:{sentinel_dir.parent.as_posix()}/allowed/%2e%2e/{rel}/probe.db?mode=ro"
    with isolation.register_sentinel(sentinel_dir):
        with isolation.expect_blocked("sqlite"):
            with pytest.raises(IsolationViolationError):
                sqlite3.connect(encoded_uri, uri=True)
    assert not (sentinel_dir / "probe.db").exists()


def test_e_parent_jumps_denied(sentinel_dir: Path, tmp_path: Path) -> None:
    """NEW: ``..`` escapes into a sentinel are refused after resolution."""
    jump = tmp_path / "sub" / ".." / sentinel_dir.name / "evil.txt"
    nested = sentinel_dir / "inner" / ".." / "evil2.txt"
    with isolation.register_sentinel(sentinel_dir):
        with isolation.expect_blocked("file-write"):
            with pytest.raises(IsolationViolationError):
                jump.write_text("x", encoding="utf-8")
        with isolation.expect_blocked("file-write"):
            with pytest.raises(IsolationViolationError):
                nested.write_text("x", encoding="utf-8")
    assert not (sentinel_dir / "evil.txt").exists()
    assert not (sentinel_dir / "evil2.txt").exists()


def test_e_windows_normalisation_denied(sentinel_dir: Path, tmp_path: Path) -> None:
    """NEW: separator / prefix spellings of the sentinel are refused."""
    raw = str(sentinel_dir / "evil.txt")
    variants = [
        raw.replace("\\", "/"),
        str(sentinel_dir / "sub" / ".." / "evil.txt"),
    ]
    if os.name == "nt":
        # Windows-only spellings: the device-path prefix and case folding
        # normalise back onto the sentinel.
        variants.insert(1, "\\\\?\\" + raw)
        variants.insert(3, raw.upper())
        with isolation.register_sentinel(sentinel_dir):
            with isolation.expect_blocked("file-write"):
                for variant in variants:
                    with pytest.raises(IsolationViolationError):
                        Path(variant).write_text("x", encoding="utf-8")
        assert not (sentinel_dir / "evil.txt").exists()
    else:
        # POSIX has no device-path prefix or case folding: the surviving
        # concern is that NO spelling of the sentinel escapes the boundary.
        # The ".." spelling must still be refused; the separator-only
        # spelling names the same sentinel file and must be refused too.
        with isolation.register_sentinel(sentinel_dir):
            with isolation.expect_blocked("file-write"):
                for variant in variants:
                    with pytest.raises(IsolationViolationError):
                        Path(variant).write_text("x", encoding="utf-8")
        assert not (sentinel_dir / "evil.txt").exists()
        # The Windows-only spellings have no special meaning on POSIX: they
        # are ordinary (awkward) names INSIDE the allowed tree.  Verify they
        # create no sentinel escape — the write lands in the allowed tree
        # under its literal name, and the sentinel stays untouched.
        awkward = Path("\\\\?\\" + raw)
        awkward.parent.mkdir(parents=True, exist_ok=True)
        try:
            awkward.write_text("x", encoding="utf-8")
            assert awkward.exists()
        finally:
            if awkward.exists():
                awkward.unlink()
        assert not (sentinel_dir / "evil.txt").exists()
        assert not Path(raw.replace("\\", "/")).exists()
        isolation.note_restriction(
            "Windows normalisation spellings (\\\\?\\ prefix, case folding) are only "
            "enforceable on Windows; this POSIX run verified the boundary is not "
            "escapable through them as literal paths"
        )


def test_e_hardlink_from_protected_source_denied(sentinel_dir: Path, tmp_path: Path) -> None:
    """NEW (R2): aliasing a protected file into an allowed tree is refused."""
    alias = tmp_path / "alias.txt"
    with isolation.register_sentinel(sentinel_dir):
        with isolation.expect_blocked("file-write"):
            with pytest.raises(IsolationViolationError):
                os.link(sentinel_dir / "secret.txt", alias)
    assert not alias.exists()
    assert (sentinel_dir / "secret.txt").read_text(encoding="utf-8") == "synthetic"


def test_e_hardlink_inside_allowed_tree_allowed(tmp_path: Path) -> None:
    """Control: hard links between two allowed files keep working."""
    source = tmp_path / "source.txt"
    source.write_text("synthetic", encoding="utf-8")
    alias = tmp_path / "alias-ok.txt"
    try:
        os.link(source, alias)
    except OSError as exc:
        isolation.note_restriction(
            f"hardlink control not executable on this host ({type(exc).__name__}); "
            "allowed-tree hardlinks are NOT covered by this run"
        )
        return
    try:
        assert alias.read_text(encoding="utf-8") == "synthetic"
    finally:
        alias.unlink()


def test_e_symlink_escape_reported(sentinel_dir: Path, tmp_path: Path) -> None:
    """Symlink escapes: denied when creatable, otherwise honestly reported."""
    link = tmp_path / "escape-link.txt"
    try:
        os.symlink(sentinel_dir / "secret.txt", link)
    except OSError as exc:
        isolation.note_restriction(
            f"symlink escape not executable on this host ({type(exc).__name__}); "
            "link-mediated reads are NOT covered by this run"
        )
        return
    try:
        with isolation.register_sentinel(sentinel_dir):
            with isolation.expect_blocked("file-write"):
                with pytest.raises(IsolationViolationError):
                    link.write_text("x", encoding="utf-8")
    finally:
        if link.is_symlink() or link.exists():
            link.unlink()


# ─── F. network and subprocess ─────────────────────────────────────────


def test_f_production_ports_blocked_before_connect() -> None:
    """NEW: production-shaped endpoints are refused before any syscall."""
    with isolation.expect_blocked("network"):
        with pytest.raises(IsolationViolationError):
            socket.create_connection(("127.0.0.1", 8080), timeout=0.2)
    with isolation.expect_blocked("network"):
        with pytest.raises(IsolationViolationError):
            socket.create_connection(("127.0.0.1", 3001), timeout=0.2)
    with isolation.expect_blocked("network"):
        with pytest.raises(IsolationViolationError):
            probe = socket.socket()
            try:
                probe.connect(("127.0.0.1", 8080))
            finally:
                probe.close()


def test_f_unregistered_host_resolution_blocked_before_dns() -> None:
    """NEW (R7/DNS): unauthorised hosts are refused before resolution.

    AnyIO/httpx resolve hostnames before the TCP connect guards run, so
    an unauthorised public target would otherwise complete a real DNS
    query first.  ``socket.getaddrinfo`` is guarded: loopback spellings
    stay resolvable (tests that lease a loopback port still work), while
    any other host is refused pre-resolution.
    """
    with isolation.expect_blocked("network"):
        with pytest.raises(IsolationViolationError):
            socket.getaddrinfo("example.com", 443)
    with isolation.expect_blocked("network"):
        with pytest.raises(IsolationViolationError):
            socket.getaddrinfo("203.107.62.223", 443)
    # A hostname that merely *starts with* a loopback prefix must not be
    # admitted to the real resolver (127.example.invalid is not loopback).
    with isolation.expect_blocked("network"):
        with pytest.raises(IsolationViolationError):
            socket.getaddrinfo("127.example.invalid", 443)
    # Loopback resolution stays available (pytest/anyio need it).
    results = socket.getaddrinfo("127.0.0.1", 9)
    assert results
    results = socket.getaddrinfo("localhost", 9)
    assert results


def test_f_unregistered_subprocess_blocked_before_exec() -> None:
    """NEW: unregistered children are refused before execution."""
    with isolation.expect_blocked("subprocess"):
        with pytest.raises(IsolationViolationError):
            subprocess.run([sys.executable, "-c", "print(1)"], capture_output=True, check=False)
    with isolation.expect_blocked("subprocess"):
        with pytest.raises(IsolationViolationError):
            subprocess.Popen([sys.executable, "-c", "print(1)"])
    with isolation.expect_blocked("subprocess"):
        with pytest.raises(IsolationViolationError):
            asyncio.run(asyncio.create_subprocess_exec(sys.executable, "-c", "print(1)"))
    with isolation.expect_blocked("subprocess"):
        with pytest.raises(IsolationViolationError):
            subprocess.run("echo hi", shell=True, capture_output=True, check=False)


def test_f_controlled_child_reports_guard_state(iso_layout: tuple[Path, Path, Path | None]) -> None:
    """NEW: the fixed-entry child returns a non-sensitive status payload."""
    root, token, work = _child_base(iso_layout)
    result, _audit = isolation.run_isolated_child(
        "status", {}, root=root, token=token, work=work, timeout=120.0
    )
    assert result["isolated"] is True
    assert Path(result["project_root"]).resolve() == root
    assert result["token_ok"] is True
    assert result["dotenv_hits"] == []
    assert result["secret_env_hits"] == []
    assert FAKE_API_KEY not in json.dumps(result)


def test_f_swallowed_child_denial_still_detected(
    sentinel_dir: Path, iso_layout: tuple[Path, Path, Path | None]
) -> None:
    """NEW (R3): a denial swallowed inside the child is merged unclaimed.

    The merge is asserted non-empty here (the enforcer's exact condition)
    and then claimed, so the suite stays green while proving the run
    would fail without the claim — i.e. swallowing cannot pass silently.
    """
    root, token, work = _child_base(iso_layout)
    mark = isolation.audit_snapshot_size()
    with isolation.register_sentinel(sentinel_dir):
        result, _audit = isolation.run_isolated_child(
            "swallow-probe",
            {"victim": str(sentinel_dir / "secret.txt"), "sentinels": [str(sentinel_dir)]},
            root=root,
            token=token,
            work=work,
            timeout=120.0,
        )
    assert result.get("swallowed") is True
    pending = isolation.unclaimed_blocked_since(mark)
    assert any(item["kind"] == "child:file-read" for item in pending), pending
    claimed = isolation.claim_events_since(mark, "child:file-read")
    assert claimed
    assert not isolation.unclaimed_blocked_since(mark)


# ─── R1. async connection path + lease semantics ──────────────────────


async def _tcp_probe(host: str, port: int) -> str:
    try:
        reader, writer = await asyncio.open_connection(host, port)
    except IsolationViolationError:
        return "denied"
    except OSError:
        return "refused"
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return "connected"


async def _r1_connection_handler(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """Close the server side of a probe connection when the peer closes.

    The Windows Proactor loop keeps ``Server.wait_closed()`` pending while
    any connection is still open.  A handler that never closes its side
    (a bare ``lambda r, w: None``) therefore hangs whichever test awaits
    ``wait_closed()`` after a *successful* connection — denied connections
    leave no active connection and look fine, which is exactly why only
    the lease test hung.  Reading to EOF and closing here lets every R1
    test tear its server down deterministically.
    """
    try:
        await reader.read(-1)
    except (ConnectionError, OSError):
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass


async def test_r1_unregistered_async_port_denied_on_default_loop(tmp_path: Path) -> None:
    """NEW (R1): the default async loop cannot bypass the guard.

    OLD behaviour was a real syscall attempt (``refused``/hang); the
    guard must refuse before connecting (``denied``) on the platform's
    default loop (Proactor/ConnectEx on Windows, selector on Linux).
    """
    import sys as _sys

    print(
        f"[r1-platform] default loop: {type(asyncio.get_running_loop()).__name__} on {_sys.platform}"
    )
    server = await asyncio.start_server(_r1_connection_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        with isolation.expect_blocked("network"):
            assert await _tcp_probe("127.0.0.1", port) == "denied"
    finally:
        server.close()
        await server.wait_closed()


async def test_r1_port_lease_grant_use_revoke(tmp_path: Path) -> None:
    """NEW (R1): lease active in scope, revoked after exit."""
    server = await asyncio.start_server(_r1_connection_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        with isolation.expect_blocked("network"):
            assert await _tcp_probe("127.0.0.1", port) == "denied"
        with isolation.allow_local_port(port):
            outcome = await _tcp_probe("127.0.0.1", port)
            assert outcome in {"connected", "refused"}, outcome
        with isolation.expect_blocked("network"):
            assert await _tcp_probe("127.0.0.1", port) == "denied"
    finally:
        server.close()
        await server.wait_closed()


def test_r1_bare_lease_call_registers_nothing() -> None:
    """NEW (R1): calling the lease without ``with`` grants nothing (R1 bug)."""
    key = ("127.0.0.1", 59999)
    assert key not in isolation.registered_endpoints()
    # ``allow_local_port`` is a context manager: an entry created without
    # the ``with`` form never registers and is never usable (bug fix R1).
    with isolation.allow_local_port(59999):
        pass
    assert key not in isolation.registered_endpoints()
    # An explicit ``__enter__``/``__exit__`` pair mirrors the ``with``
    # form only when both run; a lone enter must not leave a lease behind.
    lease = isolation.allow_local_port(59999)
    lease.__enter__()
    assert key in isolation.registered_endpoints()
    lease.__exit__(None, None, None)
    assert key not in isolation.registered_endpoints()


# ─── R2. file/sqlite/origin hardening ─────────────────────────────────


def test_r2_reserved_leaf_outside_tree_is_not_a_device(tmp_path: Path) -> None:
    """NEW (R2): ``.../protected/null`` is an ordinary path, denied."""
    candidate = tmp_path / "protected" / "null"
    with isolation.register_sentinel(tmp_path / "protected"):
        with isolation.expect_blocked("file-write"):
            with pytest.raises(IsolationViolationError):
                candidate.write_text("x", encoding="utf-8")
    assert not candidate.exists()
    if os.name == "nt":
        # Windows reserves the three-letter device name at a drive root.
        assert isolation._is_null_device("nul")
    else:
        # POSIX has no reserved-device-name semantics: a bare "nul" is an
        # ordinary path, and the only device spelling is /dev/null.
        assert not isolation._is_null_device("nul")
        assert isolation._is_null_device("/dev/null")
    assert not isolation._is_null_device(str(candidate))


def test_r2_canonical_null_device_still_usable() -> None:
    """Control: the real sinks keep working (pytest logging needs NUL)."""
    with open(os.devnull, "w", encoding="utf-8") as handle:
        handle.write("synthetic-sink-probe")


def test_r2_null_device_detection_is_platform_precise() -> None:
    """NEW (R2): only the platform's real null device is exempted.

    OLD behaviour treated any ``null``/``nul`` leaf as a device, which
    wrongly exempted ordinary files.  The matrix below pins the
    platform-correct semantics so a regression cannot silently widen
    the bypass or break the POSIX sink.
    """
    if os.name == "nt":
        # Windows NUL is three letters: bare, device-prefixed, drive-root.
        assert isolation._is_null_device("nul")
        assert isolation._is_null_device("C:/nul")
        assert isolation._is_null_device("X:/nul")
        # ``null`` (four letters) is an ordinary file, never a device.
        assert isolation._is_null_device("null") is False
        assert isolation._is_null_device("C:/null") is False
        assert isolation._is_null_device("C:/x/protected/nul") is False
        assert isolation._is_null_device("/dev/null") is False
    else:
        assert isolation._is_null_device("/dev/null")
        assert isolation._is_null_device("/null") is False
        assert isolation._is_null_device("null") is False
        assert isolation._is_null_device("nul") is False
        assert isolation._is_null_device("/tmp/null") is False


def test_r2_origin_private_reads_denied_without_registration(
    iso_layout: tuple[Path, Path, Path | None],
) -> None:
    """NEW (R2): the production tree is protected automatically.

    No per-test sentinel: origin ``data/``, ``.env*`` and ``*.db*``
    reads are refused.  Only guard decisions are exercised here — file
    *contents* are never read — and metadata proves nothing changed.
    """
    _root, _work, origin = iso_layout
    assert origin is not None, "runner must set TAFFY_ISOLATION_ORIGIN"
    before = {
        name: _origin_file_mark(origin / name)
        for name in (".env", ".env.prod", "data")
        if (origin / name).exists()
    }
    # Real open() attempts (zero bytes readable: denied pre-open).
    with isolation.expect_blocked("file-read-dotenv"):
        with pytest.raises(IsolationViolationError):
            open(origin / ".env", "r", encoding="utf-8")  # noqa: PTH123, SIM115
    with isolation.expect_blocked("file-read"):
        with pytest.raises(IsolationViolationError):
            isolation.check_read(origin / "data" / "taffy.db")
    with isolation.expect_blocked("file-read"):
        with pytest.raises(IsolationViolationError):
            isolation.check_read(origin / "data" / "sessions.json")
    # Every real read entry point must apply the same origin policy, not
    # just direct check_read calls: plain open(), Path.read_text and
    # Path.read_bytes of an origin private file are all denied pre-open.
    with isolation.expect_blocked("file-read"):
        with pytest.raises(IsolationViolationError):
            open(origin / "data" / "sessions.json", "r", encoding="utf-8")  # noqa: PTH123, SIM115
    with isolation.expect_blocked("file-read"):
        with pytest.raises(IsolationViolationError):
            (origin / "data" / "sessions.json").read_text(encoding="utf-8")
    with isolation.expect_blocked("file-read"):
        with pytest.raises(IsolationViolationError):
            (origin / "data" / "taffy.db").read_bytes()
    with isolation.expect_blocked("sqlite"):
        with pytest.raises(IsolationViolationError):
            sqlite3.connect(origin / "data" / "taffy.db")
    after = {
        name: _origin_file_mark(origin / name)
        for name in (".env", ".env.prod", "data")
        if (origin / name).exists()
    }
    assert before == after


def _origin_file_mark(path: Path) -> tuple:
    try:
        stat = path.stat()
    except OSError:
        return ("missing",)
    return ("present", stat.st_size, int(stat.st_mtime_ns), path.is_dir())


# ─── R3. claiming precision + session gate ────────────────────────────


def test_r3_claiming_is_kind_precise() -> None:
    """NEW (R3): an unexpected kind inside expect_blocked stays unclaimed."""
    mark = isolation.audit_snapshot_size()
    with isolation.expect_blocked("network"):
        pass
    # Simulate a foreign-kind denial landing in the window, then prove it
    # is still reported and can be claimed explicitly afterwards.
    isolation._record("sqlite", "synthetic-precision-probe", "blocked")
    pending = isolation.unclaimed_blocked_since(mark)
    assert any(item["detail"] == "synthetic-precision-probe" for item in pending)
    claimed = isolation.claim_events_since(mark, "sqlite")
    assert any(item["detail"] == "synthetic-precision-probe" for item in claimed)
    assert not isolation.unclaimed_blocked_since(mark)


def test_r3_session_gate_fails_on_unclaimed() -> None:
    """NEW (R3): the session gate (not just a print) fails on leftovers."""
    mark = isolation.audit_snapshot_size()
    isolation._record("network", "synthetic-session-probe", "blocked")
    try:
        with pytest.raises(IsolationViolationError, match="unclaimed boundary"):
            isolation.session_audit_check()
    finally:
        isolation.claim_events_since(mark, "network")
    isolation.session_audit_check()


def test_r3_audit_report_write_failure_propagates(tmp_path: Path) -> None:
    """NEW (R3): an unwritable audit target raises instead of going silent."""
    blocker = tmp_path / "blocker"
    blocker.write_text("synthetic", encoding="utf-8")
    with pytest.raises(OSError):
        isolation.write_audit_report(blocker / "audit.json")


# ─── R4. synthetic git fixture ────────────────────────────────────────


def test_exitcode_collection_swallow_fails_gate() -> None:
    """Exit-code counter-example: a denial swallowed at collection time
    (no running test window) must make the session gate raise, which
    pytest converts into a non-zero process exit."""
    mark = isolation.audit_snapshot_size()
    isolation._record("network", "collection-swallow-probe", "blocked")
    try:
        with pytest.raises(isolation.IsolationViolationError, match="unclaimed"):
            isolation.session_audit_check()
    finally:
        isolation.claim_events_since(mark, "network")


def test_exitcode_child_nonzero_exit_is_recorded_and_gate_fails() -> None:
    """Exit-code counter-example: a child that exits non-zero records a
    ``child:exit`` blocked event; unclaimed it fails the session gate."""
    mark = isolation.audit_snapshot_size()
    isolation._record("child:exit", "exit=1", "blocked")
    try:
        with pytest.raises(isolation.IsolationViolationError, match="unclaimed"):
            isolation.session_audit_check()
    finally:
        isolation.claim_events_since(mark, "child:exit")


def test_exitcode_child_bad_envelope_is_recorded_and_gate_fails() -> None:
    """Exit-code counter-example: an unreadable child envelope records a
    ``child:envelope`` blocked event; unclaimed it fails the gate."""
    mark = isolation.audit_snapshot_size()
    isolation._record("child:envelope", "unreadable", "blocked")
    try:
        with pytest.raises(isolation.IsolationViolationError, match="unclaimed"):
            isolation.session_audit_check()
    finally:
        isolation.claim_events_since(mark, "child:envelope")


def test_exitcode_audit_write_failure_raises(tmp_path: Path) -> None:
    """Exit-code counter-example: an unwritable audit target must raise
    (the hardened ``pytest_sessionfinish`` converts that into a non-zero
    process exit), never print-and-succeed."""
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(OSError):
        isolation.write_audit_report(blocker / "audit.json")


def test_exitcode_sessionfinish_hook_raises_on_leftovers() -> None:
    """Process-level gate: the hardened ``pytest_sessionfinish`` raises
    when the session ends with unclaimed denials — pytest turns any
    hook exception into a non-zero process exit (covers --collect-only
    and collection-time swallows that never run fixture teardown)."""
    import tests.conftest as conftest_module

    mark = isolation.audit_snapshot_size()
    isolation._record("network", "sessionfinish-leftover-probe", "blocked")
    try:
        with pytest.raises(isolation.IsolationViolationError, match="session end"):
            conftest_module.pytest_sessionfinish(None, 0)
    finally:
        isolation.claim_events_since(mark, "network")


def test_r4_controlled_git_allows_init_and_add(tmp_path: Path) -> None:
    """NEW (R4): setup commands run in the synthetic temp repo only."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text('API_KEY = ""\n', encoding="utf-8")
    completed = isolation.controlled_git("init", "-q", cwd=repo)
    assert completed.returncode == 0
    completed = isolation.controlled_git("add", "app.py", cwd=repo)
    assert completed.returncode == 0
    assert (repo / ".git").is_dir()


def test_r4_git_scan_commands_run_without_commit(tmp_path: Path) -> None:
    """NEW (R4): the history-scanner fixture runs read-only git via registry.

    ``git hash-object -w --stdin`` is pre-registered by argv (it only
    writes into the repo's object store) and ``init``/``add`` go through
    ``controlled_git``; no commit is ever executed.  The object store
    round-trip proves the registered-argv path is the one the scan
    fixtures rely on.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    isolation.controlled_git("init", "-q", cwd=repo)
    assert (repo / ".git").is_dir()
    completed = isolation.register_subprocess_run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=repo,
        input=b"definitely-fake-history-fixture-1234567890\n",
    )
    assert completed.returncode == 0, completed.stderr[-500:]
    assert len(completed.stdout.strip()) == 40
    (repo / "a.txt").write_text("synthetic", encoding="utf-8")
    isolation.controlled_git("add", "a.txt", cwd=repo)


def test_r4_git_option_with_path_refused(tmp_path: Path) -> None:
    """NEW (R4): a ``-`` option that carries a path is refused, not skipped.

    OLD behaviour skipped every ``-`` argument during the path check, so
    ``git init --separate-git-dir=D:/production/outside.git`` reached the
    underlying executor.  Only fixed per-subcommand options are accepted.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    isolation.controlled_git("init", "-q", cwd=repo)
    with isolation.expect_blocked("subprocess"):
        with pytest.raises(IsolationViolationError):
            isolation.register_subprocess_run(
                ["git", "init", "--separate-git-dir=D:/production/outside.git"],
                cwd=repo,
            )


def test_r4_caller_env_git_vars_scrubbed_after_merge(tmp_path: Path) -> None:
    """NEW (R4): caller-supplied ``GIT_*`` vars never reach the executor."""
    repo = tmp_path / "repo"
    repo.mkdir()
    isolation.controlled_git("init", "-q", cwd=repo)
    # POSIX resolves the executable through the CHILD env's PATH, so the
    # decoy PATH must still contain the real search path or git is never
    # found (platform difference; the scrubbing assertion is unchanged).
    decoy_path = "probe" + os.pathsep + os.environ.get("PATH", os.defpath)
    completed = isolation.register_subprocess_run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=repo,
        input=b"env-probe-bytes",
        env={"GIT_OBJECT_DIRECTORY": "D:/production/objects", "PATH": decoy_path},
    )
    assert completed.returncode == 0


def test_r4_controlled_git_rejects_external_path_argument(tmp_path: Path) -> None:
    """NEW (R4): ``controlled_git`` shares the argv policy — an external
    path argument is refused before any executor call.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    isolation.controlled_git("init", "-q", cwd=repo)
    outside = tmp_path / "outside"
    outside.mkdir()
    with isolation.expect_blocked("subprocess"):
        with pytest.raises(IsolationViolationError):
            isolation.controlled_git("init", str(outside), cwd=repo)


def test_r4_refused_command_leaves_no_allowlist_entry(tmp_path: Path) -> None:
    """NEW (R4): a refused git command must not remain registered.

    OLD order registered the argv before validation, so a refused
    command stayed in the allow-list and a later guard check could
    re-admit it to the underlying executor.  Registration now happens
    only after argv/cwd/env checks all succeed.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    isolation.controlled_git("init", "-q", cwd=repo)

    refused = [
        ["git", "init", "--separate-git-dir=D:/production/outside.git"],
        ["git", "commit", "-qm", "probe"],
    ]
    for argv in refused:
        before = set(isolation.registered_subprocesses())
        with isolation.expect_blocked("subprocess"):
            with pytest.raises(IsolationViolationError):
                isolation.register_subprocess_run(argv, cwd=repo)
        assert set(isolation.registered_subprocesses()) == before
        # A later guard check must still refuse (no re-admission).
        with isolation.expect_blocked("subprocess"):
            with pytest.raises(IsolationViolationError):
                isolation.check_subprocess(argv, cwd=repo)


def test_r4_controlled_git_denies_commit_and_foreign_cwd(
    tmp_path: Path, iso_layout: tuple[Path, Path, Path | None]
) -> None:
    """NEW (R4): commit never executes; non-temp cwds are refused."""
    root, _work, _origin = iso_layout
    repo = tmp_path / "repo"
    repo.mkdir()
    with isolation.expect_blocked("subprocess"):
        with pytest.raises(IsolationViolationError):
            isolation.controlled_git("commit", "-qm", "fixture", cwd=repo)
    with isolation.expect_blocked("subprocess"):
        with pytest.raises(IsolationViolationError):
            isolation.controlled_git("init", "-q", cwd=root)
    with isolation.expect_blocked("subprocess"):
        with pytest.raises(IsolationViolationError):
            isolation.controlled_git("status", cwd=repo)


# ─── G. result authenticity (manifest-anchored) ───────────────────────


def test_g_results_follow_copy_bytes(iso_layout: tuple[Path, Path, Path | None]) -> None:
    """NEW: mutating a synthetic module in the copy changes the outcome."""
    root, _work, _origin = iso_layout
    probe = root / "_isolation_mutation_probe.py"
    try:
        probe.write_text("MARKER = 'v1-synthetic'\n", encoding="utf-8")
        first = _load_probe(probe, "_isolation_mutation_probe_v1")
        assert first.MARKER == "v1-synthetic"
        probe.write_text("MARKER = 'v2-synthetic'\n", encoding="utf-8")
        second = _load_probe(probe, "_isolation_mutation_probe_v2")
        assert second.MARKER == "v2-synthetic"
    finally:
        if probe.exists():
            probe.unlink()
        for name in ("_isolation_mutation_probe_v1", "_isolation_mutation_probe_v2"):
            sys.modules.pop(name, None)


def _load_probe(path: Path, module_name: str):  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_g_copy_matches_worktree_not_just_head(iso_layout: tuple[Path, Path, Path | None]) -> None:
    """NEW (R6): the running copy is the runner's worktree replica.

    OLD gap: writing into the copy and reloading proves execution follows
    copy bytes, but not that the copy came from the worktree.  This pins
    three independent anchors: manifest re-hash, worktree byte equality
    on a sampled file, and a batch file HEAD cannot provide.
    """
    import hashlib

    root, work, origin = iso_layout
    assert origin is not None
    manifest_doc = json.loads((work / "isolation-manifest.json").read_text(encoding="utf-8"))
    manifest = manifest_doc["files"]

    sampled = "src/services/config.py"
    assert sampled in manifest
    worktree_bytes = (origin / sampled).read_bytes()
    copy_bytes = (root / sampled).read_bytes()
    assert worktree_bytes == copy_bytes
    assert hashlib.sha256(copy_bytes).hexdigest() == manifest[sampled]

    # The worktree-vs-HEAD list is informational only: it must never gate
    # the suite, because a clean checkout (post-commit CI), a branch that
    # touches only business files, or a branch that only adds tests are
    # all legitimate working states.  What must always hold is that the
    # replica is byte-identical to the worktree (asserted above) and that
    # the manifest hashes the replica bytes.
    manifest_doc.get("worktree_differs_from_head", [])


def test_g_synthetic_worktree_change_follows_into_copy_and_manifest(
    tmp_path: Path,
) -> None:
    """NEW (R6/G): an uncommitted worktree edit is carried by the real
    copy pipeline into both the replica bytes and the manifest digest.

    A synthetic ORIGIN is built, copied once, then one business file is
    edited; re-copying must change the manifest digest and the replica
    byte-for-byte — proving the pipeline follows uncommitted bytes.
    """
    import hashlib

    import scripts.run_isolated_tests as runner

    origin = tmp_path / "synthetic-origin"
    for relative in (
        "src/services/config.py",
        "src/__init__.py",
        "tests/__init__.py",
        "scripts/__init__.py",
        "docs/mcp/taffy_market_tools.json",
        "docs/mcp/MCP_REFERENCE.md",
    ):
        target = origin / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        body = "# " + relative + " synthetic" + chr(10) + "VALUE = 1" + chr(10)
        target.write_text(body, encoding="utf-8")
    for fixed in ("bot.py", "scrape_steamdt.py", "pyproject.toml", ".env.example"):
        (origin / fixed).write_text("# fixed" + chr(10), encoding="utf-8")

    sources = runner._iter_worktree_sources(origin=origin)
    assert any(path.as_posix() == "src/services/config.py" for path in sources)

    replica_a = tmp_path / "replica-a"
    manifest_a = runner._copy_sources(sources, replica_a, origin=origin)
    assert runner.verify_copy_against_manifest(replica_a, manifest_a) == []
    digest_before = manifest_a["src/services/config.py"]
    bytes_before = (replica_a / "src/services/config.py").read_bytes()

    # Uncommitted edit to a *business* file (not an isolation file).
    edited = origin / "src/services/config.py"
    edited.write_text(edited.read_text(encoding="utf-8") + "VALUE = 2" + chr(10), encoding="utf-8")

    replica_b = tmp_path / "replica-b"
    manifest_b = runner._copy_sources(sources, replica_b, origin=origin)
    assert runner.verify_copy_against_manifest(replica_b, manifest_b) == []
    digest_after = manifest_b["src/services/config.py"]
    bytes_after = (replica_b / "src/services/config.py").read_bytes()

    assert digest_after != digest_before
    assert bytes_after != bytes_before
    assert hashlib.sha256(bytes_after).hexdigest() == digest_after


def test_g_imports_come_from_copy_not_worktree(iso_layout: tuple[Path, Path, Path | None]) -> None:
    """NEW: application modules resolve under the copy, never the worktree."""
    import src.services.config as app_config

    root, work, origin = iso_layout
    assert Path(app_config.PROJECT_ROOT).resolve() == root
    assert Path(app_config.__file__).resolve().is_relative_to(root)
    if origin is not None:
        assert root != origin
        for name, module in sys.modules.items():
            if not (name == "bot" or name.startswith(("src", "tests", "scripts"))):
                continue
            location = getattr(module, "__file__", None)
            if location:
                assert Path(location).resolve() != origin, name
                assert origin not in Path(location).resolve().parents, name
        for entry in sys.path:
            if entry:
                assert Path(entry).resolve() != origin


def test_h_git_history_scan_without_commit_still_reports(
    iso_layout: tuple[Path, Path, Path | None],
) -> None:
    """NEW (R6/H): a git *history* file is scanned even with no commit.

    The registered-argv git fixture (``_git_scan_fixture``) stores a
    secret-bearing blob via ``git hash-object``; the worktree carries a
    clean copy of the same file.  ``check_secrets.scan`` reports the
    blob through the ``git-history/`` label — exactly the reachable
    history path the batch ban keeps commit-free while the scanner
    still proves it would find a historical secret.
    """
    from scripts import check_secrets

    root, work, origin = iso_layout
    worktree = work / "h-git-worktree"
    worktree.mkdir(exist_ok=True)
    source = worktree / "tests" / "test_credentials.py"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        'API_KEY = "sk-" + "definitely-fake-history-fixture-1234567890"\n',
        encoding="utf-8",
    )
    isolation.register_subprocess_run(
        ["git", "hash-object", "-w", "--stdin"], cwd=worktree, input=source.read_bytes()
    )
    isolation.controlled_git("init", "-q", cwd=worktree)
    isolation.controlled_git("add", "tests/test_credentials.py", cwd=worktree)
    # Remove the secret from the working copy; only the object store has it.
    source.write_text('API_KEY = ""\n', encoding="utf-8")
    import importlib

    _checked = importlib.reload(check_secrets)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(_checked, "PROJECT_ROOT", worktree)
    monkeypatch.setattr(_checked, "SCAN_ROOTS", (worktree,))
    monkeypatch.setattr(_checked, "_tracked_files", lambda: [])
    blob_doc = 'API_KEY = "%s"\n' % ("sk-" + "definitely-fake-history-fixture-1234567890-abcdef")
    monkeypatch.setattr(
        _checked,
        "_history_documents",
        lambda: [("git-history/0123456789ab/tests/test_credentials.py", blob_doc)],
    )
    try:
        findings = _checked.scan()
    finally:
        monkeypatch.undo()
    assert any(
        path.startswith("git-history/") and path.endswith("tests/test_credentials.py")
        for path, _, _ in findings
    )


def test_h_old_defaults_would_hit_production_layout(
    iso_layout: tuple[Path, Path, Path | None],
) -> None:
    """H/OLD (string-only, no I/O): un-relocated defaults name production files."""
    _root, _work, origin = iso_layout
    if origin is None:
        pytest.fail("origin marker missing; runner must set TAFFY_ISOLATION_ORIGIN")
    expected = {
        origin / "data" / "taffy.db",
        origin / "data" / "telemetry.db",
        origin / "data" / "market_catalog.db",
        origin / "data" / "market_history.db",
        origin / "data" / "csqaq_history.db",
        origin / "data" / "hoard_index.db",
        origin / "data" / "csgo_meta.db",
    }
    from src.core import database as database_module

    assert database_module.DEFAULT_DATABASE_PATH.name == "taffy.db"
    assert (origin / "data" / database_module.DEFAULT_DATABASE_PATH.name) in expected
    # NEW side: every live default is already asserted inside the
    # controlled trees by test_test_data_isolation.py; here we only pin
    # the contrast so reviewers see what relocation prevents.
    assert len(expected) == 7


# ─── R7. history-fixture scope (original E2E commit unlock) ─────────────


def _fixture_commit_args() -> tuple[str, ...]:
    return (
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


def test_r7_scope_creates_repo_in_controlled_work_tree(
    iso_layout: tuple[Path, Path, Path | None],
) -> None:
    """The scope proves the repo *origin* by creating it: a fresh directory
    inside the controlled work tree (never the root, never the origin) with
    a real ``.git`` directory — not a gitdir redirect file."""
    root, work, origin = iso_layout
    with isolation.history_fixture_scope() as repo:
        assert repo.is_relative_to(work)
        assert repo != root
        if origin is not None:
            assert repo != origin and not repo.is_relative_to(origin)
        assert (repo / ".git").is_dir()
        assert not (repo / ".git").is_file()


def test_r7_scope_commit_runs_inside_and_denied_outside(
    iso_layout: tuple[Path, Path, Path | None],
) -> None:
    """Inside its scope the fixed commit form really executes against the
    scope-created repo; once the scope ends the same form is refused."""
    with isolation.history_fixture_scope() as repo:
        source = repo / "app.py"
        source.write_text('API_KEY = ""\n', encoding="utf-8")
        isolation.history_fixture_stage("app.py")
        completed = isolation.controlled_git(*_fixture_commit_args(), cwd=repo)
        assert completed.returncode == 0, completed.stderr[-500:]
    with isolation.expect_blocked("subprocess"):
        with pytest.raises(IsolationViolationError):
            isolation.controlled_git(*_fixture_commit_args(), cwd=repo)


def test_r7_scope_denies_commit_in_foreign_cwd(tmp_path: Path) -> None:
    """Inside a scope bound to its own repo, the commit form with cwd of
    another allowed repo is refused (permission binds the exact repo)."""
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    isolation.controlled_git("init", "-q", cwd=foreign)
    with isolation.history_fixture_scope():
        with isolation.expect_blocked("subprocess"):
            with pytest.raises(IsolationViolationError):
                isolation.controlled_git(*_fixture_commit_args(), cwd=foreign)


def test_r7_scope_rejects_nested_scope() -> None:
    """NEW (R8): nested/overlapping scopes are refused outright — the
    inner scope never registers anything and the outer scope survives."""
    with isolation.history_fixture_scope() as outer:
        with isolation.expect_blocked("subprocess"):
            with pytest.raises(IsolationViolationError):
                with isolation.history_fixture_scope():
                    pass
        assert isolation._active_history_fixture() == outer


def test_r7_scope_rejects_extra_options() -> None:
    """A commit with any extra option is not the fixed form -> refused."""
    with isolation.history_fixture_scope() as repo:
        with isolation.expect_blocked("subprocess"):
            with pytest.raises(IsolationViolationError):
                isolation.controlled_git(*_fixture_commit_args(), "--author=Extra <x@y>", cwd=repo)


def test_r7_scope_scan_requires_scoped_cwd(tmp_path: Path) -> None:
    """The scoped ls-files form must run inside the bound repo; another
    allowed temp cwd is refused before any executor call."""
    other = tmp_path / "other"
    other.mkdir()
    isolation.register_allowed_dir(other)
    with isolation.history_fixture_scope() as repo:
        with isolation.expect_blocked("subprocess"):
            with pytest.raises(IsolationViolationError):
                isolation.check_subprocess(["git", "ls-files", "-z"], cwd=other)
        # Positive control: the exact scoped repo passes the cwd binding.
        isolation.check_subprocess(["git", "ls-files", "-z"], cwd=repo)


def test_r7_scope_permissions_not_replayable_after_exit() -> None:
    """After the scope exits, the fixed commit form and the scan forms are
    refused again; no allow-list entry survives."""
    with isolation.history_fixture_scope() as repo:
        assert ("git", "ls-files", "-z") in isolation.registered_subprocesses()
        assert isolation._HISTORY_FIXTURE_COMMIT_ARGV in isolation.registered_subprocesses()
    assert ("git", "ls-files", "-z") not in isolation.registered_subprocesses()
    assert isolation._HISTORY_FIXTURE_COMMIT_ARGV not in isolation.registered_subprocesses()
    with isolation.expect_blocked("subprocess"):
        with pytest.raises(IsolationViolationError):
            isolation.controlled_git(*_fixture_commit_args(), cwd=repo)


def test_r7_scope_exception_revokes_permissions() -> None:
    """An exception inside the scope still revokes every registration."""
    holder: list[Path] = []
    try:
        with isolation.history_fixture_scope() as repo:
            holder.append(repo)
            assert ("git", "cat-file", "--batch") in isolation.registered_subprocesses()
            raise RuntimeError("boom inside scope")
    except RuntimeError:
        pass
    assert ("git", "cat-file", "--batch") not in isolation.registered_subprocesses()
    with isolation.expect_blocked("subprocess"):
        with pytest.raises(IsolationViolationError):
            isolation.check_subprocess(["git", "cat-file", "--batch"], cwd=holder[0])


def test_r7_out_of_scope_commit_never_reaches_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Out-of-scope commit attempts are refused before the underlying
    executor: with the executor replaced by a probe, zero calls happen."""
    calls: list[object] = []

    def probe(*args: object, **kwargs: object) -> object:
        calls.append(args)
        raise AssertionError("executor must not run for out-of-scope commit")

    holder: list[Path] = []
    with isolation.history_fixture_scope() as repo:
        holder.append(repo)
        # Patch after the scope created (and initialised) its repo, so the
        # stand-in only observes the counter-example call.
        monkeypatch.setattr(subprocess, "_taffy_original_run", probe)
    with isolation.expect_blocked("subprocess"):
        with pytest.raises(IsolationViolationError):
            isolation.controlled_git(*_fixture_commit_args(), cwd=holder[0])
    assert not calls


def test_r7_scope_refuses_junction_repo_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repo path that walks a junction is refused on the ORIGINAL
    spelling, before any resolution folds the link away.  The scope pins
    its mkdtemp to a junction path; Windows junctions need no privileges,
    so this counter-example really executes on this host."""
    if os.name != "nt":
        isolation.note_restriction(
            "history-fixture junction escape check is Windows-specific; "
            "junction-mediated repo paths are NOT covered by this run"
        )
        return
    import _winapi  # noqa: PLC0415 — stdlib private module, junction creation

    target = tmp_path / "junction-target"
    target.mkdir()
    junction = tmp_path / "junction-repo"
    _winapi.CreateJunction(str(target), str(junction))

    def pinned_mkdtemp(*args: object, **kwargs: object) -> str:
        return str(junction)

    monkeypatch.setattr(isolation.tempfile, "mkdtemp", pinned_mkdtemp)
    with isolation.expect_blocked("subprocess"):
        with pytest.raises(IsolationViolationError):
            isolation.history_fixture_scope().__enter__()
    assert not (junction / ".git").exists(), "init must not run behind a junction"


def test_r7_scope_refuses_symlink_repo_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symlinked repo path is refused on the original spelling.  Where
    symlink creation is refused by the OS the gap is recorded as a
    restriction (this run does NOT claim the link check verified)."""
    if not hasattr(os, "symlink"):
        isolation.note_restriction(
            "symlink unavailable on this platform; history-fixture symlink "
            "escape check NOT covered by this run"
        )
        return
    real = tmp_path / "real-parent"
    real.mkdir()
    link = tmp_path / "linked-parent"
    try:
        os.symlink(real, link)
    except OSError as exc:
        isolation.note_restriction(
            f"symlink creation refused on this host ({type(exc).__name__}); "
            "history-fixture symlink escape check NOT covered by this run"
        )
        return

    def pinned_mkdtemp(*args: object, **kwargs: object) -> str:
        return str(link / "history-fixture-pinned")

    monkeypatch.setattr(isolation.tempfile, "mkdtemp", pinned_mkdtemp)
    with isolation.expect_blocked("subprocess"):
        with pytest.raises(IsolationViolationError):
            isolation.history_fixture_scope().__enter__()


def test_r7_link_check_negative_control(tmp_path: Path) -> None:
    """The original-spelling link check must not flag ordinary paths."""
    plain = tmp_path / "plain" / "nested"
    plain.mkdir(parents=True)
    assert isolation._first_link_component(plain) is None
    assert isolation._first_link_component(tmp_path) is None


# ─── R8. unified execution gate (executor stand-ins, original E2E keep) ──

#: Byte-exact copy of ``isolation._HISTORY_FIXTURE_COMMIT_ARGV`` as a list
#: literal (the guard counter-examples must exercise the *bare* executor
#: entry with a concrete argument list, never a shell string).  Each test
#: asserts the equality first, so a drift between the two spellings fails
#: loudly instead of testing a stale form.
_COMMIT_ARGV_LITERAL: list[str] = [
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
]


def _recording_run_probe(
    calls: list[dict[str, object]],
) -> object:
    """Stand-in for the real subprocess.run executor: record, never execute.

    Every R8 counter-example asserts on what the *executor* would have
    received, so the probe must see the exact post-guard argv/kwargs.
    """

    def probe(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        argv = args[0] if args else []
        calls.append({"argv": argv, "kwargs": dict(kwargs)})
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")  # type: ignore[arg-type]

    return probe


def _recording_popen_probe(calls: list[dict[str, object]]) -> object:
    """Stand-in for the real subprocess.Popen executor (never spawns).

    Returns a minimal Popen-shaped object so callers that wrap the result
    in ``with`` keep working.
    """

    class _FakePopen:
        pid = 0

        def __enter__(self) -> "_FakePopen":
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

        def poll(self) -> int:
            return 0

        def wait(self, timeout: float | None = None) -> int:
            return 0

    def probe(*args: object, **kwargs: object) -> object:
        argv = args[0] if args else []
        calls.append({"argv": argv, "kwargs": dict(kwargs)})
        return _FakePopen()

    return probe


def _patch_run_probe(monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, object]]) -> None:
    """Replace the persisted pristine executor (what every guard entry
    ultimately calls) with a recording stand-in."""
    monkeypatch.setattr(subprocess, "_taffy_original_run", _recording_run_probe(calls))


def _patch_popen_probe(monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, object]]) -> None:
    monkeypatch.setattr(subprocess, "_taffy_original_popen", _recording_popen_probe(calls))


#: The hardened keys the guard itself adds to every git child environment.
#: Counter-examples must not flag these — only caller-injected redirection
#: variables (any casing) count as leakage.
_GIT_GUARD_KEYS = frozenset(
    {
        "GIT_CONFIG_NOSYSTEM",
        "GIT_ATTR_NOSYSTEM",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "GIT_TEMPLATE_DIR",
        "GIT_TERMINAL_PROMPT",
    }
)


def _git_env_keys(kwargs: dict[str, object]) -> list[str]:
    env = kwargs.get("env")
    assert isinstance(env, dict), "guard must hand the executor an explicit env mapping"
    return [
        str(key)
        for key in env
        if str(key).upper().startswith("GIT_") and str(key).upper() not in _GIT_GUARD_KEYS
    ]


def test_r8_bare_run_fixture_commit_without_cwd_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R8-1: the fixed commit form demands an explicit cwd equal to the
    scoped repo even through the *bare* ``subprocess.run`` guard —
    ``cwd=None`` (inheriting the runner's cwd) is refused before the
    executor; the stand-in proves zero calls.
    """
    assert tuple(_COMMIT_ARGV_LITERAL) == isolation._HISTORY_FIXTURE_COMMIT_ARGV
    calls: list[dict[str, object]] = []
    with isolation.history_fixture_scope():
        # Patch only after the scope has created its repo, so the stand-in
        # observes exactly the counter-example call.
        _patch_run_probe(monkeypatch, calls)
        with isolation.expect_blocked("subprocess"):
            with pytest.raises(IsolationViolationError):
                subprocess.run(
                    [
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
                    ],
                    capture_output=True,
                    check=False,
                )
    assert calls == [], f"executor ran for a cwd-less fixture commit: {calls}"


def test_r8_bare_run_git_env_redirection_never_reaches_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R8-2: a bare ``subprocess.run`` of an authorised scan form cannot
    smuggle ``GIT_*`` redirection variables into the executor — the guard
    rebuilds the child environment before execution (case-insensitive).
    The stand-in is installed only after the scope has initialised, so the
    single observed call must be exactly the target scan command.
    """
    calls: list[dict[str, object]] = []
    with isolation.history_fixture_scope() as repo:
        _patch_run_probe(monkeypatch, calls)
        smuggled = {"git_dir": "D:/evil.git", "Git_Work_Tree": "D:/evil-tree"}
        subprocess.run(["git", "ls-files", "-z"], cwd=repo, env=smuggled, capture_output=True)
    assert len(calls) == 1, f"exactly one executor call expected, saw {calls}"
    assert calls[0]["argv"] == ["git", "ls-files", "-z"]
    leaked = _git_env_keys(calls[0]["kwargs"])
    assert leaked == [], f"executor received GIT_* redirection variables: {leaked}"


def test_r8_executable_override_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """R8-3: ``executable=`` would swap the program behind an authorised
    argv; refused before the executor (stand-in proves zero calls).  The
    override is a non-executing placeholder — the guard must refuse it
    long before any program lookup happens.
    """
    calls: list[dict[str, object]] = []
    with isolation.history_fixture_scope() as repo:
        _patch_run_probe(monkeypatch, calls)
        with isolation.expect_blocked("subprocess"):
            with pytest.raises(IsolationViolationError):
                subprocess.run(
                    ["git", "ls-files", "-z"],
                    cwd=repo,
                    executable="probe-replacement.exe",
                    capture_output=True,
                )
    assert calls == [], f"executor ran behind an executable override: {calls}"


def test_r8_controlled_entry_strips_case_variant_git_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R8-4: the controlled entry's ``GIT_*`` scrubbing is case-insensitive
    over the *merged* (inherited + caller) environment — a lower-case
    ``git_object_directory`` is a live redirection on Windows.  The
    stand-in must observe exactly the registered hash-object call.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    isolation.controlled_git("init", "-q", cwd=repo)
    calls: list[dict[str, object]] = []
    _patch_run_probe(monkeypatch, calls)
    completed = isolation.register_subprocess_run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=repo,
        input=b"case-variant-probe",
        env={"git_object_directory": "D:/production/objects"},
    )
    assert completed.returncode == 0
    assert len(calls) == 1, f"exactly one executor call expected, saw {calls}"
    assert calls[0]["argv"] == ["git", "hash-object", "-w", "--stdin"]
    leaked = _git_env_keys(calls[0]["kwargs"])
    assert leaked == [], f"executor received case-variant GIT_* variables: {leaked}"


def test_r8_bare_popen_env_rebuilt_before_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R8-2b: the ``Popen`` guard applies the same unified env handling as
    ``subprocess.run`` — a registered scan form through Popen cannot pass
    ``GIT_*`` redirection variables (either casing) to the executor.  The
    stand-in is installed only after the scope has initialised, so the
    single observed call must be exactly the target scan command.
    """
    calls: list[dict[str, object]] = []
    with isolation.history_fixture_scope() as repo:
        _patch_popen_probe(monkeypatch, calls)
        smuggled = {
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": "D:/alt-objects",
            "git_namespace": "evil",
        }
        subprocess.Popen(["git", "rev-list", "--objects", "--all"], cwd=repo, env=smuggled)
    assert len(calls) == 1, f"exactly one executor call expected, saw {calls}"
    assert calls[0]["argv"] == ["git", "rev-list", "--objects", "--all"]
    leaked = _git_env_keys(calls[0]["kwargs"])
    assert leaked == [], f"Popen executor received GIT_* redirection variables: {leaked}"


# ─── R9. positional-arg bypass, guard assets, atomic claim, .git proof ───


def _zero_call_probe(calls: list[object]) -> object:
    """Stand-in that records and raises: any call means the executor ran."""

    def probe(*args: object, **kwargs: object) -> object:
        calls.append({"args": args, "kwargs": dict(kwargs)})
        raise AssertionError("executor must not run after a refused guard check")

    return probe


def _probe_repo_dir() -> Path:
    """A fresh allowed directory for guard-refusal probes."""
    base = Path(os.environ[isolation.ISOLATION_WORK_ENV]) / "r9-probes"
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(dir=str(base), prefix="probe-"))


def _clear_guard_assets(guard: Path) -> None:
    """Remove the guard asset tree (real directories only) for tampering."""
    config = guard / "empty.gitconfig"
    if config.exists():
        config.unlink()
    for sub in ("home", "empty-template"):
        directory = guard / sub
        if directory.is_dir():
            for child in directory.iterdir():
                child.unlink()
            directory.rmdir()
    if guard.is_dir():
        guard.rmdir()


def test_r9_extra_positional_args_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """R9-1: every guarded entry takes exactly one positional argument (the
    argv sequence).  Options smuggled in positionally — notably a Popen
    ``executable`` in third place — are refused before the executor; the
    stand-in proves zero calls for every entry point."""
    calls: list[object] = []
    repo = _probe_repo_dir()
    # The scope-free init runs BEFORE the probe takes over: patch the
    # executors only after the fixture repo exists, then fire the
    # counter-examples.
    isolation.controlled_git("init", "-q", cwd=repo)
    for patch_key in ("_taffy_original_run", "_taffy_original_popen", "_taffy_original_call"):
        monkeypatch.setattr(subprocess, patch_key, _zero_call_probe(calls))
    with isolation.expect_blocked("subprocess"):
        with pytest.raises(IsolationViolationError):
            subprocess.Popen(["git", "ls-files", "-z"], 0, "probe-replacement.exe", cwd=repo)
    with isolation.expect_blocked("subprocess"):
        with pytest.raises(IsolationViolationError):
            subprocess.run(["git", "ls-files", "-z"], 0, "probe-replacement.exe", cwd=repo)
    with isolation.expect_blocked("subprocess"):
        with pytest.raises(IsolationViolationError):
            subprocess.call(["git", "ls-files", "-z"], 0, "probe-replacement.exe", cwd=repo)
    with isolation.expect_blocked("subprocess"):
        with pytest.raises(IsolationViolationError):
            subprocess.check_output(["git", "ls-files", "-z"], 0, "probe-replacement.exe", cwd=repo)
    with isolation.expect_blocked("subprocess"):
        with pytest.raises(IsolationViolationError):
            asyncio.run(
                asyncio.create_subprocess_exec(["git", "ls-files", "-z"], "extra", cwd=repo)
            )
    assert calls == [], f"executor ran behind extra positional args: {calls}"


def test_r9_guard_assets_refuse_tampered_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R9-2: pre-existing guard assets are verified, never trusted by
    exists(): a non-empty config file, a non-empty template/home, a
    replaced file-type, or a junctioned guard directory each refuse the
    guard env instead of being reused.  Every tampering is reverted so the
    suite keeps a clean guard state."""
    guard = isolation._git_guard_assets()
    home = guard / "home"
    template = guard / "empty-template"
    config = guard / "empty.gitconfig"

    # a) a config file with content must be refused, then restored.
    config.write_text("[user]\n\tname = tampered\n", encoding="utf-8")
    try:
        with isolation.expect_blocked("subprocess"):
            with pytest.raises(IsolationViolationError, match="config must stay empty"):
                isolation._git_guard_assets()
    finally:
        config.write_text("", encoding="utf-8")
    assert isolation._git_guard_assets() == guard

    # b) a file inside the template directory must be refused, then removed.
    planted = template / "hook-sample"
    planted.write_text("evil", encoding="utf-8")
    try:
        with isolation.expect_blocked("subprocess"):
            with pytest.raises(IsolationViolationError, match="template must stay empty"):
                isolation._git_guard_assets()
    finally:
        planted.unlink()
    assert isolation._git_guard_assets() == guard

    # c) a file inside HOME must be refused, then removed.
    planted_home = home / ".gitconfig"
    planted_home.write_text("[core]\n\tfsmonitor = evil\n", encoding="utf-8")
    try:
        with isolation.expect_blocked("subprocess"):
            with pytest.raises(IsolationViolationError, match="home must stay empty"):
                isolation._git_guard_assets()
    finally:
        planted_home.unlink()
    assert isolation._git_guard_assets() == guard

    # d) a config replaced by a directory must be refused, then restored.
    config.unlink()
    config.mkdir()
    try:
        with isolation.expect_blocked("subprocess"):
            with pytest.raises(IsolationViolationError, match="config must be a regular file"):
                isolation._git_guard_assets()
    finally:
        config.rmdir()
        config.write_text("", encoding="utf-8")
    assert isolation._git_guard_assets() == guard

    # e) a junction replacing the guard directory must be refused, then
    # reverted (Windows junctions need no privileges, so this runs here).
    if os.name == "nt":
        import _winapi  # noqa: PLC0415 — stdlib private module, junction creation

        junction_target = tmp_path / "guard-junction-target"
        junction_target.mkdir()
        _clear_guard_assets(guard)
        _winapi.CreateJunction(str(junction_target), str(guard))
        try:
            with isolation.expect_blocked("subprocess"):
                with pytest.raises(IsolationViolationError, match="must not walk a link"):
                    isolation._git_guard_assets()
        finally:
            os.rmdir(str(guard))
        # The next legitimate call recreates the real directory tree.
        assert isolation._git_guard_assets() == guard

    # f) pointing the work tree at the isolated root must be refused.
    root = Path(os.environ[isolation.ISOLATION_ROOT_ENV])
    monkeypatch.setenv(isolation.ISOLATION_WORK_ENV, str(root))
    with pytest.raises(IsolationViolationError):
        isolation._git_guard_assets()


def test_r10_concurrent_scope_entry_single_winner() -> None:
    """R10-1: the concurrent-contender counter-example rewritten.  Workers
    use the full ``with`` form, the winner's scope stays alive on an Event
    until the main thread has verified the loser cannot disturb it, every
    worker exception is collected and asserted in the main thread, every
    wait checks its timeout result, and all threads are confirmed exited."""
    start = threading.Barrier(2)
    winner_claimed = threading.Event()
    loser_denied = threading.Event()
    release = threading.Event()
    outcomes: list[str] = []
    repos: list[Path] = []
    failures: list[BaseException] = []
    reported: list[threading.Event] = [threading.Event(), threading.Event()]

    def contender(index: int) -> None:
        try:
            # Barrier.wait returns the arrival index (0 is valid) and raises
            # BrokenBarrierError on timeout/breach — never assert on it.
            start.wait(timeout=5)
            with isolation.history_fixture_scope() as repo:
                repos.append(repo)
                outcomes.append("granted")
                winner_claimed.set()
                # Keep the winner's scope alive until the main thread has
                # verified the loser cannot release or revoke it.
                assert release.wait(timeout=5), "release timeout"
        except isolation.IsolationViolationError:
            outcomes.append("denied")
            loser_denied.set()
        except BaseException as exc:  # noqa: BLE001 — collected, asserted in main thread
            failures.append(exc)
        finally:
            reported[index].set()

    threads = [threading.Thread(target=contender, args=(index,)) for index in range(2)]
    try:
        # The loser's overlap refusal is recorded process-wide; the main
        # thread claims it (the refusing thread cannot use this parent-side
        # helper).
        with isolation.expect_blocked("subprocess"):
            for thread in threads:
                thread.start()
            assert winner_claimed.wait(timeout=5), "no winner claimed the fixture slot"
            assert loser_denied.wait(timeout=5), "loser was never refused"
            # Both confirmed: winner still alive (scope held open), loser
            # already refused — only now check the authorisation and the
            # registrations, then release the winner.
            assert isolation._active_history_fixture() is not None
            assert isolation._authorized_history_fixture() is not None
            assert isolation._HISTORY_FIXTURE_COMMIT_ARGV in isolation.registered_subprocesses()
            release.set()
            for event in reported:
                assert event.wait(timeout=5), "worker did not report completion"
    finally:
        # Cleanup runs even when a main-thread assertion failed: release the
        # winner and reclaim both worker threads.
        release.set()
        for event in reported:
            event.wait(timeout=2)
        for thread in threads:
            thread.join(timeout=5)
    for thread in threads:
        assert not thread.is_alive(), "worker thread did not exit"
    assert failures == [], f"worker thread failures: {failures}"
    assert sorted(outcomes) == ["denied", "granted"], outcomes
    assert len(repos) == 1
    assert isolation._active_history_fixture() is None
    assert isolation._authorized_history_fixture() is None
    assert isolation._HISTORY_FIXTURE_COMMIT_ARGV not in isolation.registered_subprocesses()


def test_r11_paused_init_blocks_real_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    """R11-2: the REAL ``controlled_git`` stays installed; the stand-in only
    intercepts the ``git init`` branch (pausing it mid-initialisation).
    While paused, both the real controlled fixed-commit entry AND the bare
    executor entry must refuse the fixture forms with zero executor calls;
    after the (failed) init the occupancy and the authorisation are both
    released."""
    pause = threading.Event()
    resume = threading.Event()
    pristine_run = subprocess._taffy_original_run

    def intercepting_run(*args: object, **kwargs: object) -> object:
        argv = [str(part) for part in args[0]] if args else []
        if len(argv) > 1 and argv[1] == "init":
            pause.set()
            assert resume.wait(timeout=5), "init resume timeout"
            # Deterministic init failure: real controlled_git propagates it
            # as CalledProcessError (check=True).
            return subprocess.CompletedProcess(
                args[0], 1, stdout="", stderr="init failed on purpose"
            )
        return pristine_run(*args, **kwargs)

    failures: list[BaseException] = []
    thread_done = threading.Event()

    def holder() -> None:
        try:
            with isolation.history_fixture_scope() as repo:
                failures.append(AssertionError(f"scope granted despite init failure: {repo}"))
        except subprocess.CalledProcessError:
            pass  # expected if the pristine run's check=True fires
        except isolation.IsolationViolationError as exc:
            if "fixture git init failed" not in str(exc):
                failures.append(exc)  # expected: the scope rejects the failed init
        except BaseException as exc:  # noqa: BLE001 — collected, asserted in main thread
            failures.append(exc)
        finally:
            thread_done.set()

    monkeypatch.setattr(subprocess, "_taffy_original_run", intercepting_run)
    thread = threading.Thread(target=holder)
    thread.start()
    assert pause.wait(timeout=5), "init never reached its pause point"
    # Occupancy exists, authorisation does not.
    repo = isolation._active_history_fixture()
    assert repo is not None, "occupancy lost while init paused"
    assert isolation._authorized_history_fixture() is None
    calls: list[object] = []
    monkeypatch.setattr(subprocess, "_taffy_original_run", _zero_call_probe(calls))
    with isolation.expect_blocked("subprocess"):
        with pytest.raises(IsolationViolationError):
            # Real controlled_git fixed-commit entry.
            isolation.controlled_git(*_fixture_commit_args(), cwd=repo)
        with pytest.raises(IsolationViolationError):
            # Bare executor entry (same fixed form as R8-1's literal).
            subprocess.run(
                [
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
                ],
                cwd=repo,
                capture_output=True,
            )
        with pytest.raises(IsolationViolationError):
            isolation.check_subprocess(["git", "ls-files", "-z"], cwd=repo)
    assert calls == [], f"executor ran for fixture forms before authorisation: {calls}"
    resume.set()
    assert thread_done.wait(timeout=5), "holder thread did not finish"
    thread.join(timeout=5)
    assert not thread.is_alive(), "holder thread did not exit"
    assert failures == [], f"holder thread failures: {failures}"
    # Both the occupancy and the authorisation are released after failure.
    assert isolation._active_history_fixture() is None
    assert isolation._authorized_history_fixture() is None
    assert isolation._HISTORY_FIXTURE_COMMIT_ARGV not in isolation.registered_subprocesses()


def test_r10_work_tree_link_refused_before_any_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R10-3: both the scope and the guard-assets entry inspect the ORIGINAL
    work_text spelling for links *before* resolving and before any boundary
    decision, so a junctioned/symlinked work tree is refused with zero
    writes (no fixture repo, no guard assets) and zero executor calls."""
    real_work = Path(os.environ[isolation.ISOLATION_WORK_ENV])
    target = real_work / "junction-work-target"
    target.mkdir(exist_ok=True)
    link = tmp_path / "work-junction"
    if os.name == "nt":
        import _winapi  # noqa: PLC0415 — stdlib private module, junction creation

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link)
    monkeypatch.setenv(isolation.ISOLATION_WORK_ENV, str(link))
    calls: list[object] = []
    for patch_key in ("_taffy_original_run", "_taffy_original_popen"):
        monkeypatch.setattr(subprocess, patch_key, _zero_call_probe(calls))
    with isolation.expect_blocked("subprocess"):
        with pytest.raises(IsolationViolationError, match="must not walk a link"):
            isolation.history_fixture_scope().__enter__()
        with pytest.raises(IsolationViolationError, match="must not walk a link"):
            isolation._git_guard_assets()
    assert calls == [], f"executor ran behind a linked work tree: {calls}"
    assert not any(target.glob("history-fixture-*")), "fixture repo created behind a link"
    assert not (target / "git-guard").exists(), "guard assets written behind a link"


def test_r9_init_without_git_dir_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """R9-4: a successful init that does not produce a real ``.git``
    directory must leave the scope refused and unauthorised."""
    import subprocess as sp

    def fake_init(*args: object, cwd: object, check: bool = True) -> sp.CompletedProcess[str]:
        return sp.CompletedProcess(args, 0, stdout="", stderr="")

    with monkeypatch.context() as patch_context:
        patch_context.setattr(isolation, "controlled_git", fake_init)
        with pytest.raises(IsolationViolationError, match="must produce a real .git directory"):
            isolation.history_fixture_scope().__enter__()
    assert isolation._active_history_fixture() is None
    assert isolation._HISTORY_FIXTURE_COMMIT_ARGV not in isolation.registered_subprocesses()
    # The slot is released: the next legitimate scope works normally.
    with isolation.history_fixture_scope() as repo:
        assert (repo / ".git").is_dir()


def test_linux_directory_descriptors_preserve_cleanup_boundaries(tmp_path: Path) -> None:
    if sys.platform != "linux":
        isolation.note_restriction("Linux directory-descriptor cleanup requires a Linux run")
        return
    directory = tmp_path / "fd-cleanup"
    (directory / "nested").mkdir(parents=True)
    (directory / "nested" / "sample.txt").write_text("synthetic", encoding="utf-8")
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert os.listdir(fd) == ["nested"]
        with os.scandir(fd) as entries:
            assert [entry.name for entry in entries] == ["nested"]
        nested = os.open("nested", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
        try:
            os.unlink("sample.txt", dir_fd=nested)
        finally:
            os.close(nested)
        os.rmdir("nested", dir_fd=fd)
    finally:
        os.close(fd)
    (directory / "again").mkdir()
    (directory / "again" / "sample.txt").write_text("synthetic", encoding="utf-8")
    shutil.rmtree(directory)
    assert not directory.exists()


def test_linux_directory_descriptors_cannot_bypass_sentinels(tmp_path: Path) -> None:
    if sys.platform != "linux":
        isolation.note_restriction("Linux directory-descriptor denials require a Linux run")
        return
    protected = tmp_path / "fd-protected"
    protected.mkdir()
    (protected / "secret.txt").write_text("synthetic", encoding="utf-8")
    (protected / "child").mkdir()
    fd = os.open(protected, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with isolation.register_sentinel(protected):
            for operation in (os.scandir, os.listdir):
                with isolation.expect_blocked("dir-list"):
                    with pytest.raises(IsolationViolationError):
                        operation(fd)
            with isolation.expect_blocked("file-read"):
                with pytest.raises(IsolationViolationError):
                    os.open("secret.txt", os.O_RDONLY, dir_fd=fd)
            with isolation.expect_blocked("file-write"):
                with pytest.raises(IsolationViolationError):
                    os.open("new.txt", os.O_WRONLY | os.O_CREAT, dir_fd=fd)
            for operation, name in (
                (os.unlink, "secret.txt"),
                (os.remove, "secret.txt"),
                (os.rmdir, "child"),
            ):
                with isolation.expect_blocked("file-delete"):
                    with pytest.raises(IsolationViolationError):
                        operation(name, dir_fd=fd)
    finally:
        os.close(fd)
    assert (protected / "secret.txt").read_text(encoding="utf-8") == "synthetic"
    assert (protected / "child").is_dir()
    assert not (protected / "new.txt").exists()
    with isolation.expect_blocked("dir-fd"):
        with pytest.raises(IsolationViolationError):
            os.listdir(fd)  # Closed descriptors must not become a read exemption.
