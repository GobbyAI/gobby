import { describe, expect, it } from 'vitest'

import { omitNullish } from '../omitNullish'

describe('omitNullish', () => {
  it('removes null and undefined values while preserving other falsy values', () => {
    expect(
      omitNullish({
        empty: '',
        falseValue: false,
        missing: undefined,
        nil: null,
        zero: 0,
      }),
    ).toEqual({
      empty: '',
      falseValue: false,
      zero: 0,
    })
  })

  it('returns an empty object unchanged', () => {
    expect(omitNullish({})).toEqual({})
  })

  it('returns an empty object when every value is nullish', () => {
    expect(
      omitNullish({
        missing: undefined,
        nil: null,
      }),
    ).toEqual({})
  })

  it('preserves truthy object, string, and number values', () => {
    const nested = { enabled: true }

    expect(
      omitNullish({
        count: 7,
        label: 'ready',
        nested,
      }),
    ).toEqual({
      count: 7,
      label: 'ready',
      nested,
    })
  })
})
