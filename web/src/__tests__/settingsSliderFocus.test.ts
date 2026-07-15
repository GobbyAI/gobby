import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import postcss, { type Declaration, type Rule } from 'postcss'
import { describe, expect, it } from 'vitest'

const stylesheet = postcss.parse(
  readFileSync(resolve(process.cwd(), 'src/styles/settings.css'), 'utf8'),
)

function declarationsFor(selector: string): Map<string, string> {
  let matchingRule: Rule | undefined
  stylesheet.walkRules((rule) => {
    if (rule.selector === selector) matchingRule = rule
  })

  expect(matchingRule, `Expected CSS rule ${selector}`).toBeDefined()
  return new Map(
    matchingRule?.nodes
      .filter((node): node is Declaration => node.type === 'decl')
      .map((declaration) => [declaration.prop, declaration.value]),
  )
}

describe('settings slider focus treatment', () => {
  it('shows an accent focus indicator on the track and both browser thumbs', () => {
    expect(declarationsFor('.slider').get('outline')).toBeUndefined()

    expect(declarationsFor('.slider:focus-visible')).toMatchObject(
      new Map([
        ['outline', '2px solid var(--accent)'],
        ['outline-offset', '3px'],
      ]),
    )

    for (const selector of [
      '.slider:focus-visible::-webkit-slider-thumb',
      '.slider:focus-visible::-moz-range-thumb',
    ]) {
      expect(declarationsFor(selector).get('box-shadow')).toBe(
        '0 0 0 2px var(--bg-primary), 0 0 0 4px var(--accent)',
      )
    }
  })
})
