# Security hardening status (2026-09-21)

Status: hardened beta source candidate; not a security certification.
The old v0.6.0 ZIP and old Git metadata were moved to a private backup outside
the release directory. A replacement review ZIP was built from the new baseline;
it must be rebuilt after any subsequent source/documentation changes.

## Verified changes in this pass

- Remote desktop connections previously permitted plaintext HTTP, exposing the
  administrator bearer token to a network observer when used outside loopback.
  URL validation now requires HTTPS except for literal loopback IP addresses.
  This also applies to explicit API client URL overrides. Redirects remain disabled.
- Removed the unused JavaScript bridge method that returned the saved token.
  The UI uses the authenticated Python proxy instead. This reduces token exposure;
  it does not make a compromised frontend harmless, since the proxy is privileged.
- Desktop regression suite, lint and type checking passed after the credential
  and artwork changes. Main lint, formatting, lock checks and type checking passed.
- The full main isolated suite is green on every interpreter listed in
  "Verified platform matrix" below; that section holds the authoritative counts.
  Windows could not exercise symlink creation/escape cases and Linux directory
  descriptor protections; these are explicitly unverified on this host.
- Fixed stale credential reuse when keyring rotation fails: the newly saved
  in-memory credential now takes precedence over an older keyring value.
- Added destination-scoped keyring accounts. Existing unscoped credentials are
  deliberately not migrated: users must verify and save their token again.
  Proxy, event polling, log retrieval, self-test and token verification/save now
  pass an explicit destination snapshot to credential and HTTP operations.
  Regression tests switch settings during proxy loading and token verification
  and verify that the token stays with the original destination. In-memory
  fallback keeps account and token in one immutable pair, avoiding torn pairing.
- Added archive validation for committed-file identity, prohibited paths, missing
  and duplicate members, symlinks, Windows path aliases and credential patterns.
  The subsequent targeted archive/secret suite passed 36 tests after adding
  GitHub, AWS and Slack credential patterns.
- Replaced uncertain-provenance artwork with a newly drawn geometric SVG and
  rebuilt public history without the old artwork. Added complete upstream Vue,
  ECharts and D3 license/NOTICE files.
- Fixed the vision API daily-budget race: read/check/reserve now runs in one
  immediate SQLite transaction. A regression test launches 20 concurrent
  reservations with a budget of 3 and confirms exactly 3 succeed; zero-budget
  behavior is also covered. The related 21-test suite passed after this change.
- Fixed `bounded_env_text` reporting a documented blank as a configuration
  error. `.env.example` intentionally ships `TAFFY_BACKUP_LLM_MODEL=` empty and
  `src/services/llm.py` treats that as "unconfigured, fail-closed", but the
  helper grouped blank values with over-long or control-character values and
  logged `Ignoring invalid text setting` on every clean install. Blank now
  means unconfigured and stays silent, matching `env_secret`; only a
  present-but-unusable value is reported. The returned value is identical in
  every branch, so no behaviour depended on the log line. Added
  `tests/test_service_config_guards.py` (11 cases) for missing, blank,
  whitespace-only, valid, over-long and control-character inputs, plus the
  shipped `.env.example` default.
- Fixed a private-chat privacy boundary in the style-learning (slang) store.
  `persona_slang_repository.py` states that `scope_group_id == 0` collapses
  every user into one bucket and that `created_by` "MUST be part of the
  predicate", and `_scope_clause` raises when a mutation omits it -- but the
  read methods never applied it. `话术列表` in a DM therefore listed every other
  user's privately taught term, meaning and author QQ id, including rows still
  awaiting confirmation, and `prompt_terms` injected those same rows into an
  unrelated user's model prompt. `pending`, `accepted_all` and
  `accepted_for_injection` now take an optional speaker filter, and the
  user-facing reads -- the QQ listing, the index resolution behind
  `确认/删除/启用话术 N`, and prompt assembly in `src/services/llm.py` -- pass the
  requesting user for private scope. Group scope is deliberately unchanged
  because a group's vocabulary is shared by design, and the operator console
  keeps its unfiltered read. This also removes an index-skew fault where the
  numbering a user saw could address a different one of their own rows. Added
  three regression tests in `tests/test_slang_stickers.py`.
