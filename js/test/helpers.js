// Test doubles: scripted transport, manual timers, in-memory storage, fixed ids and clock.

/** Transport that answers from a script: numbers are statuses, Errors are thrown. */
export function scriptedTransport(script = []) {
  const batches = [];
  const replies = [...script];
  return {
    batches,
    async send(batch) {
      batches.push(structuredClone(batch));
      const next = replies.length ? replies.shift() : 200;
      if (next instanceof Error) throw next;
      return { status: next };
    },
  };
}

/** Timers that only fire when the test says so. */
export function manualScheduler() {
  const timers = new Map();
  let id = 0;
  return {
    timers,
    setTimeout(fn, ms) {
      id += 1;
      timers.set(id, { fn, ms });
      return id;
    },
    clearTimeout(handle) {
      timers.delete(handle);
    },
    /** Delays of armed timers. */
    delays() {
      return [...timers.values()].map((t) => t.ms);
    },
    /** Fire every armed timer once. */
    fireAll() {
      const due = [...timers.values()];
      timers.clear();
      for (const t of due) t.fn();
    },
  };
}

/** localStorage-like map; `fail` makes writes throw. */
export function memoryStorage(initial = {}) {
  const map = new Map(Object.entries(initial));
  const s = {
    map,
    fail: false,
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem(k, v) {
      if (s.fail) throw new Error("QuotaExceededError");
      map.set(k, String(v));
    },
    removeItem: (k) => void map.delete(k),
  };
  return s;
}

/** Deterministic uuid v4 strings. */
export function sequentialUuid() {
  let n = 0;
  return () => {
    n += 1;
    return `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
  };
}

/** Base options for a tracker under test. */
export function baseOptions(extra = {}) {
  const errors = [];
  return {
    errors,
    options: {
      transport: scriptedTransport(),
      consent: "granted",
      scheduler: manualScheduler(),
      uuid: sequentialUuid(),
      now: () => new Date("2026-11-14T10:00:00.000Z"),
      random: () => 0.5,
      isOnline: () => true,
      onError: (e) => errors.push(e),
      ...extra,
    },
  };
}

/** Let pending promise callbacks run. */
export const tick = () => new Promise((r) => setImmediate(r));
