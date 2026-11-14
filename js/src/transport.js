// HTTP transport for batches: fetch with a timeout, no third-party SDK.

/**
 * @typedef {{send(batch: {sent_at: string, events: object[]}): Promise<{status: number}>}} Transport
 */

/**
 * Transport that POSTs JSON to `endpoint`; network errors and timeouts reject.
 * @param {{endpoint: string, timeoutMs?: number, fetchImpl?: typeof fetch}} options
 * @returns {Transport}
 */
export function fetchTransport({ endpoint, timeoutMs = 10_000, fetchImpl = globalThis.fetch }) {
  const url = new URL(endpoint);
  if (url.protocol !== "https:" && url.protocol !== "http:") throw new TypeError("endpoint must be http(s)");
  if (!(timeoutMs > 0)) throw new RangeError("timeoutMs must be > 0");
  if (typeof fetchImpl !== "function") throw new TypeError("no fetch implementation available");
  return {
    async send(batch) {
      const res = await fetchImpl(url.href, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(batch),
        keepalive: true,
        credentials: "omit",
        signal: AbortSignal.timeout(timeoutMs),
      });
      await res.body?.cancel();
      return { status: res.status };
    },
  };
}
