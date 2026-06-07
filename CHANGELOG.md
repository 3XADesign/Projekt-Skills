# Changelog

All notable changes to **projekt-skills** are documented here.
This project adheres to [Semantic Versioning](https://semver.org/).

## [0.1.0] — unreleased

Initial public scaffold.

### Added
- Claude Code plugin (`projekt-skills`) distributed via the `3xa-projekt` marketplace.
- Primary orchestration skill **`projekt`**: the `CONNECT → DISCOVER → PLAN → CREATE → ASSIGN → ESTIMATE → TIME → DOCUMENT → REPORT` pipeline, endpoint cheatsheet, full-surface spec discovery, and safety guardrails (dry-run default, ledger, destructive-action confirmation, fingerprint-only token logging).
- Task skills: **`projekt-issues`**, **`projekt-estimate`**, **`projekt-workload`**, **`projekt-time`**, **`projekt-docs`**.
- Shared contract: `lib/http.sh` (dual-auth headers + `X-Org-Id` + rate-limit backoff), `auth_check.sh`, `context_sync.sh`, `spec_lookup.sh` / `spec_index.sh` (the OpenAPI spec never enters context), `run_ledger.sh`, `slim.jq`.
- `spec-drift-check` CI to keep the endpoint cheatsheet in sync with the live spec.
