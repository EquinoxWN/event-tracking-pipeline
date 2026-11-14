import test from "node:test";
import assert from "node:assert/strict";
import { backoffDelay } from "../src/backoff.js";

test("delay doubles per attempt up to the cap", () => {
  const top = (a) => backoffDelay(a, { baseMs: 100, maxMs: 1000, random: () => 0.999999 });
  assert.deepEqual([0, 1, 2, 3, 4, 10].map(top), [99, 199, 399, 799, 999, 999]);
});

test("full jitter: random 0 gives 0 and the delay never reaches the ceiling", () => {
  assert.equal(backoffDelay(3, { baseMs: 100, random: () => 0 }), 0);
  for (let i = 0; i < 200; i += 1) {
    const d = backoffDelay(2, { baseMs: 100, maxMs: 10_000 });
    assert.ok(d >= 0 && d < 400, `delay ${d}`);
  }
});

test("huge attempt numbers stay finite and capped", () => {
  assert.equal(backoffDelay(5000, { baseMs: 1, maxMs: 50, random: () => 0.5 }), 25);
});

test("invalid arguments are refused", () => {
  assert.throws(() => backoffDelay(-1), RangeError);
  assert.throws(() => backoffDelay(1.5), RangeError);
  assert.throws(() => backoffDelay(0, { baseMs: 0 }), RangeError);
  assert.throws(() => backoffDelay(0, { baseMs: 10, maxMs: 5 }), RangeError);
  assert.throws(() => backoffDelay(0, { random: () => 1 }), RangeError);
  assert.throws(() => backoffDelay(0, { random: () => Number.NaN }), RangeError);
});
