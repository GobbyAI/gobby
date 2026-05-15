export interface TimeBoundLruOptions {
  maxEntries: number
  ttlMs: number
}

/**
 * Prune entries by age and LRU size. When maxEntries is <= 0, the entries Map
 * is cleared and the function returns early, treating zero or negative storage
 * as "keep nothing".
 */
export function pruneTimeBoundLru<K>(
  entries: Map<K, number>,
  now: number,
  { maxEntries, ttlMs }: TimeBoundLruOptions,
): void {
  for (const [key, lastSeenAt] of entries) {
    if (now - lastSeenAt >= ttlMs) {
      entries.delete(key)
    }
  }

  if (maxEntries <= 0) {
    entries.clear()
    return
  }

  const excessCount = entries.size - maxEntries
  if (excessCount <= 0) {
    return
  }

  const oldestEntries = Array.from(entries.entries()).sort(
    ([, leftLastSeenAt], [, rightLastSeenAt]) => leftLastSeenAt - rightLastSeenAt,
  )
  for (const [key] of oldestEntries.slice(0, excessCount)) {
    entries.delete(key)
  }
}
