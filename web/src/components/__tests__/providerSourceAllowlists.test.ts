import { readdirSync, readFileSync, statSync } from 'node:fs'
import { extname, join, relative } from 'node:path'
import { describe, expect, it } from 'vitest'

const COMPONENTS_DIR = join(process.cwd(), 'src/components')
const SELF_SUFFIX = join('__tests__', 'providerSourceAllowlists.test.ts')
const PROVIDER_SOURCES = ['claude', 'gemini', 'qwen', 'codex'] as const

function walk(dir: string): string[] {
  return readdirSync(dir).flatMap((entry) => {
    const path = join(dir, entry)
    if (path.endsWith(SELF_SUFFIX)) return []
    if (statSync(path).isDirectory()) return walk(path)
    return ['.ts', '.tsx'].includes(extname(path)) ? [path] : []
  })
}

function hasEveryBaseProvider(snippet: string): boolean {
  return PROVIDER_SOURCES.every(
    (provider) => snippet.includes(`'${provider}'`) || snippet.includes(`"${provider}"`),
  )
}

function isMissingDroid(snippet: string): boolean {
  return !snippet.includes("'droid'") && !snippet.includes('"droid"')
}

describe('component provider source allowlists', () => {
  it('does not leave hardcoded four-provider allowlists without droid', () => {
    const violations = walk(COMPONENTS_DIR).flatMap((path) => {
      const contents = readFileSync(path, 'utf8')
      const arrayLiterals = contents.match(/\[[^\][]{0,500}\]/gs) ?? []
      const unionLiterals =
        contents.match(
          /(?:['"](?:claude|gemini|qwen|codex|droid)['"]\s*\|\s*){3,}['"](?:claude|gemini|qwen|codex|droid)['"]/g,
        ) ?? []

      return [...arrayLiterals, ...unionLiterals]
        .filter((snippet) => hasEveryBaseProvider(snippet) && isMissingDroid(snippet))
        .map((snippet) => `${relative(COMPONENTS_DIR, path)}: ${snippet.trim()}`)
    })

    expect(violations).toEqual([])
  })
})
