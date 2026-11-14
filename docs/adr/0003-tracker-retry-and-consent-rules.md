# ADR 0003: Tracker retry and consent rules

- **Status:** Accepted

## Context

A tracker must not lose events when the network is down, must not retry forever, must not resend
events the collector already refused, and must not keep data the user did not agree to.

## Decision

- **What is retried:** network errors, timeouts, HTTP 408, 425, 429 and 5xx. Other 4xx answers mean
  the collector rejected the request (and dead-lettered it); the events are dropped and reported
  through `onError` with code `rejected`.
- **How long:** each queued event counts its own failed attempts and is dropped after `maxAttempts`
  (default 6) with code `exhausted`. Retries wait an exponential, fully jittered delay
  (`random * min(60 s, 1 s * 2^n)`), reset after a success.
- **Offline is not a failure:** when `isOnline()` is false before or after a send, nothing is counted
  and the queue waits for `online()`. The queue is capped (`maxQueue`, default 1,000); beyond it the
  oldest events are dropped and reported with code `overflow`.
- **Consent gates storage, not just sending:** while `pending`, events live in memory only; on
  `granted` the queue and anonymous id are saved; on `denied` both are deleted and new events are not
  recorded. Global Privacy Control starts the tracker as `denied`.

## Consequences

- Bounded memory, storage and retry time, with every drop counted in `stats()` and reported.
- A collector outage longer than about six backoff rounds loses the affected events (reported, not
  silent); raising `maxAttempts` trades storage for durability.
- Events recorded while consent is pending are lost if the page closes before a decision. That is the
  price of storing nothing without consent.
- Because ids stay the same across retries, a request that succeeded but whose answer was lost is
  stored once (the collector ignores repeated ids).
