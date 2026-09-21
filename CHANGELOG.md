# Changelog

All notable public-release changes are documented in this file. Taffy follows semantic versioning for the main bot package.

## [0.6.0] - 2026-09-21

### Added

- First standalone open-source distribution, including an Apache-2.0 license, third-party notices, contributor governance, security disclosure policy, data/privacy documentation, and issue/PR templates.
- A reproducible quality baseline: locked dependencies, secret-history scanning, linting, formatting, type checks, isolation tests, coverage reporting, and GitHub Actions.
- Separate CI coverage for the Windows-oriented desktop studio.

### Changed

- Renamed the published Python package to `taffy-qq-bot`.
- Removed local credentials, runtime databases, cached market data, local agent artefacts, screenshots, deployment traces, and private planning material from the public distribution.

### Fixed

- Private-chat style learning no longer crosses users: `话术列表`, the index resolution behind `确认/删除/启用话术 N`, and prompt assembly now filter the shared private bucket by speaker, so one user cannot read another's privately taught terms, meanings or author QQ id, and those terms are no longer injected into an unrelated user's prompt. Group vocabulary and the operator console keep their intended unfiltered reads. This also corrects an index-skew fault where the numbering shown could address a different one of the requester's own rows.
- A blank setting in `.env.example` (for example `TAFFY_BACKUP_LLM_MODEL=`) no longer logs `Ignoring invalid text setting` on every start; blanks mean "unconfigured", while over-long and control-character values are still rejected and reported.
- The admin API startup notice now points at `docs/RELEASE_SAFETY.md` instead of a `SECURITY_REVIEW.md` that was never published.
