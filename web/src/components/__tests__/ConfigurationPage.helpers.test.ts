import { describe, expect, it } from 'vitest'
import { splitSafeConfigPath } from '../ConfigurationPage.helpers'

describe('splitSafeConfigPath', () => {
  it('splits ordinary nested config paths', () => {
    expect(splitSafeConfigPath('daemon.http.port')).toEqual(['daemon', 'http', 'port'])
  })

  it('rejects prototype-polluting path segments', () => {
    expect(splitSafeConfigPath('__proto__.polluted')).toBeNull()
    expect(splitSafeConfigPath('daemon.constructor.polluted')).toBeNull()
    expect(splitSafeConfigPath('daemon.prototype.polluted')).toBeNull()
  })
})
