import { act, renderHook } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { usePlans } from '../usePlans'

describe('usePlans revision history', () => {
  it('appends revised content and selects the newest revision', () => {
    const { result } = renderHook(() => usePlans())

    let planId = ''
    act(() => {
      planId = result.current.createPlan('# Plan A', 'Plan')
    })

    expect(result.current.plans.get(planId)?.versions).toHaveLength(1)

    act(() => {
      result.current.updatePlan(planId, '# Plan B (revised)')
    })

    const revised = result.current.plans.get(planId)
    expect(revised?.versions).toHaveLength(2)
    expect(revised?.versions[0].content).toBe('# Plan A')
    expect(revised?.versions[1].content).toBe('# Plan B (revised)')
    expect(revised?.currentVersionIndex).toBe(1)
  })
})
