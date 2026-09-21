# Taffy / 塔菲

[![CI](https://github.com/2702207741-dev/taffy-qq-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/2702207741-dev/taffy-qq-bot/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12--3.14-blue.svg)](pyproject.toml)

Taffy is a self-hosted QQ community assistant for Counter-Strike 2 (CS2). It is built on NoneBot2 and OneBot V11, with a local SQLite state store, explicit privacy boundaries, market-data guardrails, and an optional desktop operations console.

塔菲是一个可自行部署的 CS2 QQ 社群助手。它以 NoneBot2 / OneBot V11 为基础，提供聊天、行情查询、价格预警、库存监控、新闻、社群工具与可选的桌面运维工作台。运行数据保存在你控制的本地 SQLite 数据库中。

> **Project status:** beta. This is a community project, not affiliated with Tencent, Valve, Steam, SteamDT, PandaScore, Anthropic, DeepSeek, or OpenAI. Market information is informational only and is not financial, investment, or trading advice.

## What it does

- Answers chat requests through a separately configured model provider; credentials are never embedded in source.
- Retrieves and presents CS2 market information with cache, freshness, field-validation, and explicit-unavailable-state safeguards.
- Supports watchlists, price alerts, inventory monitoring, scheduled broadcasts, official CS2 news, and chart rendering.
- Stores bot state in SQLite with migrations, integrity checks, backup tooling, and retention jobs.
- Keeps private chats, groups, users, memories, and style-learning records scoped to their own conversation boundary.
- Provides authenticated health and admin endpoints, plus an optional Windows desktop studio.

Read [the architecture guide](docs/ARCHITECTURE.md), [privacy guide](docs/PRIVACY.md), and [data-source policy](docs/DATA_SOURCES.md) before a production deployment.

## Quick start

### Prerequisites

- Python 3.12, 3.13, or 3.14
- [uv](https://docs.astral.sh/uv/)
- A OneBot V11 implementation, such as NapCat
- Provider credentials for whichever optional features you enable

```powershell
git clone https://github.com/2702207741-dev/taffy-qq-bot.git
cd taffy-qq-bot
uv sync --frozen
Copy-Item .env.example .env
# Edit .env: at minimum configure the OneBot connection and your chosen model provider.
uv run --frozen python bot.py
```

The service binds to loopback by default. Do not expose it to the internet merely to make setup easier.

## Configuration and security

`.env.example` documents every setting; it is the only configuration file intended for version control. Keep `.env`, database files, backups, NapCat profiles, and logs private.

- Use a high-entropy `ADMIN_API_TOKEN` (at least 32 characters) before enabling `/api/admin`.
- Leave `HOST=127.0.0.1` unless a trusted TLS ingress and strong OneBot credentials are configured.
- Use a dedicated low-privilege operating-system account for production.
- Run `uv run --frozen python scripts/check_secrets.py` before every push. The CI workflow scans reachable Git history too.
- See [SECURITY.md](SECURITY.md) for disclosure guidance and [docs/RELEASE_CHECKLIST.md](docs/RELEASE_CHECKLIST.md) for the publishing checklist.

## Development

```powershell
# Main bot quality gates
uv sync --frozen --all-groups
uv lock --check
uv run --frozen python scripts/check_secrets.py
uv run --frozen ruff check bot.py scrape_steamdt.py scripts src tests
uv run --frozen ruff format --check bot.py scrape_steamdt.py scripts src tests
uv run --frozen mypy
uv run --frozen python scripts/run_isolated_tests.py -- --cov=src --cov-report=term-missing

# Optional desktop studio (Windows)
Set-Location desktop
uv sync --frozen --all-groups
uv run --frozen pytest
```

The test launcher creates a fresh, credential-free source copy before running tests. It deliberately excludes `.env*` (except `.env.example`), runtime data, Git metadata, caches, and symlinks.

## Deployment

For a containerized installation, review [docker-compose.yml](docker-compose.yml) and the [release safety notes](docs/RELEASE_SAFETY.md), then use Docker Compose. The compose stack mounts `.env` read-only and binds service ports to loopback. Do not commit generated data or a logged-in NapCat profile.

The optional [desktop workbench](desktop/README.md) is a separate Windows-focused project with its own lock file and test suite.

## Contributing

Contributions, bug reports, security hardening, tests, and documentation are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md) and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). For security issues, do not open a public issue; follow [SECURITY.md](SECURITY.md).

## License and notices

Taffy source code is licensed under the [Apache License 2.0](LICENSE). The desktop UI includes vendored Vue and Apache ECharts; their licenses and attributions are in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
