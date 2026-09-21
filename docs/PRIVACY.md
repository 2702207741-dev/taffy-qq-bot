# Privacy and data handling

Taffy is self-hosted software. The person who deploys it is responsible for telling community members how it is configured and for complying with the laws, platform rules, and provider terms that apply to that deployment.

## Data the service may process

Depending on enabled features, the bot may process QQ event metadata and message text, user/group identifiers, explicit memory commands, subscriptions and alerts, Steam identifiers supplied for inventory monitoring, configuration, operational logs, and market-query parameters.

The main state is stored in local SQLite databases under `data/` by default. Runtime data and backups are intentionally excluded from Git.

Important: the separate `usage_log` audit store can record user/group identifiers and raw input/output content. Anonymised telemetry does not mean all storage is anonymous. Restrict access to the audit database, define retention, and include this collection in your community privacy notice. Forgetting a memory does not automatically erase audit logs or backups.

## Optional third-party processing

Model, web-search, market, esports, and image/inspection features call third-party services only when the operator configures the corresponding setting. Content necessary for a configured provider request can be sent to that provider. Review its privacy policy, retention controls, geographic processing, and account terms before enabling it.

Do not send production `.env` files, full database files, raw private-chat logs, identity mappings, or screenshots containing personal data to an AI tool, an issue tracker, or a pull request.

## Design safeguards

- Private and group conversation scopes are kept distinct; user-scoped records are not meant to cross those boundaries.
- Storage does not always mirror that boundary, so read scoping is the enforcement point. Taught expressions (`persona_slang`) keep no user column in their key: every private-chat entry lands in one shared bucket identified by `scope_group_id = 0`, with `created_by` as the only per-user discriminator. The user-facing listing, the index used by the confirm/delete/enable commands, and prompt assembly filter that bucket by speaker; the operator console can still deliberately read the whole bucket. Anyone with direct database access therefore sees all users' private entries interleaved, and a future query that forgets the discriminator reintroduces the leak — treat the database file itself as private-chat material.
- Long-term memory is based on explicit user commands rather than automatic extraction from ordinary chat.
- The service can use anonymised, aggregated operational telemetry; its documented intent is to avoid saving QQ message content, user identifiers, Steam IDs, item names, and bot replies in that telemetry store.
- Logs should be access-controlled and rotated by the operator. Never publish logs without a careful data review.
- Users can use the bot's deletion/forget commands for the records described in the command documentation. Operators must still account for backups, provider-side processing, and any legal retention obligations.

## Operator checklist

1. Set a local privacy notice for the group before enabling features that call external providers.
2. Grant database and log directories only to the service account and necessary administrators.
3. Define a backup-retention and deletion policy, then test restore procedures.
4. Keep admin endpoints private and rotate credentials after suspected exposure.
5. Treat support bundles as sensitive data and redact them before sharing.
