import { readdirSync, readFileSync } from 'node:fs'
import { join, relative } from 'node:path'
import { describe, expect, it } from 'vitest'
import {
  BTN_CLASS_ALLOWLIST,
  CLS_CONSTANT_ALLOWLIST,
  CSS_FILE_ALLOWLIST,
  CSS_LINE_TIGHTEN_SLACK,
  CSS_TOTAL_LINE_CEILING,
  IMPORTANT_ALLOWLIST,
  RAW_ELEMENT_ALLOWLIST,
  type RawElement,
} from './styleRatchet.allowlist'

// Style-debt ratchet: legacy idioms may only shrink, never grow. Each check
// compares a fresh scan against the recorded allowlist and fails in both
// directions — above the ceiling (new debt) and below it (stale allowlist
// entry that must be tightened). See docs/guides/frontend-style-guide.md.

const STYLE_GUIDE = 'docs/guides/frontend-style-guide.md'
const ALLOWLIST = 'src/__tests__/styleRatchet.allowlist.ts'
const SKIP_DIRS = new Set(['__tests__', '__visual__', '__fixtures__', '__mocks__', 'test'])
const UI_PRIMITIVES_DIR = 'src/components/ui/'

const BTN_CLASS = /(?<![\w-])btn(?:-[\w-]+)?\b/g
const CLS_CONSTANT = /\bconst\s+[A-Za-z0-9_]*_CLS\b\s*=/g
const IMPORTANT = /!\s*important\b/g
const RAW_ELEMENTS: Record<RawElement, RegExp> = {
  button: /<button\b/g,
  input: /<input\b/g,
  select: /<select\b/g,
  textarea: /<textarea\b/g,
}

function scannedFiles(dir: string): string[] {
  const files: string[] = []
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name)
    if (entry.isDirectory()) {
      if (!SKIP_DIRS.has(entry.name)) files.push(...scannedFiles(path))
      continue
    }
    if (!entry.isFile()) continue
    if (/\.(?:test|spec)\.(?:ts|tsx)$/.test(entry.name)) continue
    if (entry.name.endsWith('.d.ts')) continue
    if (/\.(?:ts|tsx|css)$/.test(entry.name)) files.push(path)
  }
  return files
}

function stripComments(source: string): string {
  return source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
}

function countMatches(source: string, pattern: RegExp): number {
  return Array.from(source.matchAll(pattern)).length
}

interface Scan {
  btnClass: Map<string, number>
  rawElements: Record<RawElement, Map<string, number>>
  clsConstant: Map<string, number>
  cssFiles: string[]
  important: Map<string, number>
  cssTotalLines: number
}

function runScan(): Scan {
  const scan: Scan = {
    btnClass: new Map(),
    rawElements: { button: new Map(), input: new Map(), select: new Map(), textarea: new Map() },
    clsConstant: new Map(),
    cssFiles: [],
    important: new Map(),
    cssTotalLines: 0,
  }

  for (const file of scannedFiles(join(process.cwd(), 'src'))) {
    const rel = relative(process.cwd(), file).split('\\').join('/')
    const raw = readFileSync(file, 'utf8')
    const source = stripComments(raw)

    if (rel.endsWith('.css')) {
      scan.cssFiles.push(rel)
      scan.cssTotalLines += raw.split('\n').length
    } else {
      const btn = countMatches(source, BTN_CLASS)
      if (btn > 0) scan.btnClass.set(rel, btn)
      const cls = countMatches(source, CLS_CONSTANT)
      if (cls > 0) scan.clsConstant.set(rel, cls)
      if (rel.endsWith('.tsx') && !rel.startsWith(UI_PRIMITIVES_DIR)) {
        for (const [element, pattern] of Object.entries(RAW_ELEMENTS)) {
          const count = countMatches(source, pattern)
          if (count > 0) scan.rawElements[element as RawElement].set(rel, count)
        }
      }
    }

    const importantCount = countMatches(source, IMPORTANT)
    if (importantCount > 0) scan.important.set(rel, importantCount)
  }

  return scan
}

const scan = runScan()

