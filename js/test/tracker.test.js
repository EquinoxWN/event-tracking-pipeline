import test from "node:test";
import assert from "node:assert/strict";
import { createTracker, checkProperties, TrackerError } from "../src/index.js";
import { baseOptions, memoryStorage, scriptedTransport, tick } from "./helpers.js";

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const LONE_HIGH = String.fromCharCode(0xd800);
const LONE_LOW = String.fromCharCode(0xdc00);

/** Transport whose sends wait until the test calls release(). */
function heldTransport() {
  const t = {
    batches: [],
    release: () => {},
    send(batch) {
      t.batches.push(batch);
      return new Promise((r) => {
        t.release = () => r({ status: 200 });
      });
    },
  };
  return t;
}

test("events have the schema v1 shape", async () => {
  const { options } = baseOptions({ anonymousId: "anon-123456", context: { locale: "en-GB" } });
  const t = createTracker(options);
  t.track("item_viewed", { sku: "a1", price: 9.5, gift: false, note: null });
  await t.flush();
  const [batch] = options.transport.batches;
  assert.equal(batch.sent_at, "2026-11-14T10:00:00.000Z");
  assert.deepEqual(batch.events[0], {
    schema_version: 1,
    event_id: "00000000-0000-4000-8000-000000000001",
    type: "track",
    name: "item_viewed",
    anonymous_id: "anon-123456",
    timestamp: "2026-11-14T10:00:00.000Z",
    context: { locale: "en-GB", library: { name: "etp-js", version: "0.1.0" } },
    properties: { sku: "a1", price: 9.5, gift: false, note: null },
  });
});

test("default ids are random UUID v4 and the anonymous id is reused", async () => {
  const { options } = baseOptions();
  delete options.uuid;
  const t = createTracker(options);
  t.track("a");
  t.track("b");
  await t.flush();
  const [e1, e2] = options.transport.batches[0].events;
  assert.match(e1.event_id, UUID_RE);
  assert.notEqual(e1.event_id, e2.event_id);
  assert.equal(e1.anonymous_id, e2.anonymous_id);
});

test("a full batch is sent at once; a partial one waits for the timer", async () => {
  const { options } = baseOptions({ batchSize: 3, flushIntervalMs: 5000 });
  const t = createTracker(options);
  t.track("a");
  t.track("b");
  await tick();
  assert.equal(options.transport.batches.length, 0);
  assert.deepEqual(options.scheduler.delays(), [5000]);
  t.track("c");
  await tick();
  assert.deepEqual(options.transport.batches.map((b) => b.events.length), [3]);
  t.track("d");
  options.scheduler.fireAll();
  await tick();
  assert.deepEqual(options.transport.batches.map((b) => b.events.length), [3, 1]);
  assert.deepEqual(t.stats(), { queued: 0, sent: 4, dropped: { consent: 0, invalid: 0, overflow: 0, rejected: 0, exhausted: 0 } });
});

test("a long queue is drained in batches of batchSize, in order", async () => {
  const { options } = baseOptions({ batchSize: 4, consent: "pending" });
  const t = createTracker(options);
  for (let i = 0; i < 10; i += 1) t.track("e", { i });
  t.setConsent("granted");
  await t.flush();
  assert.deepEqual(options.transport.batches.map((b) => b.events.length), [4, 4, 2]);
  const order = options.transport.batches.flatMap((b) => b.events.map((e) => e.properties.i));
  assert.deepEqual(order, [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]);
});

test("batches are split to stay under maxBatchBytes", async () => {
  const { options } = baseOptions({ batchSize: 100, maxBatchBytes: 32_768, consent: "pending" });
  const t = createTracker(options);
  const props = Object.fromEntries(["a", "b", "c", "d", "e", "f", "g"].map((k) => [k, k.repeat(1000)]));
  for (let i = 0; i < 8; i += 1) t.track("big", props);
  t.setConsent("granted");
  await t.flush();
  const sizes = options.transport.batches.map((b) => JSON.stringify(b).length);
  assert.ok(options.transport.batches.length >= 2, "expected more than one request");
  for (const s of sizes) assert.ok(s <= 32_768 + 100, `batch of ${s} bytes`);
  assert.equal(t.stats().sent, 8);
});

