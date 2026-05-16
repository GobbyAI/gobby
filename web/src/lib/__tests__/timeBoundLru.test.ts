import { describe, expect, it } from 'vitest'
import { pruneTimeBoundLru } from '../timeBoundLru'

describe('pruneTimeBoundLru', () => {
  it('leaves an empty map unchanged', () => {
    const entries = new Map<string, number>()

    pruneTimeBoundLru(entries, 10, { maxEntries: 3, ttlMs: 10 })

    expect(entries.size).toBe(0)
  })

  it('keeps all under-capacity entries that are still within ttl', () => {
    const entries = new Map<string, number>([
      ['a', 7],
      ['b', 8],
    ])

    pruneTimeBoundLru(entries, 10, { maxEntries: 3, ttlMs: 10 })

    expect(Array.from(entries.keys())).toEqual(['a', 'b'])
  })

  it('removes a single expired entry', () => {
    const entries = new Map<string, number>([['expired', 0]])

    pruneTimeBoundLru(entries, 10, { maxEntries: 3, ttlMs: 10 })

    expect(entries.size).toBe(0)
  })

  it('keeps a single retained entry', () => {
    const entries = new Map<string, number>([['retained', 1]])

    pruneTimeBoundLru(entries, 10, { maxEntries: 3, ttlMs: 10 })

    expect(Array.from(entries.keys())).toEqual(['retained'])
  })

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

  it('aborts pruning when the clock moves behind any last-seen timestamp', () => {
    const entries = new Map<string, number>([
      ['expired', 0],
      ['future', 20],
    ])

    pruneTimeBoundLru(entries, 10, { maxEntries: 1, ttlMs: 5 })

    expect(Array.from(entries.entries())).toEqual([
      ['expired', 0],
      ['future', 20],
    ])
  })
})
