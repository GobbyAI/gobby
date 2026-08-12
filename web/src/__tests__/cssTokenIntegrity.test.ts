import { readdirSync, readFileSync, statSync } from 'node:fs'
import { extname, join, relative } from 'node:path'
import { compile } from 'tailwindcss'
import { describe, expect, it } from 'vitest'

const SOURCE_EXTENSIONS = new Set(['.css', '.ts', '.tsx', '.js', '.jsx', '.mjs'])
const IGNORED_SEGMENTS = new Set(['__tests__', '__visual__', 'test'])
const DYNAMIC_TOKEN_PREFIXES = [
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
  // `[` admits Tailwind arbitrary-property definitions ([--token:value]);
  // var() references stay excluded because `(` precedes their token.
  const definitionPattern = /(?:^|[\s{;,[])['"]?(--[A-Za-z0-9_-]+)['"]?\s*:/gm

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
  it('keeps the compiled mobile variant aligned with emitted tier properties', async () => {
    const theme = readFileSync(join(process.cwd(), 'src/styles/tailwind-theme.css'), 'utf8')
    const compiler = await compile(`${theme}\n@tailwind utilities;`)
    const compiled = compiler.build(['mobile:block'])
    const rootBlock = compiled.match(/:root,\s*:host\s*\{([^}]*)\}/)?.[1] ?? ''
    const emittedProperties = new Map(
      [...rootBlock.matchAll(/(--[\w-]+):\s*([^;]+);/g)].map(([, name, value]) => [
        name,
        value.trim(),
      ]),
    )
    const compiledMedia = compiled.match(
      /@media\s*\(max-width:\s*(\d+)px\),\s*\(max-height:\s*(\d+)px\)/,
    )

    expect(compiledMedia).not.toBeNull()
    expect(compiledMedia?.slice(1)).toEqual([
      emittedProperties.get('--breakpoint-mobile-max-width')?.replace('px', ''),
      emittedProperties.get('--breakpoint-mobile-max-height')?.replace('px', ''),
    ])
    expect(emittedProperties.get('--breakpoint-desktop')).toBe('768px')
    expect(compiledMedia?.[0]).not.toContain('var(')
  })

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

  it('keeps review and inactive colors in sanctioned palette lanes', () => {
    const tokens = readFileSync(join(process.cwd(), 'src/styles/tokens.css'), 'utf8')

    expect(tokens.match(/--color-review:\s*var\(--color-info\);/g)).toHaveLength(2)
    expect(tokens.match(/--color-review-bg:\s*var\(--color-info-bg\);/g)).toHaveLength(2)
    expect(tokens.match(/--color-review-soft:\s*var\(--color-info-soft\);/g)).toHaveLength(2)

    const inactiveValues = [...tokens.matchAll(
      /--color-inactive:\s*oklch\(([\d.]+)%\s+([\d.]+)\s+([\d.]+)\);/g,
    )].map(([, lightness, chroma, hue]) => ({
      lightness: Number(lightness),
      chroma: Number(chroma),
      hue: Number(hue),
    }))

    expect(inactiveValues).toEqual([
      { lightness: 60, chroma: 0.008, hue: 125 },
      { lightness: 45, chroma: 0.008, hue: 125 },
    ])
  })

  it('keeps the typography ladder defined once, in tokens.css', () => {
    const theme = readFileSync(join(process.cwd(), 'src/styles/tailwind-theme.css'), 'utf8')
    const tokens = readFileSync(join(process.cwd(), 'src/styles/tokens.css'), 'utf8')

    expect(theme).not.toMatch(/--text-[0-9a-z]+:\s*calc\(/)
    expect(theme).not.toMatch(/--text-[0-9a-z]+:\s*var\(--font-size-base\)/)
    for (const step of ['2xs', 'xs', 'sm', 'md', 'base', 'lg', 'xl', '2xl', '3xl', '4xl']) {
      expect(theme).toContain(`--text-${step}: var(--text-${step});`)
      expect(theme.match(new RegExp(`^\\s*--text-${step}:`, 'gm'))).toHaveLength(1)
      expect(tokens.match(new RegExp(`^\\s*--text-${step}:`, 'gm'))).toHaveLength(1)
    }
  })

  it('bridges shadow, radius, and surface families into the Tailwind theme', () => {
    const theme = readFileSync(join(process.cwd(), 'src/styles/tailwind-theme.css'), 'utf8')

    for (const shadow of ['sm', 'md', 'lg', 'xl', 'popover-up', 'panel-left']) {
      expect(theme).toContain(`--shadow-${shadow}: var(--shadow-${shadow});`)
    }
    for (const radius of ['xs', 'sm', 'md', 'lg', 'xl']) {
      expect(theme).toMatch(new RegExp(`--radius-${radius}:\\s*0\\.\\d+rem;`))
    }
    for (const [themeName, token] of [
      ['color-surface-secondary', 'bg-secondary'],
      ['color-surface-deep', 'bg-deep'],
      ['color-surface-scrim', 'surface-scrim'],
      ['color-surface-scrim-opaque', 'surface-scrim-opaque'],
      ['color-foreground-muted', 'text-muted'],
      ['color-border-soft', 'border-soft'],
      ['color-accent-soft', 'accent-soft'],
      ['color-accent-tint', 'accent-tint'],
    ]) {
      expect(theme).toContain(`--${themeName}: var(--${token});`)
    }
  })

  it('keeps state colors on the foreground-first sibling contract', () => {
    const tokens = readFileSync(join(process.cwd(), 'src/styles/tokens.css'), 'utf8')
    const theme = readFileSync(join(process.cwd(), 'src/styles/tailwind-theme.css'), 'utf8')

    for (const lane of ['info', 'warning', 'error', 'success']) {
      for (const suffix of ['', '-bg', '-foreground', '-soft', '-tint']) {
        expect(tokens.match(new RegExp(`--color-${lane}${suffix}:`, 'g'))).toHaveLength(2)
        expect(theme).toContain(`--color-${lane}${suffix}: var(--color-${lane}${suffix});`)
      }

      expect(tokens.match(new RegExp(`--text-on-${lane}:`, 'g'))).toHaveLength(2)
      const values = (name: string) =>
        [...tokens.matchAll(new RegExp(`--${name}:\\s*([^;]+);`, 'g'))].map((match) => match[1])
      expect(values(`color-${lane}-foreground`)).toEqual(values(`color-${lane}`))
    }
  })
})
