# Security policy

## Reporting a vulnerability

Please report security problems privately through GitHub:
**Security > Report a vulnerability** on this repository
(`https://github.com/EquinoxWN/event-tracking-pipeline/security/advisories/new`). Do not open a public issue.

Include what you found, how to reproduce it, and the impact you expect. You will get an answer
within 7 days. Fixes are released as soon as they are ready, and you are credited unless you ask
not to be.

## Supported versions

This project is pre-1.0. Only the latest commit on `main` receives fixes.

## Scope

- **In scope:** The collector accepting an event that does not match the schema, losing a bad event instead of dead-lettering it, crashing or using unbounded memory on any request body, CORS headers sent to an origin that is not allowed, and the tracker sending or storing anything while consent is pending or denied.
- **Out of scope:** Performance problems, denial of service by request volume (no rate limiting in M1), and features listed for M2 or M3.

## How this repository protects itself

- Every GitHub Action is pinned to a full commit SHA, and workflows run with read-only
  permissions and without persisted credentials.
- Dependabot update pull requests are paused (`open-pull-requests-limit: 0`) so the repository keeps a single `main` branch; dependencies are updated by hand and checked by the CI audit.
- CI runs lint, types, tests, the end-to-end run and a known-vulnerability check (pip-audit, npm audit) on every push and pull request.
- The collector binds to 127.0.0.1 by default, caps request bodies at 256 KB and batches at 100 events, and allows no browser origin unless configured. The tracker has no runtime dependencies and talks to one configured endpoint only.
