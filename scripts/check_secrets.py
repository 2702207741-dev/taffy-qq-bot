"""Fail CI on likely hard-coded credentials without printing matched values."""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path
from typing import NoReturn

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCAN_ROOTS = (
    PROJECT_ROOT / "bot.py",
    PROJECT_ROOT / "scrape_steamdt.py",
    PROJECT_ROOT / "src",
    PROJECT_ROOT / "scripts",
    PROJECT_ROOT / "tests",
    PROJECT_ROOT / "desktop",
    PROJECT_ROOT / ".github",
    PROJECT_ROOT / ".env.example",
    PROJECT_ROOT / "FEATURES.md",
    PROJECT_ROOT / "README.md",
    PROJECT_ROOT / "SECURITY_REVIEW.md",
    PROJECT_ROOT / "pyproject.toml",
    PROJECT_ROOT / "uv.lock",
)

PATTERNS = {
    "github-token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})"),
    "aws-access-key-id": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "slack-token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}"),
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "anthropic-key": re.compile(r"\bsk-ant-[A-Za-z0-9_-]{12,}"),
    "openai-style-key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    # Either a quoted literal of any shape, or a bare contiguous
    # credential-character token (env/shell style) long enough to be secret
    # material.  Code identifiers such as ``secret = _setting(...)`` cannot
    # match because parentheses, commas and spaces terminate a bare token.
    "literal-credential": re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|private[_-]?token|secret)\b\s*=\s*"
        r"(?:['\"][^'\"\r\n]{16,}['\"]|[A-Za-z0-9._~+/=-]{20,})"
    ),
    "literal-bearer": re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{20,}"),
}

TEXT_SUFFIXES = {
    ".js",
    ".ts",
    ".html",
    ".css",
    ".svg",
    ".ps1",
    ".sh",
    ".cmd",
    ".bat",
    ".cff",
    ".cfg",
    ".conf",
    ".env",
    ".example",
    ".ini",
    ".json",
    ".lock",
    ".md",
    ".py",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}

# Exact historical test-fixture exemptions.  Values are represented only by
# SHA-256 digests of the regex match, and each digest is scoped to one path and
# category.  A new value, category, or file therefore remains a hard failure.
_HISTORICAL_TEST_FIXTURE_ALLOWLIST = frozenset(
    {
        (
            "tests/test_csqaq_client.py",
            "literal-credential",
            "f3bc91b127d1bf2a299800b2bdd034568f7d583ffa58db80c9e414cae0b71e13",
        ),
        (
            "tests/test_health.py",
            "literal-credential",
            "4453c8ea28b731651be65c46ea1e9aef72273228bd2590e6b4b82811465c6c96",
        ),
        (
            "tests/test_secret_scan.py",
            "anthropic-key",
            "fd572a8f1da4de78ecd93a77d7daa3f44f24f2da80bb15a48d6a191d61a36427",
        ),
        (
            "tests/test_secret_scan.py",
            "openai-style-key",
            "fd572a8f1da4de78ecd93a77d7daa3f44f24f2da80bb15a48d6a191d61a36427",
        ),
        (
            "tests/test_secret_scan.py",
            "literal-credential",
            "710c9b3dd6945f8757e729040ecbaf5b750ee052afc6bcc84e909cf408289ffa",
        ),
    }
)


class SecretScanError(RuntimeError):
    """The scanner could not prove that every required source was inspected."""


def _scan_error(stage: str, cause: BaseException | None = None) -> NoReturn:
    error = SecretScanError(f"{stage} failed; refusing to pass an incomplete secret scan")
    if cause is None:
        raise error
    raise error from cause


def _is_text_path(path: str | Path) -> bool:
    """Recognize source-like paths, including layered ``.env.<name>`` files."""

    value = Path(path)
    name = value.name.casefold()
    return name == ".env" or name.startswith(".env.") or value.suffix.casefold() in TEXT_SUFFIXES


def _tracked_files() -> list[Path]:
    """Include ignored-but-still-tracked history snapshots in the scan."""

    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _scan_error("git ls-files", exc)
    if result.returncode != 0:
        _scan_error("git ls-files")

    files: list[Path] = []
    for raw_path in result.stdout.split(b"\0"):
        if not raw_path:
            continue
        try:
            relative = Path(raw_path.decode("utf-8"))
        except UnicodeDecodeError as exc:
            _scan_error("git ls-files path decoding", exc)
        path = PROJECT_ROOT / relative
        if path.is_file() and _is_text_path(relative):
            files.append(path)
    return files


