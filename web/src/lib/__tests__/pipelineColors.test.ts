import { describe, expect, it } from 'vitest'
import { EXEC_STATUS_COLORS, getExecStatusKind } from '../pipelineColors'

describe('pipelineColors', () => {
  it('maps skipped executions to the warning color pair and status kind', () => {
    expect(EXEC_STATUS_COLORS.skipped).toEqual({
      dark: 'oklch(78% 0.15 75)',
      light: 'oklch(45% 0.18 75)',
    })
    expect(getExecStatusKind('skipped')).toBe('warning')
  })
})
