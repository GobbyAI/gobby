import { act, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ACTIVITY_PANEL_TABS } from '../../activity/ActivityPanelTabs'
import { RESTART_TIMEOUT_MS } from '../../../lib/api'
import { useAppCommandPalette } from '../useAppCommandPalette'

function makeHookArgs(addSystemMessage = vi.fn()) {
  return {
    startNewChat: vi.fn(),
    clearHistory: vi.fn(),
    sendMessage: vi.fn(() => true),
    settings: {
      model: 'claude-sonnet',
      chatMode: 'normal' as const,
      ttsEnabled: false,
    },
    effectiveProjectId: 'project-1',
    currentMainReasoning: null,
    updateChatMode: vi.fn(),
    sendMode: vi.fn(),
    addSystemMessage,
    setActiveModal: vi.fn(),
    setSettingsOpen: vi.fn(),
    setResumeModalOpen: vi.fn(),
    showPlanRef: { current: vi.fn() },
    openActivityTab: vi.fn(),
  }
}

describe('useAppCommandPalette', () => {
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('treats a restart request abort as an accepted daemon restart', async () => {
    vi.useFakeTimers()
    const addSystemMessage = vi.fn()
    let capturedSignal: AbortSignal | undefined
    const fetchMock = vi.fn((_url: string | URL | Request, init?: RequestInit) => {
      capturedSignal = init?.signal ?? undefined
      return new Promise<Response>((_resolve, reject) => {
        capturedSignal?.addEventListener(
          'abort',
          () => reject(new DOMException('aborted', 'AbortError')),
          { once: true },
        )
      })
    })
    vi.stubGlobal('fetch', fetchMock)

    const { result } = renderHook(() => useAppCommandPalette(makeHookArgs(addSystemMessage)))
    const restartAction = result.current.commandPaletteActions.find((action) => action.id === 'restart')

    act(() => {
      restartAction?.onSelect()
    })

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/admin/restart',
      expect.objectContaining({
        credentials: 'include',
        method: 'POST',
        signal: expect.any(AbortSignal),
      }),
    )
    expect(capturedSignal).toBeInstanceOf(AbortSignal)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(RESTART_TIMEOUT_MS)
      await Promise.resolve()
    })

    expect(addSystemMessage).toHaveBeenCalledTimes(2)
    expect(addSystemMessage).toHaveBeenCalledWith('Requesting daemon restart...')
    expect(addSystemMessage).toHaveBeenCalledWith('Daemon restart requested; reconnecting...')
    expect(addSystemMessage).not.toHaveBeenCalledWith('Failed to restart daemon')
  })

  it('routes legacy MCP browse commands to the MCP activity tab', () => {
    const args = makeHookArgs()
    const { result } = renderHook(() => useAppCommandPalette(args))

    act(() => {
      result.current.handlePaletteSelect({
        kind: 'command',
        name: 'mcp',
        description: 'Open MCP activity',
        action: 'open_mcp',
      })
    })

    expect(args.openActivityTab).toHaveBeenCalledWith('mcp')
    expect(args.setActiveModal).not.toHaveBeenCalledWith('mcp')
  })

  it('derives activity navigation actions from the activity tab registry', () => {
    const args = makeHookArgs()
    const { result } = renderHook(() => useAppCommandPalette(args))
    const navigationActions = result.current.commandPaletteActions.filter(
      (action) => action.category === 'navigate',
    )

    expect(navigationActions.map(({ id, label }) => ({ id, label }))).toEqual(
      ACTIVITY_PANEL_TABS.map(({ id, label }) => ({ id: `nav-${id}`, label })),
    )

    for (const tab of ACTIVITY_PANEL_TABS) {
      const action = navigationActions.find(({ id }) => id === `nav-${tab.id}`)

      act(() => action?.onSelect())
      expect(args.openActivityTab).toHaveBeenLastCalledWith(tab.id)
    }
  })
})
