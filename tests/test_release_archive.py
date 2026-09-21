from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from scripts import verify_release_zip as release


@pytest.mark.parametrize(
    "name",
    [
        "../secret",
        "/absolute",
        "C:/secret",
        "folder\\secret",
        ".git/config",
        "nested/.env.prod",
        "data/state.db",
        "logs/events.txt",
        "private.pem",
        "nested/.venv/config",
        "nested/../escape",
    ],
)
def test_rejects_prohibited_archive_names(name: str) -> None:
    assert not release.validate_name(name)


@pytest.mark.parametrize("name", [".env.example", "README.md", "src/app.py"])
def test_accepts_source_names(name: str) -> None:
    assert release.validate_name(name)


def test_archive_requires_exact_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = b"print('example')\n"
    object_id = b"a" * 40

    def git(command: list[str], **kwargs: object) -> bytes:
        if command[1] == "ls-tree":
            return b"100644 blob " + object_id + b"\tapp.py\0"
        return payload

    monkeypatch.setattr(release.subprocess, "check_output", git)
    archive = tmp_path / "source.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("app.py", payload)
    assert release.verify(archive) == 1
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("app.py", payload + b"# changed\n")
    with pytest.raises(ValueError, match="match committed"):
        release.verify(archive)
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("extra.py", payload)
    with pytest.raises(ValueError, match="unexpected"):
        release.verify(archive)
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("app.py", payload)
        bundle.writestr("extra/", b"")
    with pytest.raises(ValueError, match="Unexpected archive directory"):
        release.verify(archive)
    with zipfile.ZipFile(archive, "w"):
        pass
    with pytest.raises(ValueError, match="missing"):
        release.verify(archive)


@pytest.mark.parametrize("name", [".ENV.prod", "logs./file.txt", "folder /file.txt"])
def test_windows_path_aliases_are_rejected(name: str) -> None:
    assert not release.validate_name(name)


@pytest.mark.parametrize(
    "prefix,tail",
    [
        ("ghp_", "x" * 36),
        ("github_pat_", "x" * 50),
        ("AKIA", "A" * 16),
        ("ASIA", "A" * 16),
        ("xoxb-", "1" * 24),
    ],
)
def test_provider_credential_patterns(prefix: str, tail: str) -> None:
    assert any(pattern.search(prefix + tail) for pattern in release.PATTERNS.values())
