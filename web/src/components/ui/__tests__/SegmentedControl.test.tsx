import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { SegmentedControl } from '../SegmentedControl'

const OPTIONS = [
  { value: 'a', label: 'A' },
  { value: 'b', label: 'B' },
  { value: 'c', label: 'C' },
] as const

type OptionValue = (typeof OPTIONS)[number]['value']

function renderControl(
  override: Partial<Parameters<typeof SegmentedControl<OptionValue>>[0]> = {},
) {
  const onChange = vi.fn()
  const utils = render(
    <SegmentedControl<OptionValue>
      value="a"
      onChange={onChange}
      options={OPTIONS}
      ariaLabel="Letter"
      {...override}
    />,
  )
  return { onChange, ...utils }
}

describe('SegmentedControl', () => {
  it('renders all options with one aria-checked', () => {
    renderControl()
    const radios = screen.getAllByRole('radio')
    expect(radios).toHaveLength(3)
    expect(radios.map((r) => r.getAttribute('aria-checked'))).toEqual([
      'true',
      'false',
      'false',
    ])
  })

  it('fires onChange when an inactive option is clicked', () => {
    const { onChange } = renderControl()
    fireEvent.click(screen.getByRole('radio', { name: 'B' }))
    expect(onChange).toHaveBeenCalledWith('b')
  })

  it('fires per-option onClick in addition to onChange', () => {
    const onChange = vi.fn()
    const sideEffect = vi.fn()
    render(
      <SegmentedControl<OptionValue>
        value="a"
        onChange={onChange}
        options={[
          { value: 'a', label: 'A' },
          { value: 'b', label: 'B', onClick: sideEffect },
        ]}
        ariaLabel="Letter"
      />,
    )
    fireEvent.click(screen.getByRole('radio', { name: 'B' }))
    expect(onChange).toHaveBeenCalledWith('b')
    expect(sideEffect).toHaveBeenCalledTimes(1)
  })

  it('ArrowRight wraps from last to first', () => {
    const { onChange } = renderControl({ value: 'c' })
    const last = screen.getByRole('radio', { name: 'C' })
    fireEvent.keyDown(last, { key: 'ArrowRight' })
    expect(onChange).toHaveBeenCalledWith('a')
  })

  it('ArrowLeft wraps from first to last', () => {
    const { onChange } = renderControl({ value: 'a' })
    const first = screen.getByRole('radio', { name: 'A' })
    fireEvent.keyDown(first, { key: 'ArrowLeft' })
    expect(onChange).toHaveBeenCalledWith('c')
  })

  it('Home jumps to first, End jumps to last', () => {
    const { onChange } = renderControl({ value: 'b' })
    const middle = screen.getByRole('radio', { name: 'B' })
    fireEvent.keyDown(middle, { key: 'Home' })
    expect(onChange).toHaveBeenLastCalledWith('a')
    fireEvent.keyDown(middle, { key: 'End' })
    expect(onChange).toHaveBeenLastCalledWith('c')
  })

  it('disabled state suppresses click and key handling', () => {
    const { onChange } = renderControl({ disabled: true })
    fireEvent.click(screen.getByRole('radio', { name: 'B' }))
    fireEvent.keyDown(screen.getByRole('radio', { name: 'A' }), { key: 'ArrowRight' })
    expect(onChange).not.toHaveBeenCalled()
  })

  it('active option carries the subdued accent classes (regression guard)', () => {
    renderControl({ value: 'b' })
    const active = screen.getByRole('radio', { name: 'B' })
    expect(active.className).toContain('bg-accent/15')
    expect(active.className).toContain('text-accent')
    expect(active.className).not.toContain('bg-accent text-accent-foreground')
  })
})
