import { readdirSync, readFileSync, statSync } from 'node:fs'
import { extname, join, relative } from 'node:path'
import { describe, expect, it } from 'vitest'

const SOURCE_EXTENSIONS = new Set(['.css', '.ts', '.tsx', '.js', '.jsx', '.mjs'])
const IGNORED_SEGMENTS = new Set(['__tests__', '__visual__', 'test'])
const DYNAMIC_TOKEN_PREFIXES = [
  '--category-',
  '--channel-',
  '--isolation-',
  '--lang-',
  '--provider-',
  '--source-',
  '--step-type-',
]

function sourceFiles(dir: string): string[] {
  const files: string[] = []

  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name)
    if (entry.isDirectory()) {
      if (!IGNORED_SEGMENTS.has(entry.name)) files.push(...sourceFiles(path))
      continue
    }

    if (entry.isFile() && SOURCE_EXTENSIONS.has(extname(entry.name))) {
      files.push(path)
    }
  }

  return files
}

function collectDefinedTokens(files: string[]): Set<string> {
  const defined = new Set<string>()
  const definitionPattern = /(?:^|[\s{;,])['"]?(--[A-Za-z0-9_-]+)['"]?\s*:/gm

  for (const file of files) {
    const source = stripComments(readFileSync(file, 'utf8'))
    for (const match of source.matchAll(definitionPattern)) {
      defined.add(match[1])
    }
  }

  return defined
}

function stripComments(source: string): string {
  return source
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/^\s*\/\/.*$/gm, '')
}

function isAllowedExternalToken(token: string): boolean {
  return token.startsWith('--radix-')
}

function isDynamicTokenPrefix(token: string): boolean {
  return DYNAMIC_TOKEN_PREFIXES.includes(token) || DYNAMIC_TOKEN_PREFIXES.some(prefix => {
    return token.startsWith(prefix) && token.includes('$')
  })
}

interface TokenReference {
  file: string
  line: number
  token: string
  hasFallback: boolean
}

function tokenReferences(file: string, source: string): TokenReference[] {
  const references: TokenReference[] = []
  const pattern = /var\(\s*(--[A-Za-z0-9_-]+)([^)]*)\)/g
  const lineStarts = [0]

  for (let index = source.indexOf('\n'); index !== -1; index = source.indexOf('\n', index + 1)) {
    lineStarts.push(index + 1)
  }

  for (const match of source.matchAll(pattern)) {
    const offset = match.index ?? 0
    let line = 1
    for (let i = 0; i < lineStarts.length; i += 1) {
      if (lineStarts[i] > offset) break
      line = i + 1
    }
    references.push({
      file,
      line,
      token: match[1],
      hasFallback: match[2].includes(','),
    })
  }

  return references
}

describe('CSS token integrity', () => {
  it('keeps static var() references backed by defined tokens or explicit fallbacks', () => {
    const root = join(process.cwd(), 'src')
    expect(statSync(root).isDirectory()).toBe(true)

    const files = sourceFiles(root)
    const definedTokens = collectDefinedTokens(files)
    const unresolved: string[] = []

    for (const file of files) {
      const source = stripComments(readFileSync(file, 'utf8'))
      for (const reference of tokenReferences(file, source)) {
        if (definedTokens.has(reference.token)) continue
        if (reference.hasFallback) continue
        if (isAllowedExternalToken(reference.token)) continue
        if (isDynamicTokenPrefix(reference.token)) continue

        unresolved.push(
          `${relative(process.cwd(), reference.file)}:${reference.line} ${reference.token}`,
        )
      }
    }

    expect(unresolved).toEqual([])
  })
})
