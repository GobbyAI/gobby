import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createRef } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { Textarea } from '../Textarea'

describe('Textarea', () => {
  it('renders a native textarea and forwards value changes', async () => {
    const onChange = vi.fn()
    render(<Textarea aria-label="Notes" value="" onChange={onChange} />)
    await userEvent.type(screen.getByRole('textbox', { name: 'Notes' }), 'a')
    expect(onChange).toHaveBeenCalled()
  })

  it('forwards its ref to the real HTMLTextAreaElement', () => {
    const ref = createRef<HTMLTextAreaElement>()
    render(<Textarea ref={ref} aria-label="Notes" />)
    expect(ref.current).toBeInstanceOf(HTMLTextAreaElement)
  })

  it('keeps ref-driven height changes across re-renders (auto-grow compatibility)', () => {
    const ref = createRef<HTMLTextAreaElement>()
    const { rerender } = render(
      <Textarea ref={ref} aria-label="Notes" value="one" onChange={() => {}} />,
    )
    ref.current!.style.height = '120px'
    rerender(<Textarea ref={ref} aria-label="Notes" value="one two" onChange={() => {}} />)
    expect(ref.current!.style.height).toBe('120px')
  })

  it('marks the control invalid and switches the border when error is set', () => {
    render(<Textarea aria-label="Notes" error />)
    const textarea = screen.getByRole('textbox', { name: 'Notes' })
    expect(textarea).toHaveAttribute('aria-invalid', 'true')
    expect(textarea.className).toContain('border-destructive')
  })

  it('passes aria-describedby through to the control', () => {
    render(<Textarea aria-label="Notes" aria-describedby="notes-hint" />)
    expect(screen.getByRole('textbox', { name: 'Notes' })).toHaveAttribute(
      'aria-describedby',
      'notes-hint',
    )
  })

  it('wraps the control in a label carrying the invisible coarse hit-area expansion', () => {
    render(<Textarea aria-label="Notes" />)
    const wrapper = screen.getByRole('textbox', { name: 'Notes' }).closest('label')
    expect(wrapper).not.toBeNull()
    expect(wrapper!.className).toContain('pointer-coarse:before:min-h-11')
  })

  it('lets a caller className win conflicting utilities via twMerge', () => {
    render(<Textarea aria-label="Notes" className="px-1" />)
    const textarea = screen.getByRole('textbox', { name: 'Notes' })
    expect(textarea.className).toContain('px-1')
    expect(textarea.className).not.toContain('px-3')
  })
})
