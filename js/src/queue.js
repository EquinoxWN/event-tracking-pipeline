// Bounded event queue that can be saved to localStorage-like storage.

/**
 * @typedef {{getItem(key: string): string | null, setItem(key: string, value: string): void, removeItem(key: string): void}} StorageLike
 * @typedef {{e: Record<string, unknown>, n: number}} Entry  event and its failed send attempts
 */

export class EventQueue {
  /**
   * Queue of at most `maxSize` events; the oldest are dropped first.
   * @param {{maxSize: number, storage?: StorageLike | null, key?: string, onError?: (err: Error) => void}} options
   */
  constructor({ maxSize, storage = null, key = "etp:queue", onError = () => {} }) {
    if (!Number.isInteger(maxSize) || maxSize < 1) throw new RangeError("maxSize must be an integer >= 1");
    this.maxSize = maxSize;
    this.storage = storage;
    this.key = key;
    this.onError = onError;
    /** @type {Entry[]} */
    this.items = [];
    this.persistent = false;
  }

  /** Number of queued events. */
  get size() {
    return this.items.length;
  }

  /**
   * Start saving to storage and load what an earlier page saved (corrupt data is reported and cleared).
   * @returns {number} events loaded
   */
  enablePersistence() {
    if (!this.storage || this.persistent) return 0;
    this.persistent = true;
    let saved = [];
    try {
      const text = this.storage.getItem(this.key);
      if (text !== null) saved = parseEntries(text);
    } catch (err) {
      this.onError(new Error(`stored queue unreadable, discarded: ${err.message}`));
      this.#remove();
    }
    const known = new Set(this.items.map((x) => x.e.event_id));
    const merged = saved.filter((x) => !known.has(x.e.event_id)).concat(this.items);
    this.items = merged.slice(Math.max(0, merged.length - this.maxSize));
    this.save();
    return saved.length;
  }

  /** Stop saving and delete the stored copy. */
  disablePersistence() {
    this.persistent = false;
    this.#remove();
  }

  /**
   * Append one event.
   * @param {Record<string, unknown>} event
   * @returns {number} events dropped to make room (0 or 1)
   */
  push(event) {
    this.items.push({ e: event, n: 0 });
    const dropped = Math.max(0, this.items.length - this.maxSize);
    if (dropped) this.items.splice(0, dropped);
    this.save();
    return dropped;
  }

  /**
   * The first `count` entries, oldest first.
   * @param {number} count
   * @returns {Entry[]}
   */
  peek(count) {
    return this.items.slice(0, count);
  }

  /**
   * Remove the given events wherever they are now (others may have been added or dropped meanwhile).
   * @param {Iterable<unknown>} ids
   * @returns {number} entries removed
   */
  removeIds(ids) {
    const gone = new Set(ids);
    const before = this.items.length;
    this.items = this.items.filter((x) => !gone.has(x.e.event_id));
    this.save();
    return before - this.items.length;
  }

  /**
   * Count one failed attempt for the given events and drop those that reached `maxAttempts`.
   * @param {Iterable<unknown>} ids
   * @param {number} maxAttempts
   * @returns {number} events dropped
   */
  recordFailure(ids, maxAttempts) {
    const hit = new Set(ids);
    let dropped = 0;
    this.items = this.items.filter((x) => {
      if (!hit.has(x.e.event_id)) return true;
      x.n += 1;
      if (x.n >= maxAttempts) {
        dropped += 1;
        return false;
      }
      return true;
    });
    this.save();
    return dropped;
  }

  /** Drop everything, in memory and in storage. */
  clear() {
    this.items = [];
    this.save();
  }

  /** Write the queue to storage when persistence is on; failures are reported, memory copy kept. */
  save() {
    if (!this.persistent || !this.storage) return;
    try {
      this.storage.setItem(this.key, JSON.stringify(this.items));
    } catch (err) {
      this.onError(new Error(`could not save queue: ${err.message}`));
    }
  }

  /** Delete the stored copy; failures are reported. */
  #remove() {
    if (!this.storage) return;
    try {
      this.storage.removeItem(this.key);
    } catch (err) {
      this.onError(new Error(`could not remove stored queue: ${err.message}`));
    }
  }
}

/**
 * Parse and check stored entries.
 * @param {string} text
 * @returns {Entry[]}
 */
export function parseEntries(text) {
  const data = JSON.parse(text);
  if (!Array.isArray(data)) throw new TypeError("stored queue is not an array");
  for (const x of data) {
    const ok =
      x !== null &&
      typeof x === "object" &&
      Number.isInteger(x.n) &&
      x.n >= 0 &&
      x.e !== null &&
      typeof x.e === "object" &&
      typeof x.e.event_id === "string";
    if (!ok) throw new TypeError("stored queue has a malformed entry");
  }
  return data;
}
