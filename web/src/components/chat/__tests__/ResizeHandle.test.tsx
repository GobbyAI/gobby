import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { ResizeHandle } from '../artifacts/ResizeHandle'

describe('ResizeHandle', () => {
  it('expands a right-anchored horizontal pane when dragged left', () => {
    const onResize = vi.fn()
    render(<ResizeHandle onResize={onResize} panelWidth={400} />)

    const handle = screen.getByRole('separator')
    fireEvent.mouseDown(handle, { clientX: 200 })
    fireEvent.mouseMove(document, { clientX: 170 })
    fireEvent.mouseUp(document)

    expect(onResize).toHaveBeenLastCalledWith(430)
  })

  it('expands a left-anchored horizontal pane when dragged right', () => {
    const onResize = vi.fn()
    render(
      <ResizeHandle
        onResize={onResize}
        panelWidth={400}
        horizontalAnchor="left"
      />,
    )

    const handle = screen.getByRole('separator')
    fireEvent.mouseDown(handle, { clientX: 200 })
    fireEvent.mouseMove(document, { clientX: 230 })
    fireEvent.mouseUp(document)

    expect(onResize).toHaveBeenLastCalledWith(430)
  })

  it('uses left-arrow to expand a right-anchored horizontal pane', () => {
    const onResize = vi.fn()
    render(<ResizeHandle onResize={onResize} panelWidth={400} />)

    fireEvent.keyDown(screen.getByRole('separator'), { key: 'ArrowLeft' })

    expect(onResize).toHaveBeenCalledWith(410)
  })

  it('uses right-arrow to expand a left-anchored horizontal pane', () => {
    const onResize = vi.fn()
    render(
      <ResizeHandle
        onResize={onResize}
        panelWidth={400}
        horizontalAnchor="left"
      />,
    )

    fireEvent.keyDown(screen.getByRole('separator'), { key: 'ArrowRight' })

    expect(onResize).toHaveBeenCalledWith(410)
  })
})
