import { act, renderHook } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'

import {
  loadLayoutMode,
  reduceToggleFromChat,
  reduceToggleFromPanel,
  useActivityPanel,
} from '../useActivityPanel'

const LEGACY_TAB_KEY = 'gobby-activity-panel-tab'
const TAB_KEY = 'gobby-activity-panel-tab-v2'
const LAYOUT_KEY = 'gobby-activity-panel-layout'
const LEGACY_PINNED_KEY = 'gobby-activity-panel-pinned'

describe('layout reducers', () => {
  it('reduceToggleFromChat is decision-complete for every mode', () => {
    expect(reduceToggleFromChat('chat')).toBe('split')
    expect(reduceToggleFromChat('split')).toBe('chat')
    expect(reduceToggleFromChat('panel')).toBe('split')
  })

  it('reduceToggleFromPanel is decision-complete for every mode', () => {
    expect(reduceToggleFromPanel('split')).toBe('panel')
    expect(reduceToggleFromPanel('panel')).toBe('split')
    expect(reduceToggleFromPanel('chat')).toBe('split')
  })

  it('never reaches a "both panes collapsed" state from any single toggle', () => {
    for (const mode of ['chat', 'split', 'panel'] as const) {
      expect(['chat', 'split', 'panel']).toContain(reduceToggleFromChat(mode))
      expect(['chat', 'split', 'panel']).toContain(reduceToggleFromPanel(mode))
    }
  })
})

describe('loadLayoutMode migration', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it('migrates the legacy pinned=true boolean to split and rewrites the key', () => {
    localStorage.setItem(LEGACY_PINNED_KEY, 'true')

    expect(loadLayoutMode()).toBe('split')
    expect(localStorage.getItem(LAYOUT_KEY)).toBe('split')
    expect(localStorage.getItem(LEGACY_PINNED_KEY)).toBeNull()
  })

  it('migrates the legacy pinned=false boolean to chat', () => {
    localStorage.setItem(LEGACY_PINNED_KEY, 'false')

    expect(loadLayoutMode()).toBe('chat')
    expect(localStorage.getItem(LAYOUT_KEY)).toBe('chat')
    expect(localStorage.getItem(LEGACY_PINNED_KEY)).toBeNull()
  })

  it('honors a stored layout mode over the legacy key', () => {
    localStorage.setItem(LAYOUT_KEY, 'panel')
    localStorage.setItem(LEGACY_PINNED_KEY, 'false')

    expect(loadLayoutMode()).toBe('panel')
  })

  it('defaults new users to split', () => {
    expect(loadLayoutMode()).toBe('split')
  })

  it('is idempotent: a second read returns the stored value with no legacy key', () => {
    localStorage.setItem(LEGACY_PINNED_KEY, 'true')
    loadLayoutMode()
    expect(loadLayoutMode()).toBe('split')
    expect(localStorage.getItem(LEGACY_PINNED_KEY)).toBeNull()
  })
})

describe('useActivityPanel — desktop', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it('starts in split and exposes effectiveMode = mode', () => {
    const { result } = renderHook(() => useActivityPanel(false))

    expect(result.current.mode).toBe('split')
    expect(result.current.effectiveMode).toBe('split')
  })

  it('toggleFromChat walks split -> chat -> split and persists', () => {
    const { result } = renderHook(() => useActivityPanel(false))

    act(() => result.current.toggleFromChat())
    expect(result.current.effectiveMode).toBe('chat')
    expect(localStorage.getItem(LAYOUT_KEY)).toBe('chat')

    act(() => result.current.toggleFromChat())
    expect(result.current.effectiveMode).toBe('split')
    expect(localStorage.getItem(LAYOUT_KEY)).toBe('split')
  })

  it('toggleFromPanel walks split -> panel -> split', () => {
    const { result } = renderHook(() => useActivityPanel(false))

    act(() => result.current.toggleFromPanel())
    expect(result.current.effectiveMode).toBe('panel')
    expect(localStorage.getItem(LAYOUT_KEY)).toBe('panel')

    act(() => result.current.toggleFromPanel())
    expect(result.current.effectiveMode).toBe('split')
  })

  it('toggleFromChat out of panel returns to split (chat rejoins the panel)', () => {
    localStorage.setItem(LAYOUT_KEY, 'panel')
    const { result } = renderHook(() => useActivityPanel(false))

    expect(result.current.mode).toBe('panel')
    act(() => result.current.toggleFromChat())
    expect(result.current.effectiveMode).toBe('split')
  })

  it('showTab auto-opens chat -> split and closeIfAutoOpened reverts', () => {
    localStorage.setItem(LAYOUT_KEY, 'chat')
    const { result } = renderHook(() => useActivityPanel(false))

    act(() => result.current.showTab('tasks'))
    expect(result.current.activeTab).toBe('tasks')
    expect(result.current.effectiveMode).toBe('split')

    act(() => result.current.closeIfAutoOpened())
    expect(result.current.effectiveMode).toBe('chat')
  })

  it('closeIfAutoOpened is a no-op when the panel was not auto-opened', () => {
    const { result } = renderHook(() => useActivityPanel(false))

    expect(result.current.effectiveMode).toBe('split')
    act(() => result.current.closeIfAutoOpened())
    expect(result.current.effectiveMode).toBe('split')
  })

  it('dismissOnMobile is a no-op on desktop', () => {
    const { result } = renderHook(() => useActivityPanel(false))

    act(() => result.current.dismissOnMobile())
    expect(result.current.effectiveMode).toBe('split')
  })
})

