import { existsSync, readFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import postcss, { type Root, type Rule } from 'postcss'
import * as ts from 'typescript'
import { describe, expect, it } from 'vitest'

function isWebPackageRoot(path: string): boolean {
  return existsSync(join(path, 'package.json')) && existsSync(join(path, 'src/main.tsx'))
}

function resolveWebPackageRoot(): string {
  const current = process.cwd()
  if (isWebPackageRoot(current)) return current

  const fallback = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..')
  if (isWebPackageRoot(fallback)) return fallback

  throw new Error(`Unable to resolve web package root from cwd=${current}`)
}

const cwd = resolveWebPackageRoot()

interface CssParent {
  type: string
  name?: string
  params?: string
  parent?: CssParent
}

function readSource(rel: string): string {
  return readFileSync(join(cwd, rel), 'utf8')
}

function importSpecifiers(source: string): string[] {
  const sourceFile = ts.createSourceFile(
    'main.tsx',
    source,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TSX,
  )
  return sourceFile.statements
    .filter(ts.isImportDeclaration)
    .map(statement => statement.moduleSpecifier)
    .filter(ts.isStringLiteral)
    .map(specifier => specifier.text)
}

function parseCss(rel: string): Root {
  return postcss.parse(readSource(rel), { from: rel })
}

function hasMediaAncestor(rule: Rule, media: string): boolean {
  let parent = rule.parent as CssParent | undefined
  while (parent) {
    if (parent.type === 'atrule' && parent.name === 'media') {
      if (parent.params?.includes(media)) return true
    }
    parent = parent.parent
  }
  return false
}

function hasAnyMediaAncestor(rule: Rule): boolean {
  let parent = rule.parent as CssParent | undefined
  while (parent) {
    if (parent.type === 'atrule' && parent.name === 'media') return true
    parent = parent.parent
  }
  return false
}

function findRule(root: Root, selector: string, media?: string): Rule {
  let found: Rule | undefined
  root.walkRules(selector, rule => {
    if (found) return
    const mediaMatches = media ? hasMediaAncestor(rule, media) : !hasAnyMediaAncestor(rule)
    if (mediaMatches) found = rule
  })
  expect(found, `Expected CSS rule ${selector}${media ? ` in ${media}` : ''}`).toBeDefined()
  return found as Rule
}

function expectDeclarations(
  root: Root,
  selector: string,
  expected: Record<string, string>,
  media?: string,
): void {
  const rule = findRule(root, selector, media)
  const declarations = new Map<string, string>()
  rule.walkDecls(declaration => {
    declarations.set(declaration.prop, declaration.value)
  })

  for (const [property, value] of Object.entries(expected)) {
    expect(declarations.get(property)).toBe(value)
  }
}

describe('mobile chrome CSS', () => {
  it('loads the app shell stylesheet after base button styles', () => {
    const source = readSource('src/main.tsx')
    const imports = importSpecifiers(source)

    expect(imports.indexOf('./styles/buttons.css')).toBeLessThan(
      imports.indexOf('./styles/app-shell.css'),
    )
  })

  it('keeps the top app chrome compact on mobile touch viewports', () => {
    const appSource = readSource('src/App.tsx')
    const shellCss = parseCss('src/styles/app-shell.css')

    expect(appSource).toContain('className="app-header"')
    expect(appSource).toContain('className="app-brand"')
    expect(appSource).toContain('variant="ghost"')
    expect(appSource).toContain('className="app-menu-button"')
    expect(appSource).toContain('className="app-brand-logo"')
    expect(appSource).toContain('className="app-brand-title"')
    expect(appSource).toContain('className="app-header-actions"')
    expect(appSource).toContain('className="app-health-badge')

    expectDeclarations(
      shellCss,
      '.app-header-actions .project-selector-segmented-wrap',
      { display: 'none' },
      '(max-width: 430px)',
    )
    expectDeclarations(
      shellCss,
      '.project-selector-compact-wrap',
      { display: 'inline-flex' },
      '(max-width: 430px)',
    )
    expectDeclarations(shellCss, '.project-selector-compact-trigger', {
      background: 'var(--accent-tint)',
      color: 'var(--accent)',
    })
    expectDeclarations(shellCss, '.app-menu-button', {
      width: '2.25rem',
      'min-height': '2.25rem',
      'border-color': 'transparent',
    })
    expectDeclarations(shellCss, '.app-header-actions', {
      '--control-row-height': 'var(--status-bar-control-height)',
      'flex-wrap': 'nowrap',
    })
    expectDeclarations(
      shellCss,
      '.app-brand-title',
      { 'font-size': '1.25rem' },
      '(max-width: 768px)',
    )
  })

  it('keeps activity panel filter toolbars to one compact row', () => {
    const activityCss = parseCss('src/components/chat/styles/activity-panel.css')

    expectDeclarations(activityCss, '.activity-panel-toolbar', {
      '--control-row-height-sm': 'var(--status-bar-control-height)',
      'flex-wrap': 'nowrap',
    })
    expectDeclarations(activityCss, '.activity-panel-search', {
      flex: '1 1 9rem',
      'min-width': '0',
      'min-height': 'var(--control-row-height-sm)',
    })
    expectDeclarations(activityCss, '.activity-panel-toolbar .btn-sm', {
      'min-height': 'var(--status-bar-control-height)',
    })
    expectDeclarations(
      activityCss,
      '.activity-panel-toolbar-segmented > .segmented-control__option',
      { 'padding-inline': '0.55rem' },
    )
    expectDeclarations(activityCss, '.activity-panel-toolbar-segmented', {
      'font-size': 'var(--text-sm)',
    })
  })
})
