# Architecture

Taffy is intentionally self-hosted: message handling and state storage remain under the operator's control, while optional providers are used only for features explicitly configured by that operator.

```text
QQ client
    │
    ▼
OneBot V11 implementation (for example, NapCat)
    │ WebSocket + optional shared secret
    ▼
bot.py / NoneBot2 application
    ├── chat, alert, inventory, news, scheduler, and admin plugins
    ├── policy and privacy filters
    ├── services: model provider, market data, rendering, HTTP client
    ├── state repositories
    │       └── local SQLite databases + backups/migrations
    ├── authenticated health/admin API
    │       └── optional Windows desktop studio
    └── external providers configured by the operator
```

## Trust boundaries

1. **Inbound messages:** QQ events and their contents are untrusted. Handlers validate command shape, scope, permissions, and input bounds before calling services.
2. **Remote data:** market APIs, web search, and model responses can be stale, incomplete, or adversarial. The service validates fields and returns an explicit unavailable state rather than inventing values.
3. **Credentials:** configuration is loaded from process environment and the local `.env` file; `.env.example` contains names and safe defaults only. No credential is a source-code default.
4. **State:** SQLite is the system of record for conversations, subscriptions, delivery work, and feature-specific state. Schema changes are versioned and guarded by migration tools.
5. **Operations API:** health endpoints are separate from authenticated admin operations. Default ingress is loopback; an operator must intentionally configure stronger network protections before wider exposure.

## Runtime lifecycle

At startup, the application loads configuration, applies safe ingress restrictions, initializes repositories, verifies state/schema health, and starts background jobs only after the application lifecycle is ready. On shutdown, tasks are cancelled in an orderly way and the state layer closes cleanly.

The bot is designed for one host with local SQLite. Do not share the database over SMB/NFS or between independently running hosts. Move to a server database before attempting multi-host operation.

## Desktop studio

`desktop/` is a separate Python project. It is an optional Windows-oriented shell for authenticated operational views; it has its own `pyproject.toml`, `uv.lock`, tests, and build process. The bot remains deployable without it.