- Made the embedding and SteamDT-inspection daily budgets atomic, matching the
  earlier vision fix. Both read the counter, compared it to the cap, and then
  wrote in a *separate* transaction -- and each `database.read`/`database.write`
  runs in its own transaction on its own connection, so every concurrent caller
  observed the same count and all of them passed. This is not theoretical:
  against the pre-fix code, 20 concurrent reservations with a daily budget of 3
  returned `20 == True` (asserted as `assert 20 == 3`), and 20 reservations for
  one user with a 2/day per-user cap returned 20. The check and increment now
  share one transaction, and `tests/test_paid_quota_atomicity.py` pins the hard
  cap for the global budget, the per-user cap and the zero-budget case.
- Corrected an operator-facing pointer to a non-existent `SECURITY_REVIEW.md`
  in the admin API warning; it now names `docs/RELEASE_SAFETY.md`, which ships.
- Fixed the sticker manifest cache serving one library's contents to another.
  `_load_manifest()` cached for 30 seconds keyed only by time, so a read taken
  after the library directory changed returned the previous directory's result
  for the rest of the window. The cache is now keyed by the resolved directory
  as well. This surfaced as an order-dependent sticker-listing failure in
  `tests/test_desktop_api_v4.py` that reproduced on Python 3.12 and passed on
  3.13/3.14 in the same run, so it was a latent timing fault rather than a
  platform difference; any interpreter could have failed it. Added a
  cross-directory cache regression test that also pins the intended behaviour
  (a same-directory write is still served from cache, and `reset_runtime_state()`
  still forces a reload).
- `download_inbound_image` fetched a message-supplied image with `client.get`,
  which buffers the whole response, and only `persist` applied
  `_MAX_IMAGE_BYTES` -- so the rejection came after the allocation. One QQ image
  segment naming a large object could therefore make the bot process pull
  hundreds of megabytes inside the 20 s timeout window. The fetch now streams and
  stops at the cap, and short-circuits on an oversize `Content-Length`. The
  regression test counts bytes actually pulled from a mock transport: 400 KiB
  served against a 4 KiB cap reads 5 KiB and saves nothing. Both new behaviour
  tests were run against the previous code and failed there (exit 1), while the
  accepted-image test passed on both, so they pin the fix rather than restating it.

## Verified platform matrix

Each Linux row used the source extracted from this release archive, a separate
isolated uv environment per interpreter, and `scripts/run_isolated_tests.py`.
None of them used the private upstream project.

| Python | Platform | Isolated suite | Exit |
| --- | --- | --- | --- |
| 3.12 | Windows | 2,020 passed, 1 skipped, 75 subtests passed | 0 |
| 3.12.3 | Linux (WSL Ubuntu) | 2,021 passed, 75 subtests passed | 0 |
| 3.13.13 | Linux (WSL Ubuntu) | 2,021 passed, 75 subtests passed | 0 |
| 3.14.6 | Linux (WSL Ubuntu) | 2,021 passed, 75 subtests passed | 0 |

Both platforms collect 2,021 items: the Windows run records one of them as
skipped, each Linux run collects and passes all 2,021. The three Linux rows and
the Windows row used the archive
`84705363772b9906949d753ab5a9ffb5e67acebf032609699526ac7b82964f5e`
(commit `fc1cf6d`), and every count above was read from that run's isolated
`pytest.log` rather than inferred. This page was then rebuilt into the shipped
archive with documentation-only changes, so the shipped hash differs while the
tested source under `src/`, `scripts/`, `tests/` and `bot.py` does not.

