// The tracker: builds events, honours consent, batches them and retries delivery with backoff.

import { backoffDelay } from "./backoff.js";
import { EventQueue } from "./queue.js";

export const LIBRARY = Object.freeze({ name: "etp-js", version: "0.1.0" });
export const CONSENT = Object.freeze(["pending", "granted", "denied"]);

const NAME_RE = /^[A-Za-z][A-Za-z0-9_ .:-]{0,127}$/;
const PROP_RE = /^[A-Za-z_][A-Za-z0-9_]{0,63}$/;
const ANON_RE = /^[A-Za-z0-9_-]{8,64}$/;
const MAX_PROPS = 50;
const MAX_STRING = 1024;
const MAX_EVENT_BYTES = 32_768;
const RETRYABLE = new Set([408, 425, 429]);

/** Error type for calls the tracker refuses. */
export class TrackerError extends Error {
  /** @param {string} message @param {string} code */
  constructor(message, code) {
    super(message);
    this.name = "TrackerError";
    this.code = code;
  }
}

/**
 * Validate free-form properties: flat object, at most 50 keys, scalar values.
 * @param {unknown} props
 * @returns {Record<string, string | number | boolean | null>}
 */
export function checkProperties(props) {
  if (props === undefined) return {};
  if (props === null || typeof props !== "object" || Array.isArray(props)) {
    throw new TrackerError("properties must be a plain object", "invalid_properties");
  }
  const keys = Object.keys(props);
  if (keys.length > MAX_PROPS) throw new TrackerError(`at most ${MAX_PROPS} properties`, "invalid_properties");
  /** @type {Record<string, string | number | boolean | null>} */
  const out = {};
  for (const k of keys) {
    const v = /** @type {Record<string, unknown>} */ (props)[k];
    if (!PROP_RE.test(k)) throw new TrackerError(`bad property name ${JSON.stringify(k)}`, "invalid_properties");
    const scalar =
      v === null ||
      typeof v === "boolean" ||
      (typeof v === "number" && Number.isFinite(v)) ||
      (typeof v === "string" && v.length <= MAX_STRING && v.isWellFormed());
    if (!scalar) throw new TrackerError(`property ${k} must be a short string, finite number, boolean or null`, "invalid_properties");
    out[k] = /** @type {string | number | boolean | null} */ (v);
  }
  return out;
}

/**
 * Create a tracker.
 * @param {object} options
 * @param {import("./transport.js").Transport} options.transport  where batches go
 * @param {"pending" | "granted" | "denied"} [options.consent]  initial consent, default "pending"
 * @param {boolean} [options.globalPrivacyControl]  true starts the tracker as "denied"
 * @param {import("./queue.js").StorageLike | null} [options.storage]  localStorage-like, used only after consent
 * @param {string} [options.storageKey]
 * @param {number} [options.batchSize]  events per request, 1..100
 * @param {number} [options.maxBatchBytes]  JSON size per request
 * @param {number} [options.flushIntervalMs]
 * @param {number} [options.maxQueue]  events kept while offline; oldest dropped beyond this
 * @param {number} [options.maxAttempts]  failed sends before an event is dropped
 * @param {{baseMs?: number, maxMs?: number}} [options.backoff]
 * @param {Record<string, unknown>} [options.context]  static context (page_url, locale...)
 * @param {string} [options.anonymousId]
 * @param {() => Date} [options.now]
 * @param {() => number} [options.random]
 * @param {() => string} [options.uuid]
 * @param {() => boolean} [options.isOnline]
 * @param {{setTimeout: Function, clearTimeout: Function}} [options.scheduler]
 * @param {(err: Error) => void} [options.onError]
 */
