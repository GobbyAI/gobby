interface TimeBoundLruOptions {
  maxEntries: number
  ttlMs: number
}

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

  while (entries.size > maxEntries) {
    let oldestKey: K | undefined
    let oldestSeenAt = Number.POSITIVE_INFINITY
    let foundOldest = false
    for (const [key, lastSeenAt] of entries) {
      if (!foundOldest || lastSeenAt < oldestSeenAt) {
        oldestKey = key
        oldestSeenAt = lastSeenAt
        foundOldest = true
      }
    }
    if (!foundOldest) return
    entries.delete(oldestKey as K)
  }
}
