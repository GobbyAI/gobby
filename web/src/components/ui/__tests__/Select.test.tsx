import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createRef } from 'react'
import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest'
import { NativeSelect } from '../NativeSelect'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '../Select'

// Radix Select needs pointer-capture and scrollIntoView, which jsdom lacks.
const originalElementMethods = {
  hasPointerCapture: Element.prototype.hasPointerCapture,
  setPointerCapture: Element.prototype.setPointerCapture,
  releasePointerCapture: Element.prototype.releasePointerCapture,
  scrollIntoView: Element.prototype.scrollIntoView,
}

beforeAll(() => {
  Element.prototype.hasPointerCapture = () => false
  Element.prototype.setPointerCapture = () => undefined
  Element.prototype.releasePointerCapture = () => undefined
  Element.prototype.scrollIntoView = () => undefined
})

afterAll(() => {
  for (const [name, method] of Object.entries(originalElementMethods)) {
    if (typeof method === 'function') {
      Object.defineProperty(Element.prototype, name, {
        configurable: true,
        writable: true,
        value: method,
      })
    } else {
      Reflect.deleteProperty(Element.prototype, name)
    }
  }
})

function renderRadixSelect(onValueChange = vi.fn()) {
  render(
    <Select defaultValue="a" onValueChange={onValueChange}>
      <SelectTrigger aria-label="Pick one">
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value="a">Alpha</SelectItem>
        <SelectItem value="b">Beta</SelectItem>
      </SelectContent>
    </Select>,
  )
  return onValueChange
}

describe('Radix Select', () => {
  it('opens from the trigger and commits an item selection', async () => {
    const onValueChange = renderRadixSelect()
    await userEvent.click(screen.getByRole('combobox', { name: 'Pick one' }))
    await userEvent.click(await screen.findByRole('option', { name: 'Beta' }))
    expect(onValueChange).toHaveBeenCalledWith('b')
  })

  it('gives the trigger the invisible coarse hit-area expansion', () => {
    renderRadixSelect()
    const trigger = screen.getByRole('combobox', { name: 'Pick one' })
    expect(trigger.className).toContain('pointer-coarse:before:min-h-11')
    expect(trigger.className).toContain('pointer-coarse:before:min-w-11')
    // The visible box stays on the 36px ladder.
    expect(trigger.className).toContain('h-9')
  })

  it('gives every item the invisible coarse hit-area expansion', async () => {
    renderRadixSelect()
    await userEvent.click(screen.getByRole('combobox', { name: 'Pick one' }))
    for (const option of await screen.findAllByRole('option')) {
      expect(option.className).toContain('pointer-coarse:before:min-h-11')
    }
  })
})

describe('NativeSelect', () => {
  it('renders a native select and forwards value changes', async () => {
    const onChange = vi.fn()
    render(
      <NativeSelect aria-label="Pick one" value="a" onChange={onChange}>
        <option value="a">Alpha</option>
        <option value="b">Beta</option>
      </NativeSelect>,
    )
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Pick one' }), 'b')
    expect(onChange).toHaveBeenCalled()
  })

  it('forwards its ref to the real HTMLSelectElement', () => {
    const ref = createRef<HTMLSelectElement>()
    render(
      <NativeSelect ref={ref} aria-label="Pick one">
        <option value="a">Alpha</option>
      </NativeSelect>,
    )
    expect(ref.current).toBeInstanceOf(HTMLSelectElement)
  })

  it('marks the control invalid and switches the border when error is set', () => {
    render(
      <NativeSelect aria-label="Pick one" error>
        <option value="a">Alpha</option>
      </NativeSelect>,
    )
    const select = screen.getByRole('combobox', { name: 'Pick one' })
    expect(select).toHaveAttribute('aria-invalid', 'true')
    expect(select.className).toContain('border-destructive')
  })

  it('wraps the control in a label carrying the invisible coarse hit-area expansion', () => {
    render(
      <NativeSelect aria-label="Pick one">
        <option value="a">Alpha</option>
      </NativeSelect>,
    )
    const wrapper = screen.getByRole('combobox', { name: 'Pick one' }).closest('label')
    expect(wrapper).not.toBeNull()
    expect(wrapper!.className).toContain('pointer-coarse:before:min-h-11')
  })
})
