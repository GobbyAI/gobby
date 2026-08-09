import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { describe, expect, it } from 'vitest'

const focusManagedInputs = [
  'ValidationDetectionEditor.tsx',
]

describe('shared input focus styles', () => {
  it('uses inputFocusCls instead of suppressing outlines locally', () => {
    for (const file of focusManagedInputs) {
      const source = readFileSync(join(process.cwd(), 'src/components', file), 'utf8')

      expect(source, file).toContain('inputFocusCls')
      expect(source, file).not.toMatch(/(?:^|\s)(?:focus:)?outline-none(?:\s|['"`])/)
    }
  })
})
