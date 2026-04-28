import { act, renderHook } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'

import { useActivityPanel } from '../useActivityPanel'

const LEGACY_TAB_KEY = 'gobby-activity-panel-tab'
const TAB_KEY = 'gobby-activity-panel-tab-v2'

describe('useActivityPanel', () => {
  beforeEach(() => {
    localStorage.clear()
    Object.defineProperty(window, 'innerWidth', {
      configurable: true,
      value: 1200,
    })
  })

  it('migrates the legacy artifacts tab to changes', () => {
    localStorage.setItem(LEGACY_TAB_KEY, 'artifacts')

    const { result } = renderHook(() => useActivityPanel())

    expect(result.current.activeTab).toBe('changes')
    expect(localStorage.getItem(LEGACY_TAB_KEY)).toBeNull()
  })

  it('keeps the new artifacts tab as generated artifacts', () => {
    localStorage.setItem(TAB_KEY, 'artifacts')

    const { result } = renderHook(() => useActivityPanel())

    expect(result.current.activeTab).toBe('artifacts')
  })

  it('persists the selected tab under the versioned storage key', () => {
    const { result } = renderHook(() => useActivityPanel())

    act(() => {
      result.current.setActiveTab('changes')
    })

    expect(result.current.activeTab).toBe('changes')
    expect(localStorage.getItem(TAB_KEY)).toBe('changes')
  })
})
