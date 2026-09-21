from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import check_secrets
from tests.support import isolation
from tests.support.isolation import controlled_git


def test_scanner_reports_location_without_echoing_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "sk-ant-" + "this-is-a-fake-but-sensitive-looking-test-value"
    source = tmp_path / "app.py"
    source.write_text(f'API_KEY = "{secret}"\n', encoding="utf-8")
    monkeypatch.setattr(check_secrets, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(check_secrets, "SCAN_ROOTS", (tmp_path,))
    monkeypatch.setattr(check_secrets, "_tracked_files", lambda: [])
    monkeypatch.setattr(check_secrets, "_history_documents", lambda: [])

    assert check_secrets.main() == 1

    output = capsys.readouterr().out
    assert "app.py:1" in output
    assert secret not in output


def test_scanner_accepts_empty_environment_template(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    template = tmp_path / ".env.example"
    template.write_text("API_KEY=\nACCESS_TOKEN=\n", encoding="utf-8")
    monkeypatch.setattr(check_secrets, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(check_secrets, "SCAN_ROOTS", (template,))
    monkeypatch.setattr(check_secrets, "_tracked_files", lambda: [])

    assert check_secrets.scan(include_history=False) == []


def test_scanner_redacts_credential_shaped_filename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "sk-ant-" + "fake-filename-credential-1234567890"
    source = tmp_path / f"{secret}.py"
    source.write_text(f'API_KEY = "{secret}"\n', encoding="utf-8")
    monkeypatch.setattr(check_secrets, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(check_secrets, "SCAN_ROOTS", (source,))
    monkeypatch.setattr(check_secrets, "_tracked_files", lambda: [])
    monkeypatch.setattr(check_secrets, "_history_documents", lambda: [])

    assert check_secrets.main() == 1
    output = capsys.readouterr().out
    assert secret not in output
    assert "redacted-anthropic-key" in output


def _git_scan_fixture(
    tmp_path: Path,
    relative_path: str,
    *,
    blob_secret: str,
) -> None:
    """Stage the scanner's synthetic history entirely outside the replica.

    The subprocess guard is a precise argv allow-list, so each fixture
    registers exactly the git commands it runs.  A commit is never part
    of the list: the R4 batch ban on ``git commit`` is enforced by
    :func:`controlled_git` and pinned by
    ``test_r4_controlled_git_denies_commit_and_foreign_cwd``; the
    history-scanner behaviour is exercised through the object store
    (``hash-object`` + ``cat-file``), which needs no commit either.
    """
    worktree = tmp_path / "history-worktree"
    worktree.mkdir(exist_ok=True)
    source = worktree / relative_path
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(f'API_KEY = "{blob_secret}"\n', encoding="utf-8")
    isolation.register_subprocess_run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=worktree,
        input=source.read_bytes(),
    )
    controlled_git("init", "-q", cwd=worktree)
    controlled_git("add", relative_path, cwd=worktree)


def _git_scan_blob_id(tmp_path: Path) -> str:
    """Return the stored object id of the fixture's secret blob."""
    worktree = tmp_path / "history-worktree"
    candidates = ("app.py", "tests/test_credentials.py", ".env.production")
    chosen = next((worktree / name for name in candidates if (worktree / name).exists()), None)
    assert chosen is not None
    completed = isolation.register_subprocess_run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=worktree,
        input=chosen.read_bytes(),
    )
    assert completed.returncode == 0, completed.stderr[-500:]
    return completed.stdout.strip()


def test_stubbed_documents_scanner_checks_reachable_git_history_after_current_file_is_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blob_secret = "sk-" + "historical-test-credential-1234567890"
    _git_scan_fixture(tmp_path, "app.py", blob_secret=blob_secret)
    # The working file no longer contains the secret.
    (tmp_path / "history-worktree" / "app.py").write_text(
        'API_KEY = ""\n',
        encoding="utf-8",
    )
    blob_id = _git_scan_blob_id(tmp_path)

    monkeypatch.setattr(check_secrets, "PROJECT_ROOT", tmp_path / "history-worktree")
    monkeypatch.setattr(check_secrets, "SCAN_ROOTS", (tmp_path / "history-worktree",))
    monkeypatch.setattr(check_secrets, "_tracked_files", lambda: [])
    monkeypatch.setattr(
        check_secrets,
        "_history_documents",
        lambda: [(f"git-history/{blob_id[:12]}/app.py", f'API_KEY = "{blob_secret}"\n')],
    )
    findings = check_secrets.scan()

    assert any(path.startswith("git-history/") for path, _, _ in findings)


@pytest.mark.parametrize("relative_path", ["tests/test_credentials.py", ".env.production"])
def test_stubbed_documents_scanner_does_not_skip_sensitive_history_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative_path: str,
) -> None:
    blob_secret = "sk-" + "definitely-fake-history-fixture-1234567890"
    _git_scan_fixture(tmp_path, relative_path, blob_secret=blob_secret)
    (tmp_path / "history-worktree" / relative_path).write_text(
        'API_KEY = ""\n',
        encoding="utf-8",
    )
    blob_id = _git_scan_blob_id(tmp_path)

    monkeypatch.setattr(check_secrets, "PROJECT_ROOT", tmp_path / "history-worktree")
    monkeypatch.setattr(check_secrets, "SCAN_ROOTS", (tmp_path / "history-worktree",))
    monkeypatch.setattr(check_secrets, "_tracked_files", lambda: [])
    monkeypatch.setattr(
        check_secrets,
        "_history_documents",
        lambda: [(f"git-history/{blob_id[:12]}/{relative_path}", f'API_KEY = "{blob_secret}"\n')],
    )
    findings = check_secrets.scan()

    assert any(
        path.startswith("git-history/") and path.replace("\\", "/").endswith(relative_path)
        for path, _, _ in findings
    )


def test_historical_test_allowlist_is_exact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "sk-" + "known-fake-fixture-value-1234567890"
    line = f'value = "{secret}"'
    match = check_secrets.PATTERNS["openai-style-key"].search(line)
    assert match is not None
    digest = check_secrets.hashlib.sha256(match.group(0).encode()).hexdigest()
    monkeypatch.setattr(
        check_secrets,
        "_HISTORICAL_TEST_FIXTURE_ALLOWLIST",
        frozenset({("tests/test_fixture.py", "openai-style-key", digest)}),
    )

    findings: list[tuple[str, int, str]] = []
    label = "git-history/0123456789ab/tests/test_fixture.py"
    check_secrets._scan_lines(label, [line], findings)
    assert findings == []

    changed_line = line.replace("7890", "7891")
    check_secrets._scan_lines(label, [changed_line], findings)
    assert findings == [(label, 1, "openai-style-key")]


def test_scanner_fails_closed_when_git_ls_files_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    hidden_diagnostic = "credential-value-must-not-be-echoed"

    def failed_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        del kwargs
        return subprocess.CompletedProcess(args, 1, stdout=b"", stderr=hidden_diagnostic.encode())

    monkeypatch.setattr(check_secrets, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(check_secrets, "SCAN_ROOTS", ())
    monkeypatch.setattr(check_secrets.subprocess, "run", failed_run)

    assert check_secrets.main() == 2
    output = capsys.readouterr().out
    assert "git ls-files" in output
    assert hidden_diagnostic not in output


def test_scanner_fails_closed_when_git_rev_list_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failed_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        del kwargs
        return subprocess.CompletedProcess(args, 1, stdout=b"", stderr=b"sensitive stderr")

    monkeypatch.setattr(check_secrets, "_tracked_files", lambda: [])
    monkeypatch.setattr(check_secrets, "SCAN_ROOTS", ())
    monkeypatch.setattr(check_secrets.subprocess, "run", failed_run)

    with pytest.raises(check_secrets.SecretScanError, match="git rev-list"):
        check_secrets.scan()


def test_scanner_fails_closed_when_git_cat_file_cannot_start(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    hidden_diagnostic = "credential-value-must-not-be-echoed"

    def failed_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        del args, kwargs
        raise OSError(hidden_diagnostic)

    monkeypatch.setattr(check_secrets, "_tracked_files", lambda: [])
    monkeypatch.setattr(check_secrets, "SCAN_ROOTS", ())
    monkeypatch.setattr(
        check_secrets,
        "_history_blobs",
        lambda: [("a" * 40, "src/example.py")],
    )
    monkeypatch.setattr(check_secrets.subprocess, "Popen", failed_popen)

    assert check_secrets.main() == 2
    output = capsys.readouterr().out
    assert "git cat-file" in output
    assert hidden_diagnostic not in output


def _fixture_commit_args() -> tuple[str, ...]:
    """Commit arguments for the scope's fixed form (without the ``git`` argv0)."""
    args = list(isolation._HISTORY_FIXTURE_COMMIT_ARGV)
    assert args[0] == "git"
    return tuple(args[1:])


def test_scanner_checks_reachable_git_history_after_current_file_is_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Synthetic fixture value only: scanner test data, never a credential.
    secret = "sk-" + "historical-" + "test-credential-" + "1234567890"
    key_line = 'API_KEY = "%s"\n' % secret
    with isolation.history_fixture_scope() as repo:
        source = repo / "app.py"
        source.write_text(key_line, encoding="utf-8")
        isolation.history_fixture_stage("app.py")
        controlled_git(*_fixture_commit_args(), cwd=repo)
        source.write_text("empty", encoding="utf-8")

        monkeypatch.setattr(check_secrets, "PROJECT_ROOT", repo)
        monkeypatch.setattr(check_secrets, "SCAN_ROOTS", (repo,))
        findings = check_secrets.scan()

    assert any(path.startswith("git-history/") for path, _, _ in findings)


@pytest.mark.parametrize("relative_path", ["tests/test_credentials.py", ".env.production"])
def test_scanner_does_not_skip_sensitive_history_paths(
    monkeypatch: pytest.MonkeyPatch,
    relative_path: str,
) -> None:
    # Synthetic fixture value only: scanner test data, never a credential.
    secret = "sk-" + "definitely-fake-" + "history-fixture-" + "1234567890"
    key_line = 'API_KEY = "%s"\n' % secret
    with isolation.history_fixture_scope() as repo:
        source = repo / relative_path
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(key_line, encoding="utf-8")
        isolation.history_fixture_stage(relative_path)
        controlled_git(*_fixture_commit_args(), cwd=repo)
        source.write_text("empty", encoding="utf-8")

        monkeypatch.setattr(check_secrets, "PROJECT_ROOT", repo)
        monkeypatch.setattr(check_secrets, "SCAN_ROOTS", (repo,))
        findings = check_secrets.scan()

    assert any(
        path.startswith("git-history/") and path.replace("\\", "/").endswith(relative_path)
        for path, _, _ in findings
    )
