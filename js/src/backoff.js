// Exponential backoff with full jitter, capped.

/**
 * Delay before retry number `attempt` (0-based): random in [0, min(maxMs, baseMs * 2^attempt)).
 * @param {number} attempt
 * @param {{baseMs?: number, maxMs?: number, random?: () => number}} [options]
 * @returns {number}
 */
export function backoffDelay(attempt, { baseMs = 1000, maxMs = 60_000, random = Math.random } = {}) {
  if (!Number.isInteger(attempt) || attempt < 0) throw new RangeError("attempt must be an integer >= 0");
  if (!(baseMs > 0) || !(maxMs >= baseMs)) throw new RangeError("need 0 < baseMs <= maxMs");
  const ceiling = Math.min(maxMs, baseMs * 2 ** Math.min(attempt, 30));
  const r = random();
  if (!(r >= 0 && r < 1)) throw new RangeError("random() must return a number in [0, 1)");
  return Math.floor(r * ceiling);
}
