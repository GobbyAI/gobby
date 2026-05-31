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
})