test("pending consent queues in memory only and sends nothing", async () => {
  const storage = memoryStorage();
  const { options } = baseOptions({ consent: "pending", storage });
  const t = createTracker(options);
  t.track("a");
  await t.flush();
  assert.equal(options.transport.batches.length, 0);
  assert.equal(storage.map.size, 0);
  assert.equal(t.stats().queued, 1);
  assert.deepEqual(options.scheduler.delays(), []);
});

test("granting consent stores and sends what was queued", async () => {
  const storage = memoryStorage();
  const { options } = baseOptions({ consent: "pending", storage });
  const t = createTracker(options);
  t.track("a");
  t.setConsent("granted");
  assert.ok(storage.getItem("etp:queue"));
  assert.ok(storage.getItem("etp:queue:anon"));
  await t.flush();
  assert.equal(options.transport.batches[0].events[0].name, "a");
});

test("denied consent records nothing and deletes the stored queue and anonymous id", async () => {
  const storage = memoryStorage();
  const { options } = baseOptions({ consent: "granted", storage, isOnline: () => false });
  const t = createTracker(options);
  t.track("a");
  t.track("b");
  t.setConsent("denied");
  assert.equal(t.track("c"), null);
  assert.equal(t.identify("u1"), null);
  assert.equal(storage.getItem("etp:queue"), null);
  assert.equal(storage.getItem("etp:queue:anon"), null);
  assert.deepEqual(t.stats().dropped.consent, 4);
  assert.equal(t.stats().queued, 0);
  await t.flush();
  assert.equal(options.transport.batches.length, 0);
});

test("Global Privacy Control starts the tracker denied", () => {
  const { options } = baseOptions({ consent: "granted", globalPrivacyControl: true });
  const t = createTracker(options);
  assert.equal(t.consent(), "denied");
  assert.equal(t.track("a"), null);
});

test("a new anonymous id is made after consent is withdrawn", async () => {
  const storage = memoryStorage();
  const { options } = baseOptions({ storage });
  const t = createTracker(options);
  t.track("a");
  await t.flush();
  const first = options.transport.batches[0].events[0].anonymous_id;
  t.setConsent("denied");
  t.setConsent("granted");
  t.track("b");
  await t.flush();
  assert.notEqual(options.transport.batches[1].events[0].anonymous_id, first);
});

test("the anonymous id survives a reload when consent was given", async () => {
  const storage = memoryStorage();
  const a = baseOptions({ storage });
  const ta = createTracker(a.options);
  ta.track("x");
  await ta.flush();
  const b = baseOptions({ storage, uuid: () => "00000000-0000-4000-8000-00000000beef" });
  const tb = createTracker(b.options);
  tb.track("y");
  await tb.flush();
  assert.equal(b.options.transport.batches[0].events[0].anonymous_id, a.options.transport.batches[0].events[0].anonymous_id);
});

test("offline: nothing is attempted and no attempt is counted; online() sends", async () => {
  let online = false;
  const { options } = baseOptions({ isOnline: () => online, maxAttempts: 1 });
  const t = createTracker(options);
  t.track("a");
  assert.deepEqual(await t.flush(), { status: "offline" });
  assert.deepEqual(await t.flush(), { status: "offline" });
  assert.equal(options.transport.batches.length, 0);
  online = true;
  assert.deepEqual(await t.online(), { status: "idle" });
  assert.equal(t.stats().sent, 1);
});

test("a send that fails because the network dropped does not use up an attempt", async () => {
  let online = true;
  const transport = {
    async send() {
      online = false;
      throw new TypeError("fetch failed");
    },
  };
  const { options } = baseOptions({ transport, isOnline: () => online, maxAttempts: 1 });
  const t = createTracker(options);
  t.track("a");
  assert.deepEqual(await t.flush(), { status: "offline" });
  assert.equal(t.stats().queued, 1);
  assert.equal(t.stats().dropped.exhausted, 0);
});

test("5xx, 429 and network errors are retried with growing backoff, then sent", async () => {
  const transport = scriptedTransport([503, new TypeError("fetch failed"), 429, 200]);
  const { options } = baseOptions({ transport, random: () => 0.999, backoff: { baseMs: 100, maxMs: 10_000 } });
  const t = createTracker(options);
  t.track("a");
  const delays = [];
  for (let i = 0; i < 3; i += 1) {
    assert.deepEqual(await t.flush(), { status: "retrying" });
    delays.push(...options.scheduler.delays());
    options.scheduler.timers.clear();
  }
  assert.deepEqual(delays, [99, 199, 399]);
  assert.deepEqual(await t.flush(), { status: "idle" });
  assert.equal(transport.batches.length, 4);
  assert.equal(new Set(transport.batches.map((b) => b.events[0].event_id)).size, 1, "same event id on every retry");
  assert.equal(t.stats().sent, 1);
});

