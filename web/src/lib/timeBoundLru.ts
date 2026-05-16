export interface TimeBoundLruOptions {
  maxEntries: number
  ttlMs: number
}

const FULL_SORT_MAX_ENTRIES = 128
const BOUNDED_SELECTION_MAX_EXCESS = 16

function selectOldestKeys<K>(entries: Map<K, number>, excessCount: number): K[] {
  if (
    entries.size <= FULL_SORT_MAX_ENTRIES ||
    excessCount > BOUNDED_SELECTION_MAX_EXCESS
  ) {
    return Array.from(entries.entries())
      .sort(
        ([, leftLastSeenAt], [, rightLastSeenAt]) =>
          leftLastSeenAt - rightLastSeenAt,
      )
      .slice(0, excessCount)
      .map(([key]) => key)
  }

  const oldestEntries: Array<[K, number]> = []
  for (const entry of entries.entries()) {
    const [, lastSeenAt] = entry
    if (oldestEntries.length < excessCount) {
      oldestEntries.push(entry)
      oldestEntries.sort(
        ([, leftLastSeenAt], [, rightLastSeenAt]) =>
          rightLastSeenAt - leftLastSeenAt,
      )
      continue
    }
    if (lastSeenAt < oldestEntries[0][1]) {
      oldestEntries[0] = entry
      oldestEntries.sort(
        ([, leftLastSeenAt], [, rightLastSeenAt]) =>
          rightLastSeenAt - leftLastSeenAt,
      )
    }
  }
  return oldestEntries.map(([key]) => key)
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
  for (const [, lastSeenAt] of entries) {
    if (now < lastSeenAt) {
      return
    }
  }

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

  for (const key of selectOldestKeys(entries, excessCount)) {
    entries.delete(key)
  }
}