All four runs used `scripts/run_isolated_tests.py`, which copies the tree into a
throwaway directory instead of testing in place; the three Linux runs unpacked
the release archive itself rather than a working checkout. The Linux 3.12
interpreter also cleared Ruff (`All checks passed!`), `ruff format --check`
(306 files) and mypy (150 source files). `scripts/check_secrets.py` returned
exit 2 there with `Secret scan could not complete: git ls-files failed`, because
an extracted archive has no Git metadata: the scanner refuses to report a pass on
an incomplete file set, so that gate is only satisfied in a real checkout (Windows,
exit 0) and in CI.

Warnings are not failures, but two kinds are worth recording rather than
smoothing over. The 3.13 run emitted 102 warnings and the 3.14 run 204, where
3.12 emitted none. The `DeprecationWarning`s all traced to upstream
`nonebot/typing.py:103` calling the private `ForwardRef._evaluate` without
`type_params`, from `nonebot` itself and from `tests/test_chat_market.py` and
`tests/test_reddit_news.py` importing it. CPython documents that this "will be
disallowed in Python 3.15" and that the private API "will be removed in Python
3.16", so a future NoneBot release is a prerequisite for supporting those
interpreters -- which is outside the declared `>=3.12,<3.15` window.

One 3.14 warning did not reproduce and is therefore reported as noise, not as a
property: the earlier run showed two `PytestUnraisableExceptionWarning` entries
from an exception ignored inside
`asyncio.selector_events._SelectorTransport.__del__` during
`test_index_daily_lifecycle.py::test_load_closes_serves_only_finalized_history`,
and the re-run of the same suite at `fc1cf6d` showed none. A warning that appears
in one run of 2,021 tests is a nondeterministic stdlib teardown path, and saying
"3.14 emits these" would overstate what was observed.

One caveat that must not be smoothed over: the first Linux run of this matrix
**failed** on 3.12 while 3.13 and 3.14 passed the same archive. That was not a
platform quirk -- it was the sticker-cache timing fault described above, and the
earlier "Linux 3.12 passed" result had been luck. A green interpreter is not
evidence that a timing-dependent defect is absent.

`requires-python = ">=3.12,<3.15"` is therefore demonstrated on Linux for all
three interpreters. **Python 3.13 and 3.14 remain unverified on Windows**, and
the desktop package is deliberately pinned to `>=3.12,<3.13`. This table is not
Windows coverage evidence for 3.13 or 3.14.

## First-run documentation probe

Following only the documented path (`copy .env.example .env`, then run
`bot.py`) on Linux/Python 3.13 with every credential left blank:

- The bot starts, loads 13 plugins and reports `Env key audit: 151 loaded,
  0 unknown`, so `.env.example` and the settings the code reads agree.
- Uvicorn binds `http://127.0.0.1:8080`, confirming the documented loopback
  default.
- The SteamDT integrity gate rejects an empty catalog
  (`Full catalog rejected by integrity gate entries=0 required=1000`) rather
  than presenting invented market data.
- Repeated OneBot websocket reconnect errors are expected: no NapCat or other
  OneBot implementation was running locally. Real QQ delivery is not covered by
  this probe and requires an operator-supplied endpoint.
- With every key blank no paid provider request was possible.

## Release checks and remaining external verification

- History/working-tree secret scans passed. Main and desktop dependency audits
  found no known advisories in the locked packages at the time checked. These
  results do not cover unknown vulnerabilities or establish safety of every dependency.
- Source-archive identity and credential scanning are now part of CI. Run them
  again on each newly built artifact; hashes identify a file, not its security.
- Desktop bridge navigation, external model responses, server permission flows
  and deployment-level controls still warrant an independent adversarial review.
  Existing regression tests are not evidence of exhaustive coverage.
- The Linux interpreter matrix above is verified. Python 3.13 and 3.14 on
  Windows, a real QQ/OneBot/model deployment, interactive WebView2 operation
  and executable packaging have not been verified locally. GitHub Actions must
  pass on the exact published commit; a local run is not CI evidence.
- English README and a Chinese getting-started guide are provided; this is not a
  claim that the entire application UI is multilingual.
