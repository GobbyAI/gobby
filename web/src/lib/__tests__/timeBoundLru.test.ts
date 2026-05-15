import { describe, expect, it } from 'vitest'
import { pruneTimeBoundLru } from '../timeBoundLru'

describe('pruneTimeBoundLru', () => {
  it('removes expired entries before enforcing capacity', () => {
    const entries = new Map<string, number>([
      ['expired', 0],
      ['oldest-live', 7],
      ['newest-live', 9],
    ])

    pruneTimeBoundLru(entries, 10, { maxEntries: 1, ttlMs: 10 })

    expect(Array.from(entries.keys())).toEqual(['newest-live'])
  })

  it('clears all entries when maxEntries is zero', () => {
    const entries = new Map<string, number>([
      ['a', 1],
      ['b', 2],
    ])

    pruneTimeBoundLru(entries, 3, { maxEntries: 0, ttlMs: 10 })

    expect(entries.size).toBe(0)
  })
})
