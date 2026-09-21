# Contributing to Taffy

Thank you for improving Taffy. This project is primarily used in community-chat environments, so correctness, privacy, and safe failure matter more than feature count.

## Before opening a pull request

1. Search existing issues and discussions.
2. Keep each pull request focused on one user-visible change or one maintenance concern.
3. Add or update tests for behavioural changes. Never make a test depend on a live model provider, QQ account, or market API.
4. Run the quality gates from the [README](README.md).
5. Update user-facing documentation, configuration examples, or release notes when they change.

## Local development

```powershell
uv sync --frozen --all-groups
Copy-Item .env.example .env
# Fill only the credentials needed for your private test environment.
uv run --frozen python scripts/check_secrets.py
uv run --frozen python scripts/run_isolated_tests.py -- --cov=src --cov-report=term-missing
```

Do not commit `.env`, production data, QR-login files, database backups, screenshots containing private messages, real QQ identifiers, Steam inventory data, or provider output that contains personal information.

## Contribution standards

- Treat user messages, market responses, and provider output as untrusted input.
- Prefer explicit unavailable/error states over fabricated data or a silent fallback.
- Preserve conversation and permission boundaries: a private chat, a group, and each user are distinct scopes.
- Keep authenticated endpoints loopback-only by default. New externally reachable endpoints need tests for authentication, authorization, input bounds, and logging hygiene.
- Explain migrations and provide a rollback/recovery path for persistent-state changes.
- Update `uv.lock` (or `desktop/uv.lock`) with its corresponding `pyproject.toml`; do not hand-edit a lock file.

## Pull-request review

Maintainers review changes for tests, privacy impact, deployment/recovery impact, dependency provenance, and documentation. A review may request a smaller change or defer a feature when source terms, data quality, or safety boundaries are unclear.

By submitting a contribution, you agree that it may be distributed under the [Apache-2.0 License](LICENSE).
