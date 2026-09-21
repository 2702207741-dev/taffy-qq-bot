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
