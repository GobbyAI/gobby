import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { BoundedSelectField } from '../BoundedSelectField'
import { StringListField } from '../StringListField'
import { KeyValueMapField } from '../KeyValueMapField'
import { TypedListField } from '../TypedListField'

afterEach(() => {
  cleanup()
})

const SEARCH_OPTIONS = [
  { value: 'keyword', label: 'Keyword' },
  { value: 'hybrid', label: 'Hybrid' },
]

describe('BoundedSelectField', () => {
  it('offers only the allowed options for a known value', () => {
    render(
      <BoundedSelectField
        label="Search mode"
        ariaLabel="Search mode"
        value="keyword"
        onChange={() => {}}
        options={SEARCH_OPTIONS}
      />,
    )
    const select = screen.getByLabelText('Search mode')
    expect(within(select).getAllByRole('option')).toHaveLength(2)
    expect(screen.queryByText(/unsupported/)).toBeNull()
  })

  it('surfaces a persisted out-of-range value as a flagged option', () => {
    render(
      <BoundedSelectField
        label="Search mode"
        ariaLabel="Search mode"
        value="legacy"
        onChange={() => {}}
        options={SEARCH_OPTIONS}
      />,
    )
    const select = screen.getByLabelText('Search mode')
    const options = within(select).getAllByRole('option')
    expect(options).toHaveLength(3)
    expect(options[0]?.textContent).toBe('legacy (unsupported)')
  })
})

describe('StringListField', () => {
  it('edits, removes, and appends entries', () => {
    const onChange = vi.fn()
    render(
      <StringListField
        label="CORS origins"
        ariaLabel="CORS origin"
        value={['https://a.test', 'https://b.test']}
        onChange={onChange}
      />,
    )

    fireEvent.change(screen.getByLabelText('CORS origin item 1'), {
      target: { value: 'https://c.test' },
    })
    expect(onChange).toHaveBeenCalledWith(['https://c.test', 'https://b.test'])

    fireEvent.click(screen.getByRole('button', { name: 'Remove CORS origin item 2' }))
    expect(onChange).toHaveBeenCalledWith(['https://a.test'])

    fireEvent.click(screen.getByRole('button', { name: 'Add item' }))
    expect(onChange).toHaveBeenCalledWith(['https://a.test', 'https://b.test', ''])
  })

  it('shows an empty state with no entries', () => {
    render(
      <StringListField
        label="CORS origins"
        ariaLabel="CORS origin"
        value={[]}
        onChange={() => {}}
      />,
    )
    expect(screen.getByText('No entries.')).not.toBeNull()
  })
})

describe('KeyValueMapField', () => {
  it('renames a key while preserving its value and order', () => {
    const onChange = vi.fn()
    render(
      <KeyValueMapField
        label="Tool timeouts"
        ariaLabel="Tool timeout"
        value={{ search: '30', build: '60' }}
        onChange={onChange}
      />,
    )

    fireEvent.change(screen.getByLabelText('Tool timeout key 1'), {
      target: { value: 'lookup' },
    })
    expect(onChange).toHaveBeenCalledWith({ lookup: '30', build: '60' })
  })

  it('appends a blank entry and removes by key', () => {
    const onChange = vi.fn()
    render(
      <KeyValueMapField
        label="Tool timeouts"
        ariaLabel="Tool timeout"
        value={{ search: '30' }}
        onChange={onChange}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Add entry' }))
    expect(onChange).toHaveBeenCalledWith({ search: '30', '': '' })

    fireEvent.click(screen.getByRole('button', { name: 'Remove search' }))
    expect(onChange).toHaveBeenCalledWith({})
  })

  it('uses a custom value renderer when provided', () => {
    const onChange = vi.fn()
    render(
      <KeyValueMapField<number>
        label="Limits"
        ariaLabel="Limit"
        value={{ max: 5 }}
        onChange={onChange}
        createValue={() => 0}
        renderValue={(value, onValueChange, key) => (
          <input
            type="number"
            aria-label={`limit-${key}`}
            value={value}
            onChange={(event) => onValueChange(Number(event.target.value))}
          />
        )}
      />,
    )

    fireEvent.change(screen.getByLabelText('limit-max'), { target: { value: '9' } })
    expect(onChange).toHaveBeenCalledWith({ max: 9 })
  })
})

interface Policy {
  name: string
}

describe('TypedListField', () => {
  it('renders items, adds with createItem, and removes', () => {
    const onChange = vi.fn()
    render(
      <TypedListField<Policy>
        label="Policies"
        ariaLabel="Policy"
        value={[{ name: 'allow-read' }]}
        onChange={onChange}
        createItem={() => ({ name: '' })}
        itemLabel={(item) => item.name || 'New policy'}
        renderItem={(item, onItemChange) => (
          <input
            aria-label={`policy-${item.name}`}
            value={item.name}
            onChange={(event) => onItemChange({ name: event.target.value })}
          />
        )}
      />,
    )

    expect(screen.getByText('allow-read')).not.toBeNull()

    fireEvent.change(screen.getByLabelText('policy-allow-read'), {
      target: { value: 'allow-write' },
    })
    expect(onChange).toHaveBeenCalledWith([{ name: 'allow-write' }])

    fireEvent.click(screen.getByRole('button', { name: 'Add item' }))
    expect(onChange).toHaveBeenCalledWith([{ name: 'allow-read' }, { name: '' }])

    fireEvent.click(screen.getByRole('button', { name: 'Remove Policy 1' }))
    expect(onChange).toHaveBeenCalledWith([])
  })
})
