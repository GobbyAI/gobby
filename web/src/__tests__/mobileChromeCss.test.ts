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

function readCssSource(rel: string, seen = new Set<string>()): string {
  if (seen.has(rel)) return ''
  seen.add(rel)

  const source = readSource(rel)
  const baseDir = dirname(rel)
  return source.replace(
    /^@import\s+['"]([^'"]+)['"];\s*$/gm,
    (_statement: string, specifier: string) => readCssSource(join(baseDir, specifier), seen),
  )
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
  return postcss.parse(readCssSource(rel), { from: rel })
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

function hasVariantAncestor(rule: Rule, variant: string): boolean {
  let parent = rule.parent as CssParent | undefined
  while (parent) {
    if (parent.type === 'atrule' && parent.name === 'variant' && parent.params === variant) {
      return true
    }
    parent = parent.parent
  }
  return false
}

function findRule(root: Root, selector: string, media?: string): Rule {
  let found: Rule | undefined
  root.walkRules(rule => {
    if (found) return
    if (!rule.selectors.includes(selector)) return
    const mediaMatches = media ? hasMediaAncestor(rule, media) : !hasAnyMediaAncestor(rule)
    if (mediaMatches) found = rule
  })
  expect(found, `Expected CSS rule ${selector}${media ? ` in ${media}` : ''}`).toBeDefined()
  return found as Rule
}

function findVariantRule(root: Root, selector: string, variant: string): Rule {
  let found: Rule | undefined
  root.walkRules(selector, rule => {
    if (found) return
    if (hasVariantAncestor(rule, variant)) found = rule
  })
  expect(found, `Expected CSS rule ${selector} in @variant ${variant}`).toBeDefined()
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

function expectVariantDeclarations(
  root: Root,
  selector: string,
  variant: string,
  expected: Record<string, string>,
): void {
  const rule = findVariantRule(root, selector, variant)
  const declarations = new Map<string, string>()
  rule.walkDecls(declaration => {
    declarations.set(declaration.prop, declaration.value)
  })

  for (const [property, value] of Object.entries(expected)) {
    expect(declarations.get(property)).toBe(value)
  }
}

describe('mobile chrome CSS', () => {
  it('owns Tailwind from the app global stylesheet after retiring chat aliases', () => {
    const globalStyles = readSource('src/styles/index.css')
    const chatRoot = join(resolveWebPackageRoot(), 'src/components/chat')

    expect(globalStyles).toContain('@import "tailwindcss";')
    expect(globalStyles).toContain('@config "../../tailwind.config.ts";')
    expect(globalStyles).toContain('@import "./tailwind-theme.css";')
    for (const retiredSheet of ['styles.css', 'styles/layout.css', 'styles/variables.css']) {
      expect(existsSync(join(chatRoot, retiredSheet))).toBe(false)
    }
  })

  it('loads the app shell after segmented controls and omits the retired settings sheet', () => {
    const source = readSource('src/main.tsx')
    const imports = importSpecifiers(source)
    const segmentedControlIndex = imports.indexOf('./styles/segmented-control.css')
    const appShellIndex = imports.indexOf('./styles/app-shell.css')

    expect(segmentedControlIndex).toBeGreaterThanOrEqual(0)
    expect(appShellIndex).toBeGreaterThanOrEqual(0)
    expect(segmentedControlIndex).toBeLessThan(appShellIndex)
    expect(imports).not.toContain('./styles/settings.css')
  })

  it('loads activity styles from each standalone owner', () => {
    const ownershipPins = [
      ['src/components/activity/ActivityPanel.tsx', '../chat/styles/activity-panel.css'],
      ['src/components/activity/SessionsTab.tsx', '../chat/styles/sessions-tab.css'],
      ['src/components/activity/ActivityMcpTab.tsx', '../chat/styles/mcp-tab.css'],
      ['src/components/activity/RulesTab.tsx', '../chat/styles/rules-tab.css'],
      ['src/components/activity/FilesTab.tsx', '../chat/styles/files-tab.css'],
      ['src/components/activity/CronTab.tsx', '../chat/styles/cron-tab.css'],
      ['src/components/activity/TracesTab.tsx', '../chat/styles/traces-tab.css'],
      ['src/components/activity/PipelinesTab.tsx', '../chat/styles/pipelines-tab.css'],
      ['src/components/activity/SkillsTab.tsx', '../chat/styles/rules-tab.css'],
      [
        'src/components/activity/integrations/IntegrationsFilterPanel.tsx',
        '../../chat/styles/rules-tab.css',
      ],
    ] as const

    for (const [owner, stylesheet] of ownershipPins) {
      expect(importSpecifiers(readSource(owner))).toContain(stylesheet)
    }

    const chatPageImports = importSpecifiers(readSource('src/components/chat/ChatPage.tsx'))
    expect(chatPageImports).not.toContain('./styles.css')
  })

  it('retires the chat input stylesheet family in favor of component utilities', () => {
    const retiredSheets = [
      'input-base.css',
      'input-composer.css',
      'input-voice.css',
      'input-responsive.css',
      'input-status.css',
      'input.css',
    ]

    for (const sheet of retiredSheets) {
      expect(existsSync(join(resolveWebPackageRoot(), 'src/components/chat/styles', sheet))).toBe(
        false,
      )
    }
  })

  it('keeps the top app chrome compact on narrow screens and touch-sized for coarse pointers', () => {
    const appSource = readSource('src/App.tsx')
    const projectSelectorSource = readSource('src/components/ProjectSelector.tsx')
    const segmentedControlCss = parseCss('src/styles/segmented-control.css')
    const shellCss = parseCss('src/styles/app-shell.css')

    expectClassToken(appSource, 'app-header')
    expectClassToken(appSource, 'app-brand')
    expectStringAttribute(appSource, 'aria-label', 'Log out')
    expectClassToken(appSource, 'app-logout-btn')
    expectClassToken(appSource, 'app-brand-logo')
    expectClassToken(appSource, 'app-brand-title')
    expectClassToken(appSource, 'app-header-actions')
    expectClassToken(appSource, 'app-health-badge')
    expect(projectSelectorSource).toContain('coarseTouchTarget={false}')

    expectVariantDeclarations(
      shellCss,
      '.app-header-actions .project-selector-segmented-wrap',
      'mobile',
      { display: 'none' },
    )
    expectVariantDeclarations(
      shellCss,
      '.project-selector-compact-wrap',
      'mobile',
      { display: 'inline-flex' },
    )
    expectDeclarations(shellCss, '.project-selector-compact-trigger', {
      background: 'var(--accent-tint)',
      color: 'var(--accent)',
    })
    expectDeclarations(shellCss, '.project-selector-compact-wrap', {
      height: 'var(--control-row-height)',
      'min-height': 'var(--control-row-height)',
    })
    expectDeclarations(segmentedControlCss, '.segmented-control', {
      'font-size': 'var(--text-base)',
    })
    expectDeclarations(segmentedControlCss, '.segmented-control__option', {
      'padding-inline': '0.75rem',
    })
    expectDeclarations(shellCss, '.app-header-actions', {
      '--control-row-height': 'var(--status-bar-control-height)',
      'flex-wrap': 'nowrap',
    })
    expectDeclarations(
      shellCss,
      '.app-header-actions',
      { '--control-row-height': '2.75rem' },
      '(pointer: coarse)',
    )
    expectDeclarations(
      shellCss,
      '.app-header-actions .app-theme-toggle',
      { 'min-width': '2.75rem', 'min-height': '2.75rem' },
      '(pointer: coarse)',
    )
    expectDeclarations(
      shellCss,
      '.app-header-actions .segmented-control__option',
      { 'min-width': '2.75rem', 'min-height': '2.75rem' },
      '(pointer: coarse)',
    )
  })

  it('keeps activity panel filter toolbars to one compact row', () => {
    const activityCss = parseCss('src/components/chat/styles/activity-panel.css')
    const sessionsCss = parseCss('src/components/chat/styles/sessions-tab.css')

    expect(findRule(activityCss, '.activity-panel-toolbar').selector).toBe(
      '.activity-panel-toolbar',
    )
    expectDeclarations(activityCss, '.activity-panel-toolbar', {
      '--control-row-height-sm': 'var(--status-bar-control-height)',
      'flex-wrap': 'nowrap',
    })
    expectDeclarations(activityCss, '.activity-panel-search', {
      flex: '1 1 9rem',
      'min-width': '0',
      'min-height': 'var(--control-row-height-sm)',
    })
    expectDeclarations(
      activityCss,
      '.activity-panel-search',
      { 'min-height': '2.75rem' },
      '(pointer: coarse)',
    )
    expectDeclarations(
      activityCss,
      '.activity-panel-mobile-trigger',
      { 'min-height': '2.75rem' },
      '(pointer: coarse)',
    )
    expectDeclarations(
      activityCss,
      '.activity-filter-dropdown__item',
      { 'min-height': '2.75rem' },
      '(pointer: coarse)',
    )
    expectDeclarations(
      sessionsCss,
      '.quick-menu-item',
      { 'min-height': '2.75rem' },
      '(pointer: coarse)',
    )
  })

  it('keeps rules-tab layout responsive to geometry while coarse pointers size targets only', () => {
    const rulesCss = parseCss('src/components/chat/styles/rules-tab.css')

    expect(findVariantRule(rulesCss, '.rules-tab .activity-panel-toolbar', 'mobile').selector).toBe(
      '.rules-tab .activity-panel-toolbar',
    )
    expectVariantDeclarations(
      rulesCss,
      '.rules-tab .activity-panel-toolbar',
      'mobile',
      { 'flex-wrap': 'wrap' },
    )
    expectVariantDeclarations(
      rulesCss,
      '.rules-tab .activity-panel-search',
      'mobile',
      { 'flex-basis': '100%' },
    )
    expectNoDeclaration(
      rulesCss,
      '.rules-tab .activity-panel-toolbar',
      'flex-wrap',
      '(pointer: coarse)',
    )
    expectNoDeclaration(
      rulesCss,
      '.rules-tab .activity-panel-search',
      'flex-basis',
      '(pointer: coarse)',
    )
  })

  it('shows an accent focus-visible ring on activity panel search inputs', () => {
    const activityCss = parseCss('src/components/chat/styles/activity-panel.css')
    const focusOutlines: string[] = []

    findRule(activityCss, '.activity-panel-search:focus').walkDecls('outline', declaration => {
      focusOutlines.push(declaration.value)
    })

    expect(focusOutlines).toEqual([])
    expectDeclarations(activityCss, '.activity-panel-search:focus-visible', {
      outline: '2px solid var(--accent)',
      'outline-offset': '1px',
    })
  })

  it('keeps the minimum-width chat status bar to one row', () => {
    const statusBarSource = readSource('src/components/chat/AgentStatusBar.tsx')

    expect(statusBarSource).toContain('@max-[360px]/chat-column:flex-nowrap')
    expect(statusBarSource).toContain('@max-[360px]/chat-column:hidden')
  })

  it('right-aligns command and status bar action slots', () => {
    const commandBarSource = readSource('src/components/chat/CommandBar.tsx')
    const mainColumnSource = readSource('src/components/chat/ChatMainColumn.tsx')
    const statusBarSource = readSource('src/components/chat/AgentStatusBar.tsx')

    expectClassToken(commandBarSource, 'px-3')
    expectClassToken(statusBarSource, 'px-3')
    expect(mainColumnSource).toContain('@max-[360px]/chat-column:[&_.command-bar]:pl-3')
    expect(mainColumnSource).toContain('@max-[360px]/chat-column:[&_.command-bar]:pr-2')
  })

  it('keeps the minimum-width chat input toolbar controls to one row', () => {
    const chatInputSource = readSource('src/components/chat/ChatInput.tsx')
    const toolbarSource = readSource('src/components/chat/ChatInputToolbar.tsx')
    const voiceControlsSource = readSource('src/components/chat/ChatInputVoiceControls.tsx')
    const mainColumnSource = readSource('src/components/chat/ChatMainColumn.tsx')
    const narrowHookSource = readSource('src/components/chat/useChatInputNarrow.ts')
    const modeSelectorSource = readSource('src/components/chat/ModeSelector.tsx')
    const segmentedControlSource = readSource('src/components/ui/SegmentedControl.tsx')
    const agentIndicatorSource = readSource('src/components/chat/ActiveAgentIndicator.tsx')

    expectClassToken(chatInputSource, 'chat-input-footer')
    expectClassToken(chatInputSource, 'py-3')
    expectNoClassToken(segmentedControlSource, 'px-2')
    expectNoClassToken(segmentedControlSource, 'px-3')
    expect(modeSelectorSource).toContain('controlHeight="sm"')
    expect(modeSelectorSource).toContain('coarseTouchTarget={false}')
    expectClassToken(agentIndicatorSource, 'chat-input-agent-button')
    expectClassToken(agentIndicatorSource, 'rounded')
    expectClassToken(agentIndicatorSource, 'p-1.5')
    expectClassToken(agentIndicatorSource, 'size-9')
    expectClassToken(chatInputSource, 'px-4')
    expect(toolbarSource).toContain('[--control-row-height-sm:var(--status-bar-control-height)]')
    expect(toolbarSource).toContain('@max-[360px]/chat-column:flex-nowrap')
    expect(toolbarSource).toContain('@max-[360px]/chat-column:[&_button[role=radio]]:px-1.5')
    expect(voiceControlsSource).toContain('gap-1')
    expect(mainColumnSource).toContain('@container/chat-column')
    expect(mainColumnSource).toContain('data-chat-column')
    expect(narrowHookSource).toContain("closest('[data-chat-column]')")
  })
})
