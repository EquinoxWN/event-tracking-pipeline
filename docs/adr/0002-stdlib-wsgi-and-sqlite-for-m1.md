# ADR 0002: Standard-library WSGI server and SQLite for the M1 collector

- **Status:** Accepted

## Context

The scaffold planned a FastAPI collector writing to DuckDB or Postgres. M1 needs two routes
(`POST /v1/batch`, `GET /healthz`), JSON Schema validation, and an atomic, idempotent write of good
events and dead letters. Postgres would need a service container, which is CI-only on this machine.

## Decision

- The collector is a plain WSGI callable (`Collector`), served by `wsgiref` with a threading mix-in
  and a 10 s socket timeout. Any WSGI server (gunicorn, waitress) can host it unchanged.
- Validation uses `jsonschema` against the one schema file; there are no pydantic models.
- Storage is SQLite (standard library) in WAL mode, with one lock around writes and one transaction
  per batch. `event_id` is the primary key; dead letters are keyed by a SHA-256 fingerprint.

## Consequences

- One runtime dependency (`jsonschema`); tests run without any server process or container, and the
  WSGI app is tested in-process as well as over real HTTP.
- No request validation layer duplicates the schema: one source of truth.
- `wsgiref` is not a hardened production server (no keep-alive tuning, no TLS); it binds to
  127.0.0.1 by default and the README says to put a real WSGI server or proxy in front.
- SQLite serialises writes. That is enough for local and test traffic; DuckDB or Postgres for the
  warehouse tables come with the M2 SQL models, reading from these tables.
