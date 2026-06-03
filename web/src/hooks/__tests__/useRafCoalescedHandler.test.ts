import { renderHook, act } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useRafCoalescedHandler } from '../useRafCoalescedHandler'

describe('useRafCoalescedHandler', () => {
  let rafCallbacks: Array<FrameRequestCallback | null>

  beforeEach(() => {
    rafCallbacks = []
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
      rafCallbacks.push(cb)
      return rafCallbacks.length
    })
    vi.stubGlobal('cancelAnimationFrame', (id: number) => {
      rafCallbacks[id - 1] = null
    })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  function flushFrame() {
    const callbacks = rafCallbacks
    rafCallbacks = []
    callbacks.forEach((cb) => cb?.(0))
  }

  it('invokes the handler once per frame with the latest value', () => {
    const handler = vi.fn()
    const { result } = renderHook(() => useRafCoalescedHandler<number>(handler))

    act(() => {
      result.current(1)
      result.current(2)
      result.current(3)
    })
    expect(handler).not.toHaveBeenCalled()

    act(() => flushFrame())
    expect(handler).toHaveBeenCalledTimes(1)
    expect(handler).toHaveBeenCalledWith(3)
  })

  it('uses the latest handler at flush time', () => {
    const first = vi.fn()
    const second = vi.fn()
    const { result, rerender } = renderHook(
      ({ h }: { h: (value: number) => void }) => useRafCoalescedHandler<number>(h),
      { initialProps: { h: first } },
    )

    act(() => {
      result.current(1)
    })
    rerender({ h: second })
    act(() => flushFrame())

    expect(first).not.toHaveBeenCalled()
    expect(second).toHaveBeenCalledWith(1)
  })

  it('schedules a fresh frame after the previous flush', () => {
    const handler = vi.fn()
    const { result } = renderHook(() => useRafCoalescedHandler<number>(handler))

    act(() => {
      result.current(1)
    })
    act(() => flushFrame())
    act(() => {
      result.current(2)
    })
    act(() => flushFrame())

    expect(handler).toHaveBeenCalledTimes(2)
    expect(handler).toHaveBeenNthCalledWith(1, 1)
    expect(handler).toHaveBeenNthCalledWith(2, 2)
  })

  it('cancels a pending frame on unmount', () => {
    const handler = vi.fn()
    const { result, unmount } = renderHook(() => useRafCoalescedHandler<number>(handler))

    act(() => {
      result.current(1)
    })
    unmount()
    act(() => flushFrame())

    expect(handler).not.toHaveBeenCalled()
  })
})