describe('useActivityPanel — mobile', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it('starts in chat on an initial mobile render regardless of desktop panel preference', () => {
    localStorage.setItem(LAYOUT_KEY, 'panel')
    const { result } = renderHook(() => useActivityPanel(true))

    expect(result.current.effectiveMode).toBe('chat')
  })

  it('split desktop preference collapses to chat on mobile', () => {
    localStorage.setItem(LAYOUT_KEY, 'split')
    const { result } = renderHook(() => useActivityPanel(true))

    expect(result.current.effectiveMode).toBe('chat')
  })

  it('toggleFromChat flips the mobile binary without writing the desktop key', () => {
    localStorage.setItem(LAYOUT_KEY, 'split')
    const { result } = renderHook(() => useActivityPanel(true))

    act(() => result.current.toggleFromChat())
    expect(result.current.effectiveMode).toBe('panel')
    // Desktop preference is untouched by mobile interaction.
    expect(localStorage.getItem(LAYOUT_KEY)).toBe('split')

    act(() => result.current.toggleFromChat())
    expect(result.current.effectiveMode).toBe('chat')
    expect(localStorage.getItem(LAYOUT_KEY)).toBe('split')
  })

  it('dismissOnMobile returns the mobile view to chat', () => {
    localStorage.setItem(LAYOUT_KEY, 'panel')
    const { result } = renderHook(() => useActivityPanel(true))

    act(() => result.current.toggleFromChat())
    expect(result.current.effectiveMode).toBe('panel')
    act(() => result.current.dismissOnMobile())
    expect(result.current.effectiveMode).toBe('chat')
  })

  it('crossing desktop -> mobile clamps to the derived view and leaves the desktop key intact', () => {
    localStorage.setItem(LAYOUT_KEY, 'panel')
    const { result, rerender } = renderHook(
      ({ isMobile }: { isMobile: boolean }) => useActivityPanel(isMobile),
      { initialProps: { isMobile: false } },
    )

    expect(result.current.effectiveMode).toBe('panel')

    rerender({ isMobile: true })
    expect(result.current.effectiveMode).toBe('panel')
    expect(localStorage.getItem(LAYOUT_KEY)).toBe('panel')
  })

  it('crossing mobile -> desktop restores the persisted desktop mode verbatim', () => {
    localStorage.setItem(LAYOUT_KEY, 'split')
    const { result, rerender } = renderHook(
      ({ isMobile }: { isMobile: boolean }) => useActivityPanel(isMobile),
      { initialProps: { isMobile: true } },
    )

    // On mobile, split collapses to chat; toggling shows the panel overlay.
    expect(result.current.effectiveMode).toBe('chat')
    act(() => result.current.toggleFromChat())
    expect(result.current.effectiveMode).toBe('panel')

    rerender({ isMobile: false })
    expect(result.current.effectiveMode).toBe('split')
    expect(result.current.mode).toBe('split')
  })
})

describe('useActivityPanel — tab persistence', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it('migrates the legacy artifacts tab to changes', () => {
    localStorage.setItem(LEGACY_TAB_KEY, 'artifacts')

    const { result } = renderHook(() => useActivityPanel(false))

    expect(result.current.activeTab).toBe('changes')
    expect(localStorage.getItem(LEGACY_TAB_KEY)).toBeNull()
  })

  it('keeps the new artifacts tab as generated artifacts', () => {
    localStorage.setItem(TAB_KEY, 'artifacts')

    const { result } = renderHook(() => useActivityPanel(false))

    expect(result.current.activeTab).toBe('artifacts')
  })

  it('persists the selected tab under the versioned storage key', () => {
    const { result } = renderHook(() => useActivityPanel(false))

    act(() => {
      result.current.setActiveTab('changes')
    })

    expect(result.current.activeTab).toBe('changes')
    expect(localStorage.getItem(TAB_KEY)).toBe('changes')
  })
})
