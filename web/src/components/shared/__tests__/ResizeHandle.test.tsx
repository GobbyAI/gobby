import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { ResizeHandle } from '../ResizeHandle'

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

  it('resizes vertically in both directions and clamps using the drag-start height', () => {
    const onResize = vi.fn()
    const { container } = render(
      <div>
        <ResizeHandle
          direction="vertical"
          onResize={onResize}
          panelHeight={50}
          minHeight={20}
          maxHeight={80}
        />
      </div>,
    )
    const parent = container.firstElementChild as HTMLElement
    const rect = vi.spyOn(parent, 'getBoundingClientRect').mockReturnValue({
      width: 800,
      height: 200,
      top: 0,
      right: 800,
      bottom: 200,
      left: 0,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    })
    const handle = screen.getByRole('separator')

    fireEvent.mouseDown(handle, { clientY: 100 })
    fireEvent.mouseMove(document, { clientY: 140 })
    fireEvent.mouseMove(document, { clientY: 60 })
    fireEvent.mouseMove(document, { clientY: 300 })
    fireEvent.mouseMove(document, { clientY: -100 })
    fireEvent.mouseUp(document)

    expect(onResize.mock.calls.map(([value]) => value)).toEqual([70, 30, 80, 20])
    expect(rect).toHaveBeenCalledTimes(1)
  })

  it('resizes vertically by keyboard in both directions and clamps boundaries', () => {
    const onResize = vi.fn()
    const { rerender } = render(
      <ResizeHandle
        direction="vertical"
        onResize={onResize}
        panelHeight={79}
        minHeight={20}
        maxHeight={80}
      />,
    )

    fireEvent.keyDown(screen.getByRole('separator'), { key: 'ArrowDown' })
    fireEvent.keyDown(screen.getByRole('separator'), { key: 'ArrowUp' })
    expect(onResize).toHaveBeenNthCalledWith(1, 80)
    expect(onResize).toHaveBeenNthCalledWith(2, 77)

    rerender(
      <ResizeHandle
        direction="vertical"
        onResize={onResize}
        panelHeight={21}
        minHeight={20}
        maxHeight={80}
      />,
    )
    fireEvent.keyDown(screen.getByRole('separator'), { key: 'ArrowUp' })
    expect(onResize).toHaveBeenLastCalledWith(20)
  })
})
