# AI-assisted maintainer policy

Taffy may use AI tools to reduce maintenance burden, but AI output is never an autonomous authority over code, deployment, security, or user data.

## Allowed maintainer workflows

- classify and summarise sanitised issue reports;
- explain a failing deterministic test or CI log after sensitive values are removed;
- identify test coverage gaps and draft pull-request review notes;
- compare a reviewed diff to project invariants and draft release notes;
- propose documentation improvements for already-public interfaces.

## Prohibited inputs and actions

- Do not provide `.env` files, access tokens, API keys, cookies, QR-login state, production databases, raw private chats, identity mappings, or unredacted support bundles to an AI tool.
- Do not use AI tools to scan, probe, or review repositories and systems without explicit authority.
- Do not automatically merge, deploy, rotate credentials, contact users, or execute irreversible data changes based only on model output.
- Do not turn uncertain market or provider data into asserted facts.

## Human review requirements

A maintainer must review code, tests, security impact, dependency provenance, privacy impact, and release notes before merging. Generated text must be treated as an untrusted draft, especially when it interprets user input or external data.
