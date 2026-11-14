import test from "node:test";
import assert from "node:assert/strict";
import { fetchTransport } from "../src/transport.js";

test("POSTs the batch as JSON without credentials and returns the status", async () => {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url, init });
    return { status: 202, body: null };
  };
  const t = fetchTransport({ endpoint: "https://collect.example.test/v1/batch", fetchImpl });
  const batch = { sent_at: "2026-11-14T10:00:00.000Z", events: [{ event_id: "a" }] };
  assert.deepEqual(await t.send(batch), { status: 202 });
  assert.equal(calls[0].url, "https://collect.example.test/v1/batch");
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.credentials, "omit");
  assert.equal(calls[0].init.headers["Content-Type"], "application/json");
  assert.deepEqual(JSON.parse(calls[0].init.body), batch);
  assert.ok(calls[0].init.signal instanceof AbortSignal);
});

test("a request that never answers is aborted by the timeout", async () => {
  const fetchImpl = (_url, init) =>
    new Promise((_resolve, reject) => init.signal.addEventListener("abort", () => reject(init.signal.reason)));
  const t = fetchTransport({ endpoint: "http://127.0.0.1:9/v1/batch", timeoutMs: 20, fetchImpl });
  await assert.rejects(t.send({ sent_at: "x", events: [] }), { name: "TimeoutError" });
});

test("network errors reject", async () => {
  const t = fetchTransport({
    endpoint: "http://127.0.0.1:9/v1/batch",
    fetchImpl: async () => {
      throw new TypeError("fetch failed");
    },
  });
  await assert.rejects(t.send({ sent_at: "x", events: [] }), /fetch failed/);
});

test("bad endpoints and options are refused at creation", () => {
  assert.throws(() => fetchTransport({ endpoint: "not a url" }), TypeError);
  assert.throws(() => fetchTransport({ endpoint: "ftp://x.test/" }), /http/);
  assert.throws(() => fetchTransport({ endpoint: "http://x.test/", timeoutMs: 0 }), RangeError);
  assert.throws(() => fetchTransport({ endpoint: "http://x.test/", fetchImpl: null }), /fetch/);
});