- Check application statements against public evidence. Publication, adoption,
  maintainer identity/authority, and ongoing activity cannot be manufactured by
  code changes. No approval probability is known.

## Open findings deliberately left unfixed in this pass

A focused code trace of the desktop bridge, the HTTP surface, and the
injection/cost boundaries produced the items below. They are recorded rather
than patched because each one needs a product or deployment decision, an
interactive WebView2 session, or a live OneBot/provider that this environment
cannot exercise. They are *not* believed to be exploitable today, and they are
*not* proven safe either.

Desktop studio (`desktop/`):

- The pywebview `js_api` bridge exposes 18 callables (including an
  authenticated `http_request` proxy, `import_token_from_bot_env`, and
  `start_bot`) with no origin or navigation allowlist. pywebview re-injects its
  shim on every navigation and does not check the caller's origin, so any
  document that ever renders in this window can reach all of them. No XSS or
  navigation path was found in the first-party frontend -- every value reaches
  the DOM through Vue interpolation or text nodes, and CSP is
  `default-src 'self'` with `connect-src 'none'` -- so this is a missing
  control rather than a demonstrated break.
- `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--allow-file-access-from-files`
  relaxes Chromium's `file://` isolation, which would let a hypothetical DOM
  injection read local files. Whether the CSP blocks that was not tested.
- Switching `remote_base_url` re-targets traffic and the credential namespace
  with no out-of-band confirmation before a token is sent to a newly entered
  HTTPS origin.

Bot service (`src/`):

- OneBot ingress is only authenticated when `ONEBOT_V11_SECRET` /
  `ONEBOT_V11_ACCESS_TOKEN` are set, and `.env.example` ships both blank. All
  QQ-side privilege derives from the event's `user_id`, and uvicorn does not
  validate `Host`, so a local process -- or a browser page after DNS
  rebinding -- could forge a superuser event on a loopback bind. Deciding this
  needs an operator's real topology.
- `POST /api/desktop/chat` keys its 12-per-minute limiter on a
  caller-supplied `session` string, so rotating that value raises the effective
  ceiling to the global 30-per-minute LLM limiter. An existing test asserts the
  per-session keying, so correcting it changes documented behaviour.
- `/api/admin/*` carries no rate limit at all, and the desktop market/write
  limiters use fixed global keys, meaning one caller can lock out the others.
- The `kline_data` model tool constructs a fresh screenshot service per call,
  bypassing the shared item cache and negative cache, and has no daily budget
  unlike vision/embedding/inspect. `query_market_item`, `case_ev` and
  `lookup_inventory` are network tools without a cap.
- Any group member can teach and confirm group slang without an admin check,
  and that text reaches other members' prompts. Mitigations escape the tag and
  instruct the model to ignore embedded directions; instruction-following is
  not provably closed. Whether group slang needs a moderation gate is a
  product decision.
- `search_chat_history` intentionally searches `g_<group>_*`, i.e. other
  members' stored turns for that group; this is documented as intended.
- The vision fetch follows a message-supplied URL with only an `http(s)`
  prefix check and no host allowlist. (The size cap is now enforced while
  streaming; see the verified changes above.)

## Evidence limits

No finite scan or test suite proves absence of every vulnerability. This work is
AI-assisted review, not a substitute for an independent professional security
audit. Production deployments handling private messages require operator-specific
access controls, retention decisions, and security review.

The official program documentation was checked on 2026-09-21
([Codex for OSS](https://developers.openai.com/community/codex-for-oss),
[terms](https://learn.chatgpt.com/docs/codex-for-oss-terms)). It states that
applicants should be a core maintainer or run a widely used public project,
that entitlements are capped at six months of ChatGPT Pro with Codex, that
extended Codex Security access needs write access and is granted case by case,
and that a repository without mainstream visibility is invited to "explain why"
it matters. No star, contributor or usage threshold is published. The
`openai.com/.../form/codex-for-oss/` page itself returns HTTP 403 to automated
retrieval, so the form's live field list has not been re-read here. Selection
remains OpenAI's decision and no approval probability is known.
