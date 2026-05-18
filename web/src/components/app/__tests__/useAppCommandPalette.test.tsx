import { act, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

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
      postPlanChatMode: 'normal' as const,
      ttsEnabled: false,
    },
    effectiveProjectId: 'project-1',
    currentMainReasoning: null,
    updateChatMode: vi.fn(),
    sendMode: vi.fn(),
    addSystemMessage,
    setActiveTab: vi.fn(),
    setActiveModal: vi.fn(),
    setSettingsOpen: vi.fn(),
    setResumeModalOpen: vi.fn(),
    showPlanRef: { current: vi.fn() },
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
})
