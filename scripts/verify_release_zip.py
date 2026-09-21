"""Verify an existing source archive without extracting or printing secret values.

Usage: uv run --frozen python scripts/verify_release_zip.py dist/release.zip
This verifies archive safety/identity, not absence of all application vulnerabilities.
"""

from __future__ import annotations

import argparse
import hashlib
import stat
import subprocess
import zipfile
from pathlib import Path, PurePosixPath

if __package__:
    from .check_secrets import PATTERNS, PROJECT_ROOT
else:
    from check_secrets import PATTERNS, PROJECT_ROOT

FORBIDDEN_PARTS = {
    ".git",
    ".venv",
    "__pycache__",
    "data",
    "logs",
    "napcat",
    "napcat-config",
    ".codex",
    ".gstack",
    ".versions",
    "node_modules",
}
FORBIDDEN_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".log", ".pem", ".key", ".p12", ".pfx"}


def validate_name(name: str) -> bool:
    path = PurePosixPath(name)
    parts = {part.casefold() for part in path.parts}
    return not (
        path.is_absolute()
        or ".." in path.parts
        or "\\" in name
        or ":" in name
        or parts.intersection(FORBIDDEN_PARTS)
        or path.suffix.casefold() in FORBIDDEN_SUFFIXES
        or (path.name.casefold().startswith(".env") and path.name != ".env.example")
        or any(part.endswith((".", " ")) for part in path.parts)
    )


def verify(archive: Path) -> int:
    tracked = subprocess.check_output(["git", "ls-tree", "-rz", "HEAD"], cwd=PROJECT_ROOT).split(
        b"\0"
    )
    expected: dict[str, str] = {}
    for entry in tracked:
        if not entry:
            continue
        metadata, name = entry.split(b"\t", 1)
        mode, kind, object_id = metadata.split()
        if kind != b"blob" or mode not in (b"100644", b"100755"):
            raise ValueError("Release contains a link or unsupported Git object")
        expected[name.decode("utf-8")] = object_id.decode("ascii")
    with zipfile.ZipFile(archive) as bundle:
        seen: set[str] = set()
        members: set[str] = set()
        directories = {
            str(parent) + "/"
            for name in expected
            for parent in PurePosixPath(name).parents
            if str(parent) != "."
        }
        for entry in bundle.infolist():
            if not validate_name(entry.filename):
                raise ValueError("Archive contains a prohibited path")
            folded = entry.filename.casefold()
            if folded in members:
                raise ValueError("Duplicate archive member")
            members.add(folded)
            if stat.S_ISLNK(entry.external_attr >> 16):
                raise ValueError("Unsupported archive member")
            if entry.is_dir():
                if entry.filename not in directories or entry.file_size:
                    raise ValueError("Unexpected archive directory")
                continue
            if entry.filename in seen or entry.filename not in expected:
                raise ValueError("Duplicate or unexpected archive file")
            seen.add(entry.filename)
            if stat.S_ISLNK(entry.external_attr >> 16) or entry.file_size > 20_000_000:
                raise ValueError("Unsupported archive member")
            payload = bundle.read(entry)
            original = subprocess.check_output(
                ["git", "cat-file", "blob", expected[entry.filename]], cwd=PROJECT_ROOT
            )
            # The repository explicitly declares CRLF for Windows scripts.
            # Permit only that transformation, not arbitrary whitespace changes.
            expected_payload = original
            if PurePosixPath(entry.filename).suffix in {".ps1", ".cmd", ".bat"}:
                expected_payload = original.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            if payload not in (original, expected_payload):
                raise ValueError("Archive does not match committed source")
            text = payload.decode("utf-8", errors="replace")
            if any(pattern.search(text) for pattern in PATTERNS.values()):
                raise ValueError("Archive contains a possible credential")
        if seen != set(expected):
            raise ValueError("Archive is missing committed files")
    return len(expected)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    try:
        count = verify(args.archive)
        digest = hashlib.sha256(args.archive.read_bytes()).hexdigest()
    except (OSError, ValueError, zipfile.BadZipFile, subprocess.SubprocessError) as exc:
        print(f"Archive verification failed ({type(exc).__name__}); no contents printed.")
        return 1
    print(f"Archive verified: {count} committed files; SHA256 {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
