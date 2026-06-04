import { describe, it, expect } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useArtifacts } from '../useArtifacts'

// The Plans-panel revision history (PlansTab, 1a.3) renders a plan artifact's
// `versions`. On a reject -> revise cycle the backend re-broadcasts the revised
// plan; useChatPageArtifacts.onPlanReady routes that to updateArtifact, which
// must append a new revision to the existing plan artifact.
describe('useArtifacts plan revision history', () => {
  it('appends a new revision entry when a plan artifact is updated with revised content', () => {
    const { result } = renderHook(() => useArtifacts())

    let planId = ''
    act(() => {
      planId = result.current.createArtifact('text', '# Plan A', 'markdown', 'Plan', {
        isPlan: true,
      })
    })

    const first = result.current.artifacts.get(planId)
    expect(first?.isPlan).toBe(true)
    expect(first?.versions).toHaveLength(1)

    act(() => {
      result.current.updateArtifact(planId, '# Plan B (revised)')
    })

    const revised = result.current.artifacts.get(planId)
    expect(revised?.versions).toHaveLength(2)
    expect(revised?.versions[0].content).toBe('# Plan A')
    expect(revised?.versions[1].content).toBe('# Plan B (revised)')
    // The newest revision becomes current (what the history highlights).
    expect(revised?.currentVersionIndex).toBe(1)
  })
})
