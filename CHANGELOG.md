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
