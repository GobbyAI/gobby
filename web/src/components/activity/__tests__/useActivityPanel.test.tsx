import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  loadLayoutMode,
  reduceToggleFromChat,
  reduceToggleFromPanel,
  useActivityPanel,
} from '../useActivityPanel'
import { ACTIVITY_PANEL_TABS } from '../ActivityPanelTabs'

const TAB_KEY = 'gobby-activity-panel-tab-v2'
const LAYOUT_KEY = 'gobby-activity-panel-layout'

afterEach(() => {
  vi.restoreAllMocks()
})

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

describe('loadLayoutMode', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it('honors a stored layout mode', () => {
    localStorage.setItem(LAYOUT_KEY, 'panel')

    expect(loadLayoutMode()).toBe('panel')
  })

  it('defaults new users to split', () => {
    expect(loadLayoutMode()).toBe('split')
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
    expect(result.current.activeTab).toBe('sessions')
  })

  it('restores Terminal from the registry-backed stored tab value', () => {
    localStorage.setItem(TAB_KEY, 'terminal')

    const { result } = renderHook(() => useActivityPanel(false))

    expect(result.current.activeTab).toBe('terminal')
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

  it('supports a transient full-width override without persisting layout', () => {
    localStorage.setItem(LAYOUT_KEY, 'split')
    const { result } = renderHook(() => useActivityPanel(false))

    expect(result.current.viewOverride).toBeNull()

    act(() => result.current.requestPanelOverride())
    expect(result.current.viewOverride).toBe('panel')
    expect(result.current.mode).toBe('split')
    expect(result.current.effectiveMode).toBe('panel')
    expect(localStorage.getItem(LAYOUT_KEY)).toBe('split')

    act(() => result.current.releasePanelOverride())
    expect(result.current.viewOverride).toBeNull()
    expect(result.current.effectiveMode).toBe('split')
    expect(localStorage.getItem(LAYOUT_KEY)).toBe('split')
  })

  it('clears the transient override when the activity tab changes', () => {
    const { result } = renderHook(() => useActivityPanel(false))

    act(() => result.current.requestPanelOverride())
    expect(result.current.effectiveMode).toBe('panel')

    act(() => result.current.setActiveTab('tasks'))
    expect(result.current.viewOverride).toBeNull()
    expect(result.current.activeTab).toBe('tasks')
    expect(result.current.effectiveMode).toBe('split')
  })

  it('clears the transient override when showTab selects another activity', () => {
    const { result } = renderHook(() => useActivityPanel(false))

    act(() => result.current.requestPanelOverride())
    expect(result.current.effectiveMode).toBe('panel')

    act(() => result.current.showTab('rules'))
    expect(result.current.viewOverride).toBeNull()
    expect(result.current.activeTab).toBe('rules')
    expect(result.current.effectiveMode).toBe('split')
  })

  it('switches to Terminal on the gobby:show-activity-tab event and ignores unknown tabs', () => {
    const { result } = renderHook(() => useActivityPanel(false))

    act(() => {
      window.dispatchEvent(
        new CustomEvent('gobby:show-activity-tab', { detail: { tab: 'terminal' } }),
      )
    })
    expect(result.current.activeTab).toBe('terminal')

    act(() => {
      window.dispatchEvent(
        new CustomEvent('gobby:show-activity-tab', { detail: { tab: 'bogus' } }),
      )
    })
    expect(result.current.activeTab).toBe('terminal')
  })

  it('clears the transient override when the user explicitly toggles layout', () => {
    const { result } = renderHook(() => useActivityPanel(false))

    act(() => result.current.requestPanelOverride())
    expect(result.current.effectiveMode).toBe('panel')

    act(() => result.current.toggleFromChat())
    expect(result.current.viewOverride).toBeNull()
    expect(result.current.effectiveMode).toBe('chat')
    expect(localStorage.getItem(LAYOUT_KEY)).toBe('chat')
  })

  it('clears the transient override when the panel toggle changes layout', () => {
    localStorage.setItem(LAYOUT_KEY, 'chat')
    const { result } = renderHook(() => useActivityPanel(false))

    act(() => result.current.requestPanelOverride())
    expect(result.current.mode).toBe('chat')
    expect(result.current.effectiveMode).toBe('panel')

    act(() => result.current.toggleFromPanel())
    expect(result.current.viewOverride).toBeNull()
    expect(result.current.effectiveMode).toBe('split')
    expect(localStorage.getItem(LAYOUT_KEY)).toBe('split')
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

  it('does not write the desktop layout key on initial mobile render', () => {
    const { result } = renderHook(() => useActivityPanel(true))

    expect(result.current.effectiveMode).toBe('chat')
    expect(localStorage.getItem(LAYOUT_KEY)).toBeNull()
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

  it('handles rapid mobile toggles without touching the desktop preference', () => {
    localStorage.setItem(LAYOUT_KEY, 'split')
    const { result } = renderHook(() => useActivityPanel(true))

    act(() => {
      result.current.toggleFromChat()
      result.current.toggleFromChat()
      result.current.toggleFromChat()
    })

    expect(result.current.effectiveMode).toBe('panel')
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

  it('ignores the transient full-width override on mobile', () => {
    localStorage.setItem(LAYOUT_KEY, 'split')
    const { result } = renderHook(() => useActivityPanel(true))

    act(() => result.current.requestPanelOverride())

    expect(result.current.viewOverride).toBeNull()
    expect(result.current.effectiveMode).toBe('chat')
    expect(localStorage.getItem(LAYOUT_KEY)).toBe('split')
  })
})

describe('useActivityPanel — tab persistence', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it('restores every registered activity tab from the versioned storage key', () => {
    for (const { id } of ACTIVITY_PANEL_TABS) {
      localStorage.setItem(TAB_KEY, id)

      const { result, unmount } = renderHook(() => useActivityPanel(false))

      expect(result.current.activeTab).toBe(id)
      unmount()
    }
  })

  it('keeps the MCP tab as a persisted activity tab', () => {
    localStorage.setItem(TAB_KEY, 'mcp')

    const { result } = renderHook(() => useActivityPanel(false))

    expect(result.current.activeTab).toBe('mcp')
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
