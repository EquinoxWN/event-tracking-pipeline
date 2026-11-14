# RFC 0001: event-tracking-pipeline design

- **Status:** Accepted for M1 (M2 and M3 sections remain proposals)
- **Author:** EquinoxWN
- **Created:** 2026-11-14

## Problem

Product analytics breaks quietly. A third-party SDK sends data to someone else's servers and decides
on its own what to retry and what to drop; a release renames an event and nothing fails; a browser
goes offline and the events of that visit are gone; a malformed event is dropped at the edge and
nobody can say how many there were. Teams then argue about numbers instead of using them.

This project builds the pipeline in small, testable parts that a team owns end to end: a JavaScript
tracker without third-party SDKs, a collector that checks every event against a versioned JSON
Schema and keeps the bad ones in a dead-letter table, and (later) SQL models for sessions.

**Users:** engineers who add tracking to a web app and analysts who need to trust the counts.

## Critical-to-quality measures (Define)

| CTQ | Target (M1) | How it is measured |
|---|---|---|
| No silent loss at the collector | 100% of received events end up in `events` or `dead_letters` | `test_collector.py` mixed and invalid batches; e2e: 201 stored + 4 dead letters for exactly what was sent |
| No double counting on retry | a retried batch adds 0 rows | store and collector tests; e2e replays a delivered batch: `accepted 0, duplicates 10` |
| Delivery survives offline and errors | 0 events lost by the tracker in the e2e run (offline start, one network drop) | `scripts/e2e.py`: `sent == events`, `dropped` all 0 |
| Consent respected | 0 events sent or stored while consent is pending or denied | `tracker.test.js` consent tests; e2e: 5 events under `denied`, none stored |
| Bounded resources | body <= 256 KB, <= 100 events per batch, tracker queue <= `maxQueue` | HTTP 413/400 tests, queue overflow test |

## Goals

- M1: a small JavaScript tracker batches events, retries when offline and respects consent settings.
- M1: a Python collector validates each event against a JSON Schema; bad events go to a dead-letter
  table instead of disappearing.
- M2: every event carries a unique ID, so retries and duplicates are removed downstream; SQL models
  sessionize events (30 minutes of inactivity), stitch anonymous and logged-in identities, and filter bots.
- M3: volume checks alert when an event suddenly drops; tests replay a day of synthetic traffic and
  assert that sessions and counts are exact.

## Non-goals

- Running as a hosted production service, or sending data to any real analytics endpoint.
- A browser SDK distribution (bundling, CDN, npm publishing) in M1.
- Rate limiting, authentication of senders, or bot filtering at the collector in M1.
- Server-side tracking libraries for other languages.

## Design (M1)

### Event contract

`python/src/event_tracking_pipeline/schemas/event.v1.json` (JSON Schema Draft 2020-12) is the single
contract. Required: `schema_version` (const 1), `event_id` (UUID v4), `type` (`track`, `page`,
`identify`), `name`, `anonymous_id`, `timestamp` (UTC, RFC 3339 with `Z`). Optional: `user_id`
(required for `identify`), `context`, and `properties` (flat, at most 50 keys, scalar values).
Unknown top-level fields are rejected so typos surface as dead letters; new fields will only be added
as optional (rule: never break compatibility). The collector adds two checks the schema language
cannot express: the timestamp is a real calendar date, and no string holds a lone surrogate.

A batch is `{"sent_at": <UTC timestamp, optional>, "events": [1..100 events]}`.

### Tracker (`js/src`)

- `createTracker()` builds events, validates names and properties before queuing (invalid calls are
  counted and reported through `onError`, never thrown into the host page), and stamps each event
  with a random UUID that stays the same on every retry.
- **Consent:** `pending` (default) keeps events in memory only and sends nothing; `granted` saves the
  queue and anonymous id to `localStorage`-like storage and sends; `denied` drops new events and
  deletes the queue and anonymous id. A Global Privacy Control signal starts the tracker as `denied`.
- **Batching:** a batch goes out when `batchSize` events are queued or after `flushIntervalMs`;
  a batch is also cut to stay under `maxBatchBytes` (default 60,000, below the 64 KB browsers allow
  for `keepalive` requests).
- **Retry:** network errors, timeouts, 408, 425, 429 and 5xx are retried with exponential backoff
  and full jitter (1 s base, 60 s cap). Each event carries its failed-attempt count and is dropped,
  with an error report, after `maxAttempts` (6). Other 4xx answers are not retried: the collector has
  already dead-lettered the batch. When the browser is offline, nothing is sent and no attempt is
  counted; `online()` resumes.
