import { describe, it, expect } from 'vitest'
import { cn, escapeHtml, previewContent } from '../utils'

describe('cn', () => {
  it('returns empty string with no args', () => {
    expect(cn()).toBe('')
  })

  it('passes through a single class', () => {
    expect(cn('text-green-500')).toBe('text-green-500')
  })

  it('merges multiple classes', () => {
    const result = cn('px-2', 'py-1', 'text-sm')
    expect(result).toContain('px-2')
    expect(result).toContain('py-1')
    expect(result).toContain('text-sm')
  })

  it('resolves conflicting Tailwind classes (last wins)', () => {
    const result = cn('text-green-500', 'text-blue-500')
    expect(result).toBe('text-blue-500')
    expect(result).not.toContain('text-green-500')
  })

  it('handles conditional classes via clsx', () => {
    const active = true
    const result = cn('base', active && 'active-class', !active && 'inactive-class')
    expect(result).toContain('base')
    expect(result).toContain('active-class')
    expect(result).not.toContain('inactive-class')
  })

  it('handles object syntax', () => {
    const result = cn({ 'text-green-500': true, 'bg-blue-500': false })
    expect(result).toBe('text-green-500')
  })

  it('handles array syntax', () => {
    const result = cn(['px-2', 'py-1'])
    expect(result).toContain('px-2')
    expect(result).toContain('py-1')
  })

  it('handles undefined and null gracefully', () => {
    const result = cn('base', undefined, null, 'extra')
    expect(result).toContain('base')
    expect(result).toContain('extra')
  })
})

describe('escapeHtml', () => {
  it('escapes single quotes along with other HTML-sensitive characters', () => {
    expect(escapeHtml(`'"<>&`)).toBe('&#x27;&quot;&lt;&gt;&amp;')
  })
})

describe('previewContent', () => {
  it('truncates long memory text for compact labels', () => {
    expect(previewContent('a'.repeat(141))).toBe(`${'a'.repeat(140)}...`)
  })
})
