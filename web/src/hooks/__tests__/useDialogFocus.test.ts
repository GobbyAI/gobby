import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useDialogFocus } from '../useDialogFocus'

describe('useDialogFocus', () => {
  let rafCallbacks: Array<FrameRequestCallback | null>

  beforeEach(() => {
    rafCallbacks = []
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
      rafCallbacks.push(callback)
      return rafCallbacks.length
    })
    vi.stubGlobal('cancelAnimationFrame', (id: number) => {
      rafCallbacks[id - 1] = null
    })
  })

  afterEach(() => {
    document.body.replaceChildren()
    vi.unstubAllGlobals()
  })

  it('keeps focus on the restore target when closed before initial focus runs', () => {
    const restoreTarget = document.createElement('button')
    const dialog = document.createElement('div')
    const initialTarget = document.createElement('button')
    initialTarget.autofocus = true
    dialog.append(initialTarget)
    document.body.append(restoreTarget, dialog)
    restoreTarget.focus()
    const ref = { current: dialog }

    const { rerender } = renderHook(
      ({ isOpen }: { isOpen: boolean }) =>
        useDialogFocus({ ref, isOpen, onClose: vi.fn() }),
      { initialProps: { isOpen: true } },
    )

    rerender({ isOpen: false })
    expect(document.activeElement).toBe(restoreTarget)

    act(() => {
      rafCallbacks.forEach((callback) => callback?.(0))
    })

    expect(document.activeElement).toBe(restoreTarget)
  })
})
