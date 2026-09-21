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
- The latest full main isolated suite completed: 1,998 passed, 1 skipped, 75 subtests passed.
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

## Release checks and remaining external verification

- History/working-tree secret scans passed. Main and desktop dependency audits
  found no known advisories in the locked packages at the time checked. These
  results do not cover unknown vulnerabilities or establish safety of every dependency.
- Source-archive identity and credential scanning are now part of CI. Run them
  again on each newly built artifact; hashes identify a file, not its security.
- Desktop bridge navigation, external model responses, server permission flows
  and deployment-level controls still warrant an independent adversarial review.
  Existing regression tests are not evidence of exhaustive coverage.
- The full Linux/Python compatibility matrix, a real QQ/OneBot/model deployment,
  interactive WebView2 operation and executable packaging have not been verified
  in this local run. GitHub CI must pass on the exact published commit.
- English README and a Chinese getting-started guide are provided; this is not a
  claim that the entire application UI is multilingual.
- Check application statements against public evidence. Publication, adoption,
  maintainer identity/authority, and ongoing activity cannot be manufactured by
  code changes. No approval probability is known.

## Evidence limits

No finite scan or test suite proves absence of every vulnerability. This work is
AI-assisted review, not a substitute for an independent professional security
audit. Production deployments handling private messages require operator-specific
access controls, retention decisions, and security review.

The official [Codex for OSS application page](https://openai.com/zh-Hans-CN/form/codex-for-oss/)
was checked on 2026-09-21: selection considers meaningful usage, adoption or
ecosystem importance and active maintenance. Selection remains OpenAI's decision.
