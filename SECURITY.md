# Security policy

## Supported versions

Security fixes are applied to the latest public release on the default branch. The current supported line is `0.6.x`.

## Reporting a vulnerability

Do **not** post exploit details, credentials, personal data, or database extracts in a public issue.

After the GitHub repository is created, the maintainer must enable **Private vulnerability reporting** in the repository's Security settings. Use the resulting **Report a vulnerability** flow for confidential reports. If the private channel has not yet been enabled, open a public issue with no technical details and request a private contact route instead.

Include only what is needed to reproduce safely:

- affected version and deployment shape;
- a minimal, sanitised reproduction;
- impact and preconditions;
- suggested remediation, if known.

Maintainers aim to acknowledge a valid report within seven days, provide a status update within thirty days, and coordinate disclosure after a fix or mitigation is available. Timelines can change for complex dependency or upstream issues.

## Deployment hardening

- Keep `HOST=127.0.0.1` unless trusted TLS ingress and strong OneBot credentials are in place.
- Use a unique high-entropy `ADMIN_API_TOKEN`; rotate it if disclosure is suspected.
- Keep `.env`, `data/`, NapCat profiles, logs, and backup archives outside version control and protected by OS permissions.
- Apply migrations with the supplied tools and verified backups; do not copy a live SQLite database without its WAL considerations.
- Keep dependencies locked and run the secret scanner before publishing.

See [docs/PRIVACY.md](docs/PRIVACY.md) for data-handling responsibilities.