- **One flush at a time:** concurrent `flush()` calls share one drain. Delivered events are removed
  by id, not by position, so events added, dropped or cleared while a request is in flight are
  never removed by mistake.

### Collector (`python/src/event_tracking_pipeline`)

- `Collector.ingest(body)` parses JSON (NaN and Infinity refused), checks the envelope, validates
  each event, then writes good events and dead letters in one SQLite transaction.
- Dead-letter reasons: `invalid_json`, `invalid_envelope` (whole body, HTTP 400) and `schema`
  (per event, HTTP 200 with counts). Each row keeps the reason, the problems with their JSON paths,
  the payload (capped at 16 KB), first and last seen times, and a `seen` count.
- **Idempotent writes:** `events.event_id` is the primary key (`INSERT OR IGNORE`, first write
  wins); a dead letter's key is the SHA-256 of reason and payload. Replaying a batch changes no
  counts except `seen`.
- A storage failure returns 503 so the tracker retries; nothing is acknowledged that was not
  committed.
- HTTP: a WSGI app served by the standard library's threaded server on 127.0.0.1 by default:
  `POST /v1/batch`, `OPTIONS /v1/batch` (CORS, only for configured origins), `GET /healthz`. Body
  limit 256 KB (413), `Content-Length` required (411), JSON or `text/plain` only (415), 10 s socket
  timeout. The reasons for the standard library and SQLite are in
  [ADR 0002](../adr/0002-stdlib-wsgi-and-sqlite-for-m1.md).

## Alternatives considered

| Option | Why not (yet) |
|---|---|
| Use an existing SDK (Segment analytics.js, RudderStack, Snowplow tracker) | The point is to own and test the delivery rules; the project must not send data to third parties. Their designs informed this one (persistent queue, per-event retry count). |
| FastAPI + pydantic for the collector | Adds about ten packages for two routes; pydantic models would duplicate the JSON Schema, giving two sources of truth. Revisit if the API grows (ADR 0002). |
| Validate in the tracker against the full JSON Schema (e.g. with ajv) | Adds a runtime dependency and kilobytes to every page; the collector must validate anyway because clients can be old or hostile. The tracker checks only what prevents obviously bad calls. |
| Drop invalid events at the collector and log them | Logs rotate and are not queryable; the dead-letter table can be counted, inspected and replayed after a fix. |
| Deduplicate only downstream in SQL (no primary key on `event_id`) | Would store every retry and push the cost to every query; M1 already needs idempotent writes for the tracker's retries. M2 still adds downstream deduplication for duplicates that arrive with different ids. |
| `navigator.sendBeacon` as the only transport | No response status, so no retry decision; it remains a candidate for the page-unload flush in M2. |

## Measurement plan (Measure)

- Unit and HTTP tests for each rule above (`python/tests`, `js/test`), and hand mutations of the core
  functions (`docs/quality/2026-11-01.md`).
- `scripts/e2e.py`: the real tracker (Node.js `fetch`) sends 200 events to the real collector on
  127.0.0.1, starting offline and with one simulated network drop, plus 5 events under denied consent,
  a replayed batch, a mixed batch (1 good, 3 bad) and a non-JSON body. It asserts exact counts.
- Results with commands, machine and counts in `docs/results/m1.md`.

## Milestones

- **M1 (done):** tracker with batching, offline retry and consent; collector with JSON Schema
  validation and a dead-letter table.
- **M2:** downstream deduplication; SQL models for sessions (30 minutes of inactivity), identity
  stitching and bot filtering.
- **M3:** volume-drop alerts; replay of a day of synthetic traffic with exact session counts.

## Risks and open questions

- Client clocks are wrong: `timestamp` is stored as sent, next to the collector's `received_at`.
  Correcting skew with `sent_at` (as Segment does) is left for M2 sessionization.
- First write wins for a reused `event_id`; a client bug that reuses ids would hide events. M2 can
  count id reuse with differing payloads.
- SQLite with one writer lock serialises writes; fine for local use, not for high volume (ADR 0002).
- The tracker runs in Node.js tests only; real browser behaviour (`keepalive`, `localStorage` quota,
  `online` events) is not tested in M1. Browser tests, if added, run in CI only.
