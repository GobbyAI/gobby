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

let webPackageRoot: string | undefined

interface CssParent {
  type: string
  name?: string
  params?: string
  parent?: CssParent
}

function readSource(rel: string): string {
  webPackageRoot ??= resolveWebPackageRoot()
  return readFileSync(join(webPackageRoot, rel), 'utf8')
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

function jsxAttributeTextParts(attribute: ts.JsxAttribute): string[] {
  const initializer = attribute.initializer
  if (!initializer) return []
  if (ts.isStringLiteral(initializer)) return [initializer.text]
  if (ts.isJsxExpression(initializer) && initializer.expression) {
    return expressionTextParts(initializer.expression)
  }
  return []
}

function expressionTextParts(expression: ts.Expression): string[] {
  if (ts.isStringLiteral(expression) || ts.isNoSubstitutionTemplateLiteral(expression)) {
    return [expression.text]
  }
  if (ts.isTemplateExpression(expression)) {
    return [
      expression.head.text,
      ...expression.templateSpans.map(span => span.literal.text),
    ]
  }
  if (ts.isConditionalExpression(expression)) {
    return [
      ...expressionTextParts(expression.whenTrue),
      ...expressionTextParts(expression.whenFalse),
    ]
  }
  if (ts.isParenthesizedExpression(expression)) {
    return expressionTextParts(expression.expression)
  }
  if (ts.isCallExpression(expression)) {
    return expression.arguments.flatMap(argument => expressionTextParts(argument))
  }
  return []
}

function jsxAttributeValues(source: string, attrName: string): string[] {
  const sourceFile = ts.createSourceFile(
    'component.tsx',
    source,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TSX,
  )
  const values: string[] = []

  function visit(node: ts.Node): void {
    if (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) {
      for (const prop of node.attributes.properties) {
        if (!ts.isJsxAttribute(prop) || !ts.isIdentifier(prop.name)) continue
        if (prop.name.text !== attrName) continue
        values.push(...jsxAttributeTextParts(prop))
      }
    }
    ts.forEachChild(node, visit)
  }

  visit(sourceFile)
  return values
}

function jsxClassTokens(source: string): Set<string> {
  return new Set(
    jsxAttributeValues(source, 'className')
      .flatMap(value => value.split(/\s+/))
      .filter(Boolean),
  )
}

function expectClassToken(source: string, token: string): void {
  expect(jsxClassTokens(source).has(token), `Expected JSX class token ${token}`).toBe(true)
}

function expectNoClassToken(source: string, token: string): void {
  expect(jsxClassTokens(source).has(token), `Unexpected JSX class token ${token}`).toBe(false)
}

function expectStringAttribute(source: string, attrName: string, value: string): void {
  expect(jsxAttributeValues(source, attrName)).toContain(value)
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
    if (parent.type === 'atrule') return true
    parent = parent.parent
  }
  return false
}