export function createTracker(options) {
  const {
    transport,
    consent = "pending",
    globalPrivacyControl = false,
    storage = null,
    storageKey = "etp:queue",
    batchSize = 20,
    maxBatchBytes = 60_000,
    flushIntervalMs = 5000,
    maxQueue = 1000,
    maxAttempts = 6,
    backoff = {},
    context = {},
    anonymousId,
    now = () => new Date(),
    random = Math.random,
    uuid = () => globalThis.crypto.randomUUID(),
    isOnline = () => globalThis.navigator?.onLine ?? true,
    scheduler = globalThis,
    onError = (err) => console.warn("[etp]", err.message),
  } = options ?? {};

  if (!transport || typeof transport.send !== "function") throw new TypeError("transport.send is required");
  if (!CONSENT.includes(consent)) throw new TypeError(`consent must be one of ${CONSENT.join(", ")}`);
  if (!Number.isInteger(batchSize) || batchSize < 1 || batchSize > 100) throw new RangeError("batchSize must be 1..100");
  if (!Number.isInteger(maxAttempts) || maxAttempts < 1) throw new RangeError("maxAttempts must be >= 1");
  if (!(flushIntervalMs > 0)) throw new RangeError("flushIntervalMs must be > 0");
  if (!(maxBatchBytes >= MAX_EVENT_BYTES)) throw new RangeError(`maxBatchBytes must be >= ${MAX_EVENT_BYTES}`);
  if (anonymousId !== undefined && !ANON_RE.test(anonymousId)) throw new TypeError("anonymousId must match [A-Za-z0-9_-]{8,64}");
  backoffDelay(0, { ...backoff, random: () => 0 });

  const queue = new EventQueue({ maxSize: maxQueue, storage, key: storageKey, onError });
  const anonKey = `${storageKey}:anon`;
  const stats = { sent: 0, dropped: { consent: 0, invalid: 0, overflow: 0, rejected: 0, exhausted: 0 } };
  let state = globalPrivacyControl ? "denied" : consent;
  let anon = anonymousId ?? null;
  /** @type {string | null} */
  let userId = null;
  /** @type {unknown} */
  let timer = null;
  /** @type {Promise<{status: string}> | null} */
  let flushing = null;
  let failures = 0;
  let lastError = "";
  let closed = false;

  /** Anonymous id: given, stored from an earlier visit (only with consent), or new. */
  function anonymous() {
    if (anon) return anon;
    if (state === "granted" && storage) {
      try {
        const saved = storage.getItem(anonKey);
        if (saved !== null && ANON_RE.test(saved)) anon = saved;
      } catch (err) {
        onError(new Error(`could not read anonymous id: ${err.message}`));
      }
    }
    anon ??= uuid();
    rememberAnonymous();
    return anon;
  }

  /** Save the anonymous id, only with consent. */
  function rememberAnonymous() {
    if (state !== "granted" || !storage || !anon) return;
    try {
      storage.setItem(anonKey, anon);
    } catch (err) {
      onError(new Error(`could not save anonymous id: ${err.message}`));
    }
  }

  /** Arm the timer unless one is set (`force` replaces it, used for retries). */
  function schedule(delayMs, force = false) {
    if (closed || (timer !== null && !force)) return;
    if (timer !== null) scheduler.clearTimeout(timer);
    timer = scheduler.setTimeout(() => {
      timer = null;
      void flush();
    }, delayMs);
  }

  /** Build, check and enqueue one event; returns its id or null. */
  function enqueue(type, name, props, extra = {}) {
    if (closed) return null;
    if (state === "denied") {
      stats.dropped.consent += 1;
      return null;
    }
    let event;
    try {
      if (typeof name !== "string" || !NAME_RE.test(name)) throw new TrackerError(`bad event name ${JSON.stringify(name)}`, "invalid_name");
      event = {
        schema_version: 1,
        event_id: uuid(),
        type,
        name,
        anonymous_id: anonymous(),
        ...(userId !== null ? { user_id: userId } : {}),
        ...extra,
        timestamp: now().toISOString(),
        context: { ...context, library: { ...LIBRARY } },
        properties: checkProperties(props),
      };
      if (JSON.stringify(event).length > MAX_EVENT_BYTES) throw new TrackerError("event larger than 32 KB", "too_large");
    } catch (err) {
      stats.dropped.invalid += 1;
      onError(err);
      return null;
    }
    const dropped = queue.push(event);
    if (dropped) {
      stats.dropped.overflow += dropped;
      onError(new TrackerError(`queue full, dropped ${dropped} oldest event(s)`, "overflow"));
    }
    if (state === "granted") {
      if (queue.size >= batchSize) void flush();
      else schedule(flushIntervalMs);
    }
    return event.event_id;
  }

  /** Next batch: up to batchSize events and maxBatchBytes of JSON. */
  function nextBatch() {
    const entries = queue.peek(batchSize);
    let bytes = 40;
    let n = 0;
    for (const x of entries) {
      bytes += JSON.stringify(x.e).length + 1;
      if (n > 0 && bytes > maxBatchBytes) break;
      n += 1;
    }
    return entries.slice(0, n).map((x) => x.e);
  }

  /** One send; "sent", "rejected" (4xx, never retried) or "retry". */
  async function sendOnce(events) {
    try {
      const { status } = await transport.send({ sent_at: now().toISOString(), events });
      if (status >= 200 && status < 300) return "sent";
      if (status >= 400 && status < 500 && !RETRYABLE.has(status)) return `rejected:${status}`;
      lastError = `HTTP ${status}`;
      return "retry";
    } catch (err) {
      lastError = err instanceof Error ? err.message : String(err);
      return "retry";
    }
  }

  /** Send queued events until empty, offline, or a retry is scheduled. */
  async function drain() {
    while (state === "granted" && queue.size > 0) {
      if (!isOnline()) return { status: "offline" };
      const events = nextBatch();
      const ids = events.map((e) => e.event_id);
      const outcome = await sendOnce(events);
      if (outcome === "sent") {
        stats.sent += queue.removeIds(ids);
        failures = 0;
      } else if (outcome.startsWith("rejected")) {
        const n = queue.removeIds(ids);
        stats.dropped.rejected += n;
        onError(new TrackerError(`collector refused ${n} event(s) (HTTP ${outcome.slice(9)})`, "rejected"));
      } else {
        if (!isOnline()) return { status: "offline" };
        const dropped = queue.recordFailure(ids, maxAttempts);
        if (dropped) {
          stats.dropped.exhausted += dropped;
          onError(new TrackerError(`gave up on ${dropped} event(s) after ${maxAttempts} attempts: ${lastError}`, "exhausted"));
        }
        schedule(backoffDelay(failures, { ...backoff, random }), true);
        failures += 1;
        return { status: "retrying" };
      }
    }
    return { status: state === "granted" ? "idle" : state };
  }

  /** Start a drain unless one is running; never rejects. */
  function flush() {
    if (flushing) return flushing;
    const p = drain().catch((err) => {
      onError(err);
      return { status: "error" };
    });
    flushing = p;
    void p.then(() => {
      if (flushing === p) flushing = null;
    });
    return p;
  }

  /** Change consent: granted saves and sends the queue, denied deletes it. */
  function setConsent(next) {
    if (!CONSENT.includes(next)) throw new TypeError(`consent must be one of ${CONSENT.join(", ")}`);
    state = next;
    if (next === "granted") {
      queue.enablePersistence();
      rememberAnonymous();
      if (queue.size > 0) void flush();
    } else if (next === "denied") {
      stats.dropped.consent += queue.size;
      queue.clear();
      queue.disablePersistence();
      anon = anonymousId ?? null;
      if (storage) {
        try {
          storage.removeItem(anonKey);
        } catch (err) {
          onError(new Error(`could not remove anonymous id: ${err.message}`));
        }
      }
    }
  }

  if (state === "granted") queue.enablePersistence();

  return {
    /** Record a named action; returns the event id, or null when not recorded. */
    track: (name, properties) => enqueue("track", name, properties),
    /** Record a page view. */
    page: (name = "page_view", properties) => enqueue("page", name, properties),
    /** Attach a user id to this and later events. */
    identify(id, traits) {
      if (typeof id !== "string" || id.length < 1 || id.length > 128 || !id.isWellFormed()) {
        stats.dropped.invalid += 1;
        onError(new TrackerError("user id must be a string of 1 to 128 characters", "invalid_user"));
        return null;
      }
      if (state === "denied") {
        stats.dropped.consent += 1;
        return null;
      }
      userId = id;
      return enqueue("identify", "identify", traits);
    },
    setConsent,
    /** Current consent state. */
    consent: () => state,
    flush,
    /** Browser came back online: send now. */
    online: () => (state === "granted" && queue.size > 0 ? flush() : Promise.resolve({ status: "idle" })),
    /** Counters and queue length. */
    stats: () => ({ queued: queue.size, sent: stats.sent, dropped: { ...stats.dropped } }),
    /** Stop timers; queued events stay in storage for the next page. */
    shutdown() {
      closed = true;
      if (timer !== null) scheduler.clearTimeout(timer);
      timer = null;
    },
  };
}
