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

  it('removes the oldest excess entries when enforcing capacity', () => {
    const entries = new Map<string, number>([
      ['newer', 40],
      ['oldest', 10],
      ['newest', 50],
      ['second-oldest', 20],
    ])

    pruneTimeBoundLru(entries, 60, { maxEntries: 2, ttlMs: 100 })

    expect(Array.from(entries.keys())).toEqual(['newer', 'newest'])
  })
})
