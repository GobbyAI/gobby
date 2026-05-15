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
