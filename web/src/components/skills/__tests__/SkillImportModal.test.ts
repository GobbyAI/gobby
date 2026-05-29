import { describe, expect, it } from 'vitest'
import { detectSourceType } from '../sourceType'

describe('detectSourceType', () => {
  it('accepts GitHub sources with an exact github.com host', () => {
    expect(detectSourceType('https://github.com/GobbyAI/gobby')).toBe('github')
    expect(detectSourceType('http://github.com/GobbyAI/gobby')).toBe('github')
    expect(detectSourceType('github:GobbyAI/gobby')).toBe('github')
  })

  it('rejects URLs that only contain github.com as a prefix', () => {
    expect(detectSourceType('https://github.com.evil.example/GobbyAI/gobby')).toBe('unknown')
    expect(detectSourceType('http://github.com@evil.example/GobbyAI/gobby')).toBe('unknown')
  })

  it('rejects non-GitHub URL schemes before owner/repo fallback', () => {
    expect(detectSourceType('ssh://github.com/GobbyAI/gobby')).toBe('unknown')
    expect(detectSourceType('foo://bar/baz')).toBe('unknown')
  })
})
