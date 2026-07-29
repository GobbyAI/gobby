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

  it('never restores Terminal as the active panel tab (it lives in the dock)', () => {
    localStorage.setItem(TAB_KEY, 'terminal')

    const { result } = renderHook(() => useActivityPanel(false))

    expect(result.current.activeTab).toBe('sessions')
  })

  it('opens, expands, and closes the terminal dock', () => {
    const { result } = renderHook(() => useActivityPanel(false))

    expect(result.current.terminalOpen).toBe(false)
    expect(result.current.terminalExpanded).toBe(false)

    act(() => result.current.openTerminal())
    expect(result.current.terminalOpen).toBe(true)
    expect(localStorage.getItem('gobby-terminal-dock-open')).toBe('true')

    act(() => result.current.toggleTerminalExpanded())
    expect(result.current.terminalExpanded).toBe(true)

    act(() => result.current.closeTerminal())
    expect(result.current.terminalOpen).toBe(false)
    expect(result.current.terminalExpanded).toBe(false)
    expect(localStorage.getItem('gobby-terminal-dock-open')).toBe('false')
  })

  it('opens the terminal dock without running a panel-leave guard on desktop', () => {
    const confirmLeave = vi.fn(async () => false)
    const { result } = renderHook(() => useActivityPanel(false))
    const unregister = result.current.dirtyGuard.registerDirtyGuard({
      isDirty: () => true,
      confirmLeave,
    })

    act(() => result.current.openTerminal())

    expect(result.current.terminalOpen).toBe(true)
    expect(confirmLeave).not.toHaveBeenCalled()
    unregister()
  })

  it('restores the persisted dock-open preference', () => {
    localStorage.setItem('gobby-terminal-dock-open', 'true')

    const { result } = renderHook(() => useActivityPanel(false))

    expect(result.current.terminalOpen).toBe(true)
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

  it('opens the terminal dock on the gobby:show-activity-tab event and ignores unknown tabs', () => {
    const { result } = renderHook(() => useActivityPanel(false))
    const initialTab = result.current.activeTab

    act(() => {
      window.dispatchEvent(
        new CustomEvent('gobby:show-activity-tab', { detail: { tab: 'terminal' } }),
      )
    })
    expect(result.current.terminalOpen).toBe(true)
    expect(result.current.activeTab).toBe(initialTab)

    act(() => {
      window.dispatchEvent(
        new CustomEvent('gobby:show-activity-tab', { detail: { tab: 'bogus' } }),
      )
    })
    expect(result.current.activeTab).toBe(initialTab)
  })

  it('stores terminal session request', () => {
    const { result } = renderHook(() => useActivityPanel(false))

    act(() => {
      window.dispatchEvent(
        new CustomEvent('gobby:show-activity-tab', {
          detail: { tab: 'terminal', sessionId: 'session-focus' },
        }),
      )
    })
    expect(result.current.terminalOpen).toBe(true)
    expect(result.current.terminalSessionRequest).toBe('session-focus')

    act(() => result.current.clearTerminalSessionRequest())
    expect(result.current.terminalSessionRequest).toBeNull()

    act(() => {
      window.dispatchEvent(
        new CustomEvent('gobby:show-activity-tab', {
          detail: { tab: 'sessions', sessionId: 'wrong-tab' },
        }),
      )
    })
    expect(result.current.terminalSessionRequest).toBeNull()
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

  it('opens the terminal while guarding the mobile panel-to-chat transition', async () => {
    const confirmLeave = vi.fn(async () => false)
    const { result } = renderHook(() => useActivityPanel(true))

    await act(async () => result.current.toggleFromChat())
    expect(result.current.effectiveMode).toBe('panel')
    const unregister = result.current.dirtyGuard.registerDirtyGuard({
      isDirty: () => true,
      confirmLeave,
    })

    await act(async () => {
      result.current.openTerminal()
      await Promise.resolve()
    })

    expect(result.current.terminalOpen).toBe(true)
    expect(result.current.effectiveMode).toBe('panel')
    expect(confirmLeave).toHaveBeenCalledTimes(1)
    unregister()
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

      // Terminal is dock-only content: it stays in the tab registry for the
      // dropdown but never restores as the panel's active tab.
      expect(result.current.activeTab).toBe(id === 'terminal' ? 'sessions' : id)
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