function hasContainerAncestor(rule: Rule, container: string): boolean {
  let parent = rule.parent as CssParent | undefined
  while (parent) {
    if (parent.type === 'atrule' && parent.name === 'container') {
      if (parent.params === container) return true
    }
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

function findContainerRule(root: Root, selector: string, container: string): Rule {
  let found: Rule | undefined
  root.walkRules(selector, rule => {
    if (found) return
    if (hasContainerAncestor(rule, container)) found = rule
  })
  expect(found, `Expected CSS rule ${selector} in @container ${container}`).toBeDefined()
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

function expectNoDeclaration(root: Root, selector: string, property: string, media?: string): void {
  const rule = findRule(root, selector, media)
  const declarations = new Set<string>()
  rule.walkDecls(declaration => {
    declarations.add(declaration.prop)
  })

  expect(declarations.has(property)).toBe(false)
}

function expectContainerDeclarations(
  root: Root,
  selector: string,
  container: string,
  expected: Record<string, string>,
): void {
  const rule = findContainerRule(root, selector, container)
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
    const buttonsIndex = imports.indexOf('./styles/buttons.css')
    const segmentedControlIndex = imports.indexOf('./styles/segmented-control.css')
    const appShellIndex = imports.indexOf('./styles/app-shell.css')

    expect(buttonsIndex).toBeGreaterThanOrEqual(0)
    expect(segmentedControlIndex).toBeGreaterThanOrEqual(0)
    expect(appShellIndex).toBeGreaterThanOrEqual(0)
    expect(buttonsIndex).toBeLessThan(segmentedControlIndex)
    expect(segmentedControlIndex).toBeLessThan(appShellIndex)
  })

  it('keeps the top app chrome compact on mobile touch viewports', () => {
    const appSource = readSource('src/App.tsx')
    const segmentedControlCss = parseCss('src/styles/segmented-control.css')
    const shellCss = parseCss('src/styles/app-shell.css')

    expectClassToken(appSource, 'app-header')
    expectClassToken(appSource, 'app-brand')
    expectStringAttribute(appSource, 'variant', 'ghost')
    expectClassToken(appSource, 'app-menu-button')
    expectClassToken(appSource, 'app-brand-logo')
    expectClassToken(appSource, 'app-brand-title')
    expectClassToken(appSource, 'app-header-actions')
    expectClassToken(appSource, 'app-health-badge')

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
      width: '2rem',
      'min-height': 'var(--control-row-height)',
      border: '1px solid color-mix(in srgb, var(--accent) 35%, transparent)',
      background: 'var(--accent-tint)',
      color: 'var(--accent)',
    })
    expectDeclarations(shellCss, '.app-menu-button:hover', {
      'border-color': 'color-mix(in srgb, var(--accent) 55%, transparent)',
      background: 'var(--accent-soft)',
      color: 'var(--accent)',
    })
    expectDeclarations(segmentedControlCss, '.segmented-control__option--sm', {
      'padding-inline': '0.5rem',
    })
    expectDeclarations(segmentedControlCss, '.segmented-control__option--md', {
      'padding-inline': '0.75rem',
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

  it('keeps the minimum-width chat status bar to one row', () => {
    const inputCss = parseCss('src/components/chat/styles/input.css')
    const narrowChatColumn = 'chat-column (max-width: 360px)'

    expectContainerDeclarations(inputCss, '.agent-status-bar__summary', narrowChatColumn, {
      'flex-wrap': 'nowrap',
    })
    expectContainerDeclarations(inputCss, '.chat-session-status', narrowChatColumn, {
      'flex-wrap': 'nowrap',
    })
    expectContainerDeclarations(inputCss, '.chat-session-status__state', narrowChatColumn, {
      display: 'none',
    })
  })

  it('right-aligns command and status bar action slots', () => {
    const layoutCss = parseCss('src/components/chat/styles/layout.css')
    const inputCss = parseCss('src/components/chat/styles/input.css')

    expectDeclarations(layoutCss, '.command-bar', {
      padding: '0 0.75rem',
    })
    expectDeclarations(inputCss, '.agent-status-bar', {
      padding: '0 0.75rem',
    })
    expectContainerDeclarations(inputCss, '.command-bar', 'chat-column (max-width: 360px)', {
      'padding-inline': '0.75rem 0.5rem',
    })
    expectContainerDeclarations(inputCss, '.agent-status-bar', 'chat-column (max-width: 360px)', {
      'padding-inline': '0.75rem 0.5rem',
    })
  })

  it('keeps the minimum-width chat input toolbar controls to one row', () => {
    const inputCss = parseCss('src/components/chat/styles/input.css')
    const chatInputSource = readSource('src/components/chat/ChatInput.tsx')
    const segmentedControlSource = readSource('src/components/ui/SegmentedControl.tsx')
    const agentIndicatorSource = readSource('src/components/chat/ActiveAgentIndicator.tsx')
    const narrowChatColumn = 'chat-column (max-width: 360px)'

    expectClassToken(chatInputSource, 'chat-input-footer')
    expectClassToken(chatInputSource, 'py-3')
    expectNoClassToken(chatInputSource, 'px-4')
    expectNoClassToken(segmentedControlSource, 'px-2')
    expectNoClassToken(segmentedControlSource, 'px-3')
    expectClassToken(agentIndicatorSource, 'chat-input-agent-button')
    expectClassToken(agentIndicatorSource, 'rounded')
    expectNoClassToken(agentIndicatorSource, 'p-1.5')
    expectDeclarations(inputCss, '.chat-input-footer', {
      'padding-inline': '1rem',
    })
    expectNoDeclaration(inputCss, '.chat-input-toolbar', 'padding-right')
    expectContainerDeclarations(inputCss, '.chat-input-toolbar__left', narrowChatColumn, {
      gap: '0.25rem',
      'flex-wrap': 'nowrap',
    })
    expectDeclarations(inputCss, '.chat-input-voice-mic', {
      gap: '0.25rem',
    })
    expectDeclarations(inputCss, '.chat-input-agent-button', {
      display: 'inline-flex',
      width: '2.25rem',
      height: '2.25rem',
      padding: '0',
    })
    expectContainerDeclarations(
      inputCss,
      '.chat-input-toolbar__left .segmented-control__option',
      narrowChatColumn,
      { 'padding-inline': '0.375rem' },
    )
    expectContainerDeclarations(inputCss, '.chat-input-footer', narrowChatColumn, {
      'padding-inline': '0.75rem 0.5rem',
    })
  })
})