test("the retry timer resends without a manual flush", async () => {
  const transport = scriptedTransport([500]);
  const { options } = baseOptions({ transport });
  const t = createTracker(options);
  t.track("a");
  await t.flush();
  options.scheduler.fireAll();
  await tick();
  assert.equal(transport.batches.length, 2);
  assert.equal(t.stats().sent, 1);
});

test("events are dropped and reported after maxAttempts failed sends", async () => {
  const transport = scriptedTransport([500, 502, 503]);
  const { options, errors } = baseOptions({ transport, maxAttempts: 3 });
  const t = createTracker(options);
  t.track("a");
  for (let i = 0; i < 3; i += 1) await t.flush();
  assert.equal(t.stats().queued, 0);
  assert.equal(t.stats().dropped.exhausted, 1);
  assert.match(errors.at(-1).message, /gave up on 1 event\(s\) after 3 attempts: HTTP 503/);
  assert.equal(errors.at(-1).code, "exhausted");
});

test("a 4xx answer is not retried; the events are dropped and reported", async () => {
  const transport = scriptedTransport([400]);
  const { options, errors } = baseOptions({ transport });
  const t = createTracker(options);
  t.track("a");
  t.track("b");
  assert.deepEqual(await t.flush(), { status: "idle" });
  assert.equal(transport.batches.length, 1);
  assert.equal(t.stats().dropped.rejected, 2);
  assert.equal(errors[0].code, "rejected");
  assert.deepEqual(options.scheduler.delays(), [5000], "only the flush timer, no retry");
});

test("408 is retried like a server error", async () => {
  const transport = scriptedTransport([408]);
  const { options } = baseOptions({ transport });
  const t = createTracker(options);
  t.track("a");
  assert.deepEqual(await t.flush(), { status: "retrying" });
});

test("success resets the backoff", async () => {
  const transport = scriptedTransport([500, 500, 200, 500]);
  const { options } = baseOptions({ transport, random: () => 0.999, backoff: { baseMs: 100 } });
  const t = createTracker(options);
  t.track("a");
  await t.flush();
  await t.flush();
  await t.flush();
  options.scheduler.timers.clear();
  t.track("b");
  await t.flush();
  assert.deepEqual(options.scheduler.delays(), [99]);
});

test("a queue saved by one page is sent by the next", async () => {
  const storage = memoryStorage();
  const first = baseOptions({ storage, isOnline: () => false });
  const t1 = createTracker(first.options);
  t1.track("a");
  t1.track("b");
  t1.shutdown();
  const second = baseOptions({ storage });
  const t2 = createTracker(second.options);
  assert.equal(t2.stats().queued, 2);
  await t2.flush();
  assert.deepEqual(second.options.transport.batches[0].events.map((e) => e.name), ["a", "b"]);
  assert.equal(JSON.parse(storage.getItem("etp:queue")).length, 0);
});

test("consent withdrawn while a send is in flight: an event queued later is not lost", async () => {
  const transport = heldTransport();
  const { options } = baseOptions({ transport });
  const t = createTracker(options);
  t.track("a");
  const inflight = t.flush();
  t.setConsent("denied");
  t.setConsent("granted");
  t.track("b");
  transport.release();
  await tick();
  assert.equal(t.stats().queued, 1, "event b must still be queued after a's send completes");
  transport.release();
  await inflight;
  assert.deepEqual(transport.batches.map((b) => b.events.map((e) => e.name)), [["a"], ["b"]]);
  assert.equal(t.stats().queued, 0);
});

test("events added during a send are kept and sent next", async () => {
  const transport = heldTransport();
  const { options } = baseOptions({ transport, batchSize: 2 });
  const t = createTracker(options);
  t.track("a");
  t.track("b");
  await tick();
  t.track("c");
  transport.release();
  await tick();
  transport.release();
  await tick();
  assert.deepEqual(transport.batches.map((b) => b.events.map((e) => e.name)), [["a", "b"], ["c"]]);
  assert.equal(t.stats().sent, 3);
});

test("concurrent flush calls share one drain", async () => {
  const { options } = baseOptions();
  const t = createTracker(options);
  t.track("a");
  const [p1, p2] = [t.flush(), t.flush()];
  assert.equal(p1, p2);
  await Promise.all([p1, p2]);
  assert.equal(options.transport.batches.length, 1);
  await t.flush();
  assert.equal(options.transport.batches.length, 1);
});