// Compares a scan against its allowlist. `remedy` is appended to over-ceiling
// failures; stale failures always demand tightening the allowlist entry.
function ratchet(
  actual: Map<string, number>,
  allowed: Record<string, number>,
  label: string,
  remedy: string,
): string[] {
  const failures: string[] = []
  for (const [file, count] of [...actual.entries()].sort(([a], [b]) => a.localeCompare(b))) {
    const ceiling = allowed[file] ?? 0
    if (count > ceiling) {
      failures.push(`${file}: ${count} ${label} (allowlist permits ${ceiling}). ${remedy}`)
    }
  }
  for (const [file, ceiling] of Object.entries(allowed)) {
    const count = actual.get(file) ?? 0
    if (count < ceiling) {
      failures.push(
        `${file}: allowlist records ${ceiling} ${label} but only ${count} remain. ` +
          `Decrease or delete the entry in ${ALLOWLIST} — entries only ever shrink.`,
      )
    }
  }
  return failures
}

describe('style ratchet', () => {
  it('keeps .btn class usage at or below the recorded per-file counts', () => {
    expect(
      ratchet(
        scan.btnClass,
        BTN_CLASS_ALLOWLIST,
        'btn class tokens',
        `Migrate onto <Button> from components/ui — see ${STYLE_GUIDE}.`,
      ),
    ).toEqual([])
  })

  it('keeps raw interactive elements at or below the recorded per-file counts', () => {
    const failures = (Object.keys(RAW_ELEMENTS) as RawElement[]).flatMap((element) =>
      ratchet(
        scan.rawElements[element],
        RAW_ELEMENT_ALLOWLIST[element],
        `raw <${element}> elements`,
        `Use the components/ui primitive instead of a raw <${element}> — see ${STYLE_GUIDE}.`,
      ),
    )
    expect(failures).toEqual([])
  })

  it('keeps *_CLS style constants at or below the recorded per-file counts', () => {
    expect(
      ratchet(
        scan.clsConstant,
        CLS_CONSTANT_ALLOWLIST,
        '*_CLS constants',
        `Style at the call site with Tailwind utilities (cva for variants) — see ${STYLE_GUIDE}.`,
      ),
    ).toEqual([])
  })

  it('bans stylesheets beyond the recorded set', () => {
    const recorded = new Set(CSS_FILE_ALLOWLIST)
    const actual = new Set(scan.cssFiles)
    const failures = [
      ...scan.cssFiles
        .filter((file) => !recorded.has(file))
        .map(
          (file) =>
            `${file}: new stylesheet. New CSS files are banned — use Tailwind utilities and components/ui primitives (${STYLE_GUIDE}).`,
        ),
      ...CSS_FILE_ALLOWLIST.filter((file) => !actual.has(file)).map(
        (file) => `${file}: recorded stylesheet no longer exists. Delete its entry in ${ALLOWLIST}.`,
      ),
    ]
    expect(failures).toEqual([])
  })

  it('keeps !important usage at or below the recorded per-file counts', () => {
    expect(
      ratchet(
        scan.important,
        IMPORTANT_ALLOWLIST,
        '!important declarations',
        `Never add !important; fix specificity at the source — see ${STYLE_GUIDE}.`,
      ),
    ).toEqual([])
  })

  it('holds the total CSS line ceiling and demands tightening as CSS shrinks', () => {
    expect(
      scan.cssTotalLines,
      `Total CSS grew to ${scan.cssTotalLines} lines (ceiling ${CSS_TOTAL_LINE_CEILING}). ` +
        `Move styling to Tailwind utilities instead of stylesheets — see ${STYLE_GUIDE}.`,
    ).toBeLessThanOrEqual(CSS_TOTAL_LINE_CEILING)
    expect(
      scan.cssTotalLines,
      `Total CSS shrank to ${scan.cssTotalLines} lines, more than ${CSS_LINE_TIGHTEN_SLACK} below ` +
        `the ceiling ${CSS_TOTAL_LINE_CEILING}. Lower CSS_TOTAL_LINE_CEILING to ${scan.cssTotalLines} in ${ALLOWLIST}.`,
    ).toBeGreaterThanOrEqual(CSS_TOTAL_LINE_CEILING - CSS_LINE_TIGHTEN_SLACK)
  })
})
