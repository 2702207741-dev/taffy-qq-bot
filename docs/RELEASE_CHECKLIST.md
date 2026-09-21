# Public release checklist

Run this checklist before creating a GitHub release.

## Repository safety

- [ ] Confirm `.env`, `.env.*` other than `.env.example`, `data/`, NapCat profiles, logs, backups, screenshots, and local agent artefacts are absent from the staged diff.
- [ ] Run `uv run --frozen python scripts/check_secrets.py` in the final Git repository.
- [ ] After committing the final source, build with `git archive --format=zip --output=dist/release.zip HEAD` (create `dist/` first), then run `uv run --frozen python scripts/verify_release_zip.py dist/release.zip`. This checks committed-file identity, prohibited files, credential patterns and ZIP integrity, but does not certify application security.
- [ ] Inspect `git status --short`, `git diff --cached --check`, and the staged file list.
- [ ] Check third-party notices and license compatibility for any newly vendored file.

## Quality

- [ ] Run `uv lock --check` and `uv sync --frozen --all-groups`.
- [ ] Run lint, format, mypy, and isolated tests from the README.
- [ ] Run the desktop test suite when `desktop/` changes.
- [ ] Review the GitHub Actions run on the exact release commit.
- [ ] Update `CHANGELOG.md`, version metadata, and user-facing documentation.

## GitHub settings after the first push

- [ ] Create a **public** repository named `taffy-qq-bot` under the intended maintainer account; do not initialize it with GitHub's README, license, or `.gitignore`.
- [ ] Push the `main` branch and verify Actions are enabled.
- [ ] Enable Discussions and private vulnerability reporting.
- [ ] Protect `main`: require pull requests and successful CI where the maintainer workflow supports it.
- [ ] Add repository topics such as `nonebot2`, `onebot`, `qq-bot`, `cs2`, `python`, `sqlite`, and `self-hosted`.
- [ ] Publish a GitHub release only after CI is green; attach a concise, truthful release note.

## Never publish

- credentials, access tokens, cookies, QR-login state, or secrets in Git history;
- private message content, real account identifiers, inventory snapshots, backups, or unredacted logs;
- material copied from another project without a compatible license and attribution;
- claims about users, stars, downloads, security audits, partnerships, or production reliability that cannot be verified.

## Status for the released source line

This records what was actually executed, so a later maintainer does not re-run
green checks. The source line from commit `dab559c` onward changes only
documentation, so every result below holds for the archive built from the shipped
commit. It states verified behaviour, not a certification.

Already satisfied here:

- Repository safety: `scripts/check_secrets.py` exits 0; `scripts/verify_release_zip.py`
  exits 0 over the 416 committed files of the archive built from this line; the
  staged tree contains no `.env` (only `.env.example`), no database, log, key or
  image binaries, no `data/`, `dist/`, `node_modules/` or `__pycache__`, and no
  personal absolute paths -- confirmed with a positive control that the same
  searches do match a string that is present.
- Quality: main lint, format, mypy (151 files) and lock checks exit 0; the
  isolated suite passes on Windows 3.12 (2,034 passed, 1 skipped) and on Linux
  3.12.3 / 3.13.13 / 3.14.6 (2,035 each); desktop lint, format, mypy and its 78
  tests exit 0; both locked dependency sets audit with no known vulnerabilities.
  `docs/HARDENING_STATUS.md` carries the per-interpreter table and its limits.

Still requires an action outside this repository:

- A GitHub Actions run **on the pushed commit**; local exit codes are not that run.
- Creating the public repository and pushing (needs the maintainer's account).
- Enabling Discussions, private vulnerability reporting and `main` protection,
  then writing release notes from the verified facts above.
- Live acceptance the test suite cannot reach: a real OneBot endpoint, a real
  paid vision/embedding key exercising the daily budgets under traffic, and the
  desktop WebView2 launch on a real Windows desktop.
- Python 3.13 and 3.14 on Windows remain unverified, and the items in
  "Open findings deliberately left unfixed" need a product or deployment decision.
