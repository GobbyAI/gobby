import { describe, it, expect } from 'vitest'
import { COMMANDS } from '../useSlashCommands'

// ---------------------------------------------------------------------------
// COMMANDS constant
// ---------------------------------------------------------------------------
describe('COMMANDS', () => {
  it('is a non-empty array', () => {
    expect(COMMANDS.length).toBeGreaterThan(0)
  })

  it('every command has required fields', () => {
    for (const cmd of COMMANDS) {
      expect(cmd.name).toBeTruthy()
      expect(cmd.description).toBeTruthy()
      expect(cmd.action).toBeTruthy()
    }
  })

  it('command names are unique', () => {
    const names = COMMANDS.map(c => c.name)
    expect(new Set(names).size).toBe(names.length)
  })

  it('command names are alphabetically sorted', () => {
    const names = COMMANDS.map(c => c.name)
    const sorted = [...names].sort()
    expect(names).toEqual(sorted)
  })

  it('includes expected core commands', () => {
    const names = COMMANDS.map(c => c.name)
    expect(names).toContain('clear')
    expect(names).toContain('plan')
    expect(names).toContain('settings')
    expect(names).toContain('skills')
  })
})
