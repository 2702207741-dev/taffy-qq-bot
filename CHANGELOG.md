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
- The embedding and SteamDT-inspection daily budgets are now reserved inside one transaction. Each check and increment previously ran in its own transaction, so concurrent calls all saw the same count and all passed: measured against the old code, 20 concurrent requests against a 3/day budget all succeeded, as did 20 requests against a 2/day per-user cap.
- The sticker manifest cache is keyed by library directory as well as time. It previously timed only, so a read taken within the 30-second window after the library location changed was served the other directory's contents. This appeared as an order-dependent sticker-listing failure that could strike any interpreter.
- The listener now refuses requests whose `Host` names a domain it was not configured for. `.env.example` ships `ONEBOT_V11_SECRET` blank, in which case the OneBot adapter verifies no signature at all, and uvicorn never checked `Host` -- so a page in the operator's own browser could be DNS-rebound onto `127.0.0.1` and post an event whose `user_id` decides superuser status. Loopback names and literal IP addresses (which a browser cannot rebind) keep working, `TAFFY_ALLOWED_HOSTS` adds DNS names, and `*` opts out. Websocket handshakes are covered as well as HTTP. An unsigned local POST still succeeds by design, because requiring a credential there would break a plain local gateway.
- An inbound image is now streamed with the size cap enforced while reading. `download_inbound_image` used `client.get`, which buffers the entire response before `persist` rejected an oversized payload, so a single message naming a large URL made the bot process allocate all of it inside the timeout window. A regression test counts the bytes actually pulled from a mock transport: 400 KiB served against a 4 KiB cap now reads 5 KiB and rejects, where the previous code read all 400 KiB.
