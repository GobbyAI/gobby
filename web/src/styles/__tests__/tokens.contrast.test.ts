import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

import { contrastRatio, contrastRatioOnSrgbTint } from '../../lib/colorContrast'

const AA_NORMAL_TEXT = 4.5
const tokensCss = readFileSync(resolve(process.cwd(), 'src/styles/tokens.css'), 'utf8')
const lightTheme = tokensCss.slice(tokensCss.indexOf('[data-theme="light"]'))

function token(name: string): string {
  const match = new RegExp(`--${name}:\\s*(oklch\\([^;]+\\));`).exec(lightTheme)
  if (!match) throw new Error(`Missing light-theme token --${name}`)
  return match[1]
}

const surfaces = ['bg-primary', 'bg-secondary', 'bg-tertiary'] as const

describe('light-theme semantic token contrast', () => {
  it.each(surfaces)('keeps warning text AA on %s and its warning tint', (surfaceName) => {
    const foreground = token('color-warning-foreground')
    const surface = token(surfaceName)

    expect(contrastRatio(foreground, surface)).toBeGreaterThanOrEqual(AA_NORMAL_TEXT)
    expect(contrastRatioOnSrgbTint(foreground, foreground, 0.1, surface)).toBeGreaterThanOrEqual(
      AA_NORMAL_TEXT,
    )
  })

  it('uses dark ink on the light warning surface', () => {
    expect(contrastRatio(token('text-on-warning'), token('color-warning'))).toBeGreaterThanOrEqual(
      AA_NORMAL_TEXT,
    )
  })

  it.each([
    ['accent', 0.14],
    ['color-error', 0.14],
  ] as const)('keeps %s badge text AA on tertiary and tinted surfaces', (tokenName, alpha) => {
    const foreground = token(tokenName)
    const tertiary = token('bg-tertiary')

    expect(contrastRatio(foreground, tertiary)).toBeGreaterThanOrEqual(AA_NORMAL_TEXT)
    expect(contrastRatioOnSrgbTint(foreground, foreground, alpha, tertiary)).toBeGreaterThanOrEqual(
      AA_NORMAL_TEXT,
    )
  })
})