test("queue overflow drops the oldest and reports it", () => {
  const { options, errors } = baseOptions({ maxQueue: 2, isOnline: () => false });
  const t = createTracker(options);
  t.track("a");
  t.track("b");
  t.track("c");
  assert.equal(t.stats().dropped.overflow, 1);
  assert.equal(errors[0].code, "overflow");
});

test("identify attaches the user id to later events", async () => {
  const { options } = baseOptions();
  const t = createTracker(options);
  t.track("before");
  t.identify("user-1", { plan: "pro" });
  t.page();
  await t.flush();
  const events = options.transport.batches[0].events;
  assert.deepEqual(events.map((e) => [e.type, e.name, e.user_id]), [
    ["track", "before", undefined],
    ["identify", "identify", "user-1"],
    ["page", "page_view", "user-1"],
  ]);
  assert.deepEqual(events[1].properties, { plan: "pro" });
});

for (const [label, call] of [
  ["empty name", (t) => t.track("")],
  ["name starting with a digit", (t) => t.track("1x")],
  ["non-string name", (t) => t.track(42)],
  ["array properties", (t) => t.track("a", [1])],
  ["nested property", (t) => t.track("a", { o: { x: 1 } })],
  ["NaN property", (t) => t.track("a", { n: Number.NaN })],
  ["long string property", (t) => t.track("a", { s: "x".repeat(1025) })],
  ["bad property name", (t) => t.track("a", { "bad-key": 1 })],
  ["too many properties", (t) => t.track("a", Object.fromEntries(Array.from({ length: 51 }, (_, i) => [`k${i}`, i])))],
  ["empty user id", (t) => t.identify("")],
  ["lone surrogate in a property", (t) => t.track("a", { s: `x${LONE_HIGH}` })],
  ["lone surrogate in the user id", (t) => t.identify(LONE_LOW)],
]) {
  test(`invalid call is refused, counted and reported: ${label}`, () => {
    const { options, errors } = baseOptions();
    const t = createTracker(options);
    assert.equal(call(t), null);
    assert.equal(t.stats().dropped.invalid, 1);
    assert.equal(t.stats().queued, 0);
    assert.ok(errors[0] instanceof TrackerError);
  });
}

test("an event over 32 KB is refused", () => {
  const { options, errors } = baseOptions();
  const t = createTracker(options);
  const props = Object.fromEntries(Array.from({ length: 40 }, (_, i) => [`k${i}`, "x".repeat(1000)]));
  assert.equal(t.track("a", props), null);
  assert.equal(errors[0].code, "too_large");
});

test("bad options are refused at creation", () => {
  const ok = baseOptions().options;
  assert.throws(() => createTracker({ ...ok, transport: undefined }), /transport/);
  assert.throws(() => createTracker({ ...ok, consent: "yes" }), /consent/);
  assert.throws(() => createTracker({ ...ok, batchSize: 0 }), RangeError);
  assert.throws(() => createTracker({ ...ok, batchSize: 101 }), RangeError);
  assert.throws(() => createTracker({ ...ok, maxAttempts: 0 }), RangeError);
  assert.throws(() => createTracker({ ...ok, flushIntervalMs: 0 }), RangeError);
  assert.throws(() => createTracker({ ...ok, maxBatchBytes: 1000 }), RangeError);
  assert.throws(() => createTracker({ ...ok, anonymousId: "short" }), /anonymousId/);
  assert.throws(() => createTracker({ ...ok, backoff: { baseMs: -1 } }), RangeError);
  assert.throws(() => createTracker(ok).setConsent("maybe"), /consent/);
});

test("shutdown stops timers and later calls record nothing", () => {
  const { options } = baseOptions();
  const t = createTracker(options);
  t.track("a");
  assert.equal(options.scheduler.timers.size, 1);
  t.shutdown();
  assert.equal(options.scheduler.timers.size, 0);
  assert.equal(t.track("b"), null);
});

test("checkProperties keeps scalars and returns a copy", () => {
  const input = { a: 1, b: "x", c: true, d: null };
  const out = checkProperties(input);
  assert.deepEqual(out, input);
  assert.notEqual(out, input);
  assert.deepEqual(checkProperties(undefined), {});
  assert.throws(() => checkProperties(null), TrackerError);
});