def _history_blobs() -> list[tuple[str, str]]:
    """Return unique reachable text-like Git blobs and their last observed path."""

    try:
        result = subprocess.run(
            ["git", "rev-list", "--objects", "--all"],
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _scan_error("git rev-list", exc)
    if result.returncode != 0:
        _scan_error("git rev-list")

    blobs: dict[str, str] = {}
    for raw_line in result.stdout.splitlines():
        try:
            line = raw_line.decode("utf-8")
        except UnicodeDecodeError as exc:
            _scan_error("git rev-list path decoding", exc)
        object_id, separator, relative_path = line.partition(" ")
        if separator and len(object_id) >= 40 and _is_text_path(relative_path):
            blobs.setdefault(object_id, relative_path)
    return list(blobs.items())


def _history_documents() -> list[tuple[str, str]]:
    """Read bounded historical blobs without checking them out or printing content."""

    blobs = _history_blobs()
    if not blobs:
        return []
    try:
        process = subprocess.Popen(
            ["git", "cat-file", "--batch"],
            cwd=PROJECT_ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        _scan_error("git cat-file", exc)
    if process.stdin is None or process.stdout is None:
        process.kill()
        process.wait()
        _scan_error("git cat-file pipe setup")

    documents: list[tuple[str, str]] = []
    try:
        for object_id, relative_path in blobs:
            process.stdin.write(f"{object_id}\n".encode())
            process.stdin.flush()
            raw_header = process.stdout.readline()
            if not raw_header:
                _scan_error("git cat-file response")
            try:
                header = raw_header.decode("ascii").strip()
            except UnicodeDecodeError as exc:
                _scan_error("git cat-file header decoding", exc)
            parts = header.split()
            if (
                len(parts) != 3
                or parts[0] != object_id
                or parts[1] != "blob"
                or not parts[2].isdecimal()
            ):
                _scan_error("git cat-file response")
            size = int(parts[2])
            payload = process.stdout.read(size)
            trailing = process.stdout.read(1)
            if len(payload) != size or trailing != b"\n":
                _scan_error("git cat-file payload")
            if size > 2_000_000:
                _scan_error("git history text-blob size validation")
            if b"\0" in payload:
                _scan_error("git history text-blob content validation")
            try:
                text = payload.decode("utf-8")
            except UnicodeDecodeError as exc:
                _scan_error("git history text-blob decoding", exc)
            label = f"git-history/{object_id[:12]}/{relative_path}"
            documents.append((label, text))
        process.stdin.close()
        try:
            return_code = process.wait(timeout=10)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.wait(timeout=5)
            _scan_error("git cat-file shutdown", exc)
        if return_code != 0:
            _scan_error("git cat-file")
    except (OSError, subprocess.SubprocessError) as exc:
        _scan_error("git cat-file", exc)
    except BaseException:
        if not process.stdin.closed:
            process.stdin.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        raise
    finally:
        # The normal path already closed and waited for the process.  This
        # branch handles errors raised while decoding or validating a blob.
        if not process.stdin.closed:
            process.stdin.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
    return documents


def _files() -> list[Path]:
    files: list[Path] = _tracked_files()
    for root in SCAN_ROOTS:
        if root.is_file():
            files.append(root)
        elif root.is_dir():
            files.extend(
                path
                for path in root.rglob("*")
                if path.is_file()
                and _is_text_path(path)
                and not set(path.relative_to(PROJECT_ROOT).parts).intersection(
                    {
                        ".venv",
                        "__pycache__",
                        ".git",
                        ".pytest_cache",
                        ".mypy_cache",
                        ".ruff_cache",
                        "node_modules",
                        "dist",
                        "build",
                    }
                )
            )
    return sorted(set(files))


def _scan_lines(
    label: str,
    lines: list[str],
    findings: list[tuple[str, int, str]],
) -> None:
    for line_number, line in enumerate(lines, 1):
        for category, pattern in PATTERNS.items():
            matches = pattern.finditer(line)
            if any(
                not _is_allowlisted_historical_test_match(label, category, match.group(0))
                for match in matches
            ):
                findings.append((label, line_number, category))


def _is_allowlisted_historical_test_match(label: str, category: str, value: str) -> bool:
    normalized = label.replace("\\", "/")
    parts = normalized.split("/", 2)
    if len(parts) != 3 or parts[0] != "git-history":
        return False
    source_path = parts[2]
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return (source_path, category, digest) in _HISTORICAL_TEST_FIXTURE_ALLOWLIST


def scan(*, include_history: bool = True) -> list[tuple[str, int, str]]:
    findings: list[tuple[str, int, str]] = []
    for path in _files():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            findings.append((str(path.relative_to(PROJECT_ROOT)), 0, "unreadable"))
            continue
        _scan_lines(str(path.relative_to(PROJECT_ROOT)), lines, findings)
    if include_history:
        for label, document in _history_documents():
            _scan_lines(label, document.splitlines(), findings)
    return findings


def _safe_output_path(path: str) -> str:
    """Render a location while redacting credential-shaped filename content."""

    redacted = path[:500]
    for category, pattern in PATTERNS.items():
        redacted = pattern.sub(f"[redacted-{category}]", redacted)
    return "".join(
        character if 32 <= ord(character) < 127 else f"\\u{ord(character):04x}"
        for character in redacted
    )


def main() -> int:
    try:
        findings = scan()
    except SecretScanError as exc:
        # The message names only the failed Git stage. Never include stderr,
        # command output, source lines, or exception representations here.
        print(f"Secret scan could not complete: {exc}")
        return 2
    for path, line_number, category in findings[:100]:
        # Never render the source line or regex match: CI logs are not a safe
        # place to copy a credential that was accidentally committed.
        safe_path = _safe_output_path(path)
        print(f"{safe_path}:{line_number}: possible {category}")
    if findings:
        if len(findings) > 100:
            print(f"... {len(findings) - 100} additional finding(s) omitted.")
        print(f"Secret scan failed with {len(findings)} finding(s).")
        return 1
    print("Secret scan passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
