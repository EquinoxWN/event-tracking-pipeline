import test from "node:test";
import assert from "node:assert/strict";
import { EventQueue, parseEntries } from "../src/queue.js";
import { memoryStorage } from "./helpers.js";

const ev = (id) => ({ event_id: id, name: "x" });

test("push keeps order and drops the oldest beyond maxSize", () => {
  const q = new EventQueue({ maxSize: 3 });
  assert.deepEqual([q.push(ev("a")), q.push(ev("b")), q.push(ev("c")), q.push(ev("d"))], [0, 0, 0, 1]);
  assert.deepEqual(q.peek(10).map((x) => x.e.event_id), ["b", "c", "d"]);
  assert.equal(q.size, 3);
});

test("removeIds removes exactly the given events, wherever they are", () => {
  const q = new EventQueue({ maxSize: 10 });
  for (const id of ["a", "b", "c", "d"]) q.push(ev(id));
  assert.equal(q.removeIds(["b", "d", "zz"]), 2);
  assert.deepEqual(q.peek(10).map((x) => x.e.event_id), ["a", "c"]);
});

test("recordFailure counts attempts and drops events at maxAttempts", () => {
  const q = new EventQueue({ maxSize: 10 });
  q.push(ev("a"));
  q.push(ev("b"));
  assert.equal(q.recordFailure(["a"], 2), 0);
  assert.equal(q.peek(1)[0].n, 1);
  assert.equal(q.recordFailure(["a", "b"], 2), 1);
  assert.deepEqual(q.peek(10).map((x) => [x.e.event_id, x.n]), [["b", 1]]);
});

test("nothing is stored until persistence is enabled", () => {
  const s = memoryStorage();
  const q = new EventQueue({ maxSize: 5, storage: s, key: "k" });
  q.push(ev("a"));
  assert.equal(s.map.size, 0);
  q.enablePersistence();
  assert.deepEqual(JSON.parse(s.getItem("k")), [{ e: ev("a"), n: 0 }]);
});

test("enablePersistence merges a saved queue without duplicating ids", () => {
  const s = memoryStorage({ k: JSON.stringify([{ e: ev("old"), n: 2 }, { e: ev("a"), n: 0 }]) });
  const q = new EventQueue({ maxSize: 5, storage: s, key: "k" });
  q.push(ev("a"));
  q.push(ev("new"));
  assert.equal(q.enablePersistence(), 2);
  assert.deepEqual(q.peek(10).map((x) => x.e.event_id), ["old", "a", "new"]);
  assert.equal(q.peek(1)[0].n, 2);
});

test("a saved queue larger than maxSize keeps the newest events", () => {
  const saved = ["a", "b", "c", "d"].map((id) => ({ e: ev(id), n: 0 }));
  const q = new EventQueue({ maxSize: 2, storage: memoryStorage({ k: JSON.stringify(saved) }), key: "k" });
  q.enablePersistence();
  assert.deepEqual(q.peek(10).map((x) => x.e.event_id), ["c", "d"]);
});

for (const [label, text] of [
  ["not JSON", "{oops"],
  ["not an array", '{"a":1}'],
  ["entry without event_id", '[{"e":{},"n":0}]'],
  ["negative attempts", '[{"e":{"event_id":"a"},"n":-1}]'],
  ["null entry", "[null]"],
]) {
  test(`corrupt stored queue (${label}) is reported and cleared`, () => {
    const s = memoryStorage({ k: text });
    const errors = [];
    const q = new EventQueue({ maxSize: 5, storage: s, key: "k", onError: (e) => errors.push(e) });
    assert.equal(q.enablePersistence(), 0);
    assert.equal(errors.length, 1);
    assert.match(errors[0].message, /unreadable/);
    assert.equal(s.getItem("k"), "[]");
  });
}

test("a failing storage write is reported and the memory copy kept", () => {
  const s = memoryStorage();
  const errors = [];
  const q = new EventQueue({ maxSize: 5, storage: s, key: "k", onError: (e) => errors.push(e) });
  q.enablePersistence();
  s.fail = true;
  q.push(ev("a"));
  assert.equal(q.size, 1);
  assert.match(errors[0].message, /could not save/);
});

test("disablePersistence deletes the stored copy", () => {
  const s = memoryStorage();
  const q = new EventQueue({ maxSize: 5, storage: s, key: "k" });
  q.enablePersistence();
  q.push(ev("a"));
  q.disablePersistence();
  assert.equal(s.getItem("k"), null);
  q.push(ev("b"));
  assert.equal(s.getItem("k"), null);
});

test("maxSize must be a positive integer", () => {
  assert.throws(() => new EventQueue({ maxSize: 0 }), RangeError);
  assert.throws(() => new EventQueue({ maxSize: 1.5 }), RangeError);
});

test("parseEntries accepts well-formed entries", () => {
  assert.deepEqual(parseEntries('[{"e":{"event_id":"a"},"n":3}]'), [{ e: { event_id: "a" }, n: 3 }]);
});
