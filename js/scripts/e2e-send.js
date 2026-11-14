// End-to-end sender: drives the real tracker against a local collector and prints a JSON summary.
// usage: node scripts/e2e-send.js <http://127.0.0.1:PORT/v1/batch> <count>
// Only loopback endpoints are accepted, so this script never sends data off the machine.

import { createTracker, fetchTransport } from "../src/index.js";

const [endpoint, countText] = process.argv.slice(2);
const url = new URL(endpoint ?? "");
if (!["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)) throw new Error("loopback endpoint only");
const count = Number(countText);
if (!Number.isInteger(count) || count < 1 || count > 10_000) throw new Error("count must be 1..10000");

const deadline = performance.now() + 30_000;
const errors = [];
const real = fetchTransport({ endpoint: url.href, timeoutMs: 5000 });
let online = false;
let failNext = 1;
let requests = 0;
let firstBatch = null;
/** First real attempt fails as if the network dropped; later ones go through. */
const flaky = {
  async send(batch) {
    requests += 1;
    if (failNext > 0) {
      failNext -= 1;
      throw new Error("simulated network drop");
    }
    const res = await real.send(batch);
    if (res.status === 200) firstBatch ??= batch;
    return res;
  },
};
const store = new Map();
const storage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => void store.set(k, String(v)),
  removeItem: (k) => void store.delete(k),
};

/** Wait until `done()` holds, polling, with an overall deadline. */
async function until(done) {
  while (!done()) {
    if (performance.now() > deadline) throw new Error("timed out waiting for the tracker");
    await new Promise((r) => setTimeout(r, 10));
  }
}

// 1. Consent denied: nothing is recorded.
const denied = createTracker({ transport: real, consent: "denied", onError: (e) => errors.push(e.message) });
for (let i = 0; i < 5; i += 1) denied.track("should_not_send", { i });

// 2. Consent pending, then offline, then granted: events wait, then go out with one retry.
const tracker = createTracker({
  transport: flaky,
  storage,
  batchSize: 10,
  flushIntervalMs: 20,
  backoff: { baseMs: 20, maxMs: 200 },
  isOnline: () => online,
  context: { page_url: "http://127.0.0.1/demo", locale: "en-GB" },
  onError: (e) => errors.push(e.message),
});
tracker.page("home");
for (let i = 1; i < count - 1; i += 1) tracker.track(i % 3 === 0 ? "signup_clicked" : "item_viewed", { item: `sku-${i}`, price: i * 1.5 });
tracker.setConsent("granted");
tracker.identify("user-42", { plan: "pro" });
const offline = await tracker.flush();
online = true;
await tracker.online();
await until(() => tracker.stats().queued === 0);
const stats = tracker.stats();
tracker.shutdown();

/** POST a raw body and return the status and JSON reply. */
async function post(body) {
  const res = await fetch(url.href, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body,
    signal: AbortSignal.timeout(5000),
  });
  return { status: res.status, body: await res.json() };
}

// 3. Replay the first delivered batch: every event must count as a duplicate.
const replay = await post(JSON.stringify(firstBatch));

// 4. Bad input: three invalid events next to one valid one, then a body that is not JSON.
const now = new Date().toISOString();
const base = { schema_version: 1, type: "track", anonymous_id: "anon-e2e-0001", timestamp: now };
const mixed = await post(
  JSON.stringify({
    sent_at: now,
    events: [
      { ...base, event_id: crypto.randomUUID(), name: "checkout_started" },
      { ...base, event_id: "not-a-uuid", name: "bad_id" },
      { ...base, event_id: crypto.randomUUID(), name: "1starts_with_digit" },
      { ...base, event_id: crypto.randomUUID(), name: "bad_time", timestamp: "2026-02-30T10:00:00Z" },
    ],
  }),
);
const garbage = await post("{not json");

console.log(
  JSON.stringify({
    sent: stats.sent,
    queued: stats.queued,
    dropped: stats.dropped,
    deniedDropped: denied.stats().dropped.consent,
    offlineStatus: offline.status,
    requests,
    replay,
    mixed,
    garbage,
    errors,
  }),
);
