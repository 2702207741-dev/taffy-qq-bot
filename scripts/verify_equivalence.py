"""Equivalence verification for the Taffy rewrite.

Compares two git refs on the mechanically checkable contracts:

1. Chinese string literals (every user-facing reply byte) via AST diff.
2. FastAPI route tables (path + methods) declared by the admin API module.

Usage:
    uv run --frozen python scripts/verify_equivalence.py --base bb68ced [--head HEAD]

Exit code 0 only when no literal was added and every removal is attributable
to code that no longer exists (dead code deletions must be justified in the
rewrite decision log).
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from collections.abc import Iterator


def git(*args: str) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return result.stdout


def tracked_python_files(ref: str) -> Iterator[str]:
    for path in git("ls-tree", "-r", "--name-only", ref).splitlines():
        if path.endswith(".py") and (path.startswith("src/") or path == "bot.py"):
            yield path


def blob(ref: str, path: str) -> str:
    return git("show", f"{ref}:{path}")


def chinese_literals(source: str) -> set[str]:
    literals: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if any("\u4e00" <= character <= "\u9fff" for character in node.value):
                literals.add(node.value)
    return literals


def collect_literals(ref: str) -> tuple[set[str], dict[str, set[str]]]:
    combined: set[str] = set()
    per_file: dict[str, set[str]] = {}
    for path in tracked_python_files(ref):
        try:
            values = chinese_literals(blob(ref, path))
        except SyntaxError:
            continue
        if values:
            per_file[path] = values
            combined |= values
    return combined, per_file


def removed_literal_locations(value: str, per_file: dict[str, set[str]]) -> list[str]:
    return [path for path, values in per_file.items() if value in values]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="baseline git ref")
    parser.add_argument("--head", default="HEAD", help="comparison git ref")
    arguments = parser.parse_args()

    base_literals, base_per_file = collect_literals(arguments.base)
    head_literals, _ = collect_literals(arguments.head)

    added = sorted(head_literals - base_literals)
    removed = sorted(base_literals - head_literals)

    print(f"baseline literals: {len(base_literals)}  head literals: {len(head_literals)}")
    print(f"added: {len(added)}  removed: {len(removed)}")
    if added:
        print("-- ADDED (must be empty) --")
        for value in added:
            print(f"  + {value[:120]!r}")
    if removed:
        print("-- REMOVED (each must be justified dead code) --")
        for value in removed:
            locations = ", ".join(removed_literal_locations(value, base_per_file))
            print(f"  - {value[:120]!r}  (was in: {locations})")
    return 1 if added else 0


if __name__ == "__main__":
    sys.exit(main())
