import { existsSync, readFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { compile } from '@tailwindcss/node'
import postcss from 'postcss'
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
  if (ts.isBinaryExpression(expression)) {
    return [
      ...expressionTextParts(expression.left),
      ...expressionTextParts(expression.right),
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

async function injectUtilityStyles(
  candidates: readonly string[],
  includeCoarsePointer = false,
): Promise<HTMLStyleElement> {
  const tailwind = await compile('@import "tailwindcss";', {
    base: join(resolveWebPackageRoot(), 'src'),
    onDependency() {},
  })
  const root = postcss.parse(tailwind.build([...new Set(candidates)]))
  const rules: string[] = []
  let spacing: string | undefined

  root.walkDecls('--spacing', declaration => {
    spacing ??= declaration.value
  })
  root.walkRules(rule => {
    let conditional = false
    for (
      let parent = rule.parent;
      parent && parent.type !== 'root';
      parent = parent.parent
    ) {
      if (parent.type === 'atrule' && parent.name !== 'layer') conditional = true
    }
    if (conditional) return
    const declarations = (rule.nodes ?? [])
      .filter(node => node.type === 'decl')
      .map(String)
      .join('; ')
    if (declarations) rules.push(`${rule.selector} { ${declarations} }`)
  })
  if (includeCoarsePointer) {
    root.walkAtRules('media', rule => {
      if (!/pointer\s*:\s*coarse/.test(rule.params)) return
      if (rule.parent?.type === 'rule') {
        rules.push(rule.parent.clone({ nodes: rule.nodes }).toString())
      } else {
        for (const nestedRule of rule.nodes ?? []) rules.push(nestedRule.toString())
      }
    })
  }

  expect(spacing).toBeDefined()
  document.documentElement.style.setProperty('--spacing', spacing!)
  const style = document.createElement('style')
  style.textContent = rules.join('\n')
  document.head.append(style)
  return style
}

function cssLengthToPixels(value: string): number {
  if (!value || value === 'auto') return 0
  const number = Number.parseFloat(value)
  if (value.endsWith('px')) return number
  if (value.endsWith('rem')) return number * 16

  const spacingMultiple = value.match(
    /^calc\(var\(--spacing\)\s*\*\s*([\d.]+)\)$/,
  )?.[1]
  if (!spacingMultiple) return 0
  const spacing = getComputedStyle(document.documentElement).getPropertyValue('--spacing')
  return cssLengthToPixels(spacing) * Number(spacingMultiple)
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

  it('loads the app without the retired shell, primitive, and settings sheets', () => {
    const source = readSource('src/main.tsx')
    const imports = importSpecifiers(source)

    expect(existsSync(join(resolveWebPackageRoot(), 'src/styles/app-shell.css'))).toBe(false)
    expect(imports).not.toContain('./styles/app-shell.css')
    for (const sheet of ['segmented-control.css', 'dropdown-caret.css']) {
      expect(existsSync(join(resolveWebPackageRoot(), 'src/styles', sheet))).toBe(false)
      expect(imports).not.toContain(`./styles/${sheet}`)
    }
    expect(imports).not.toContain('./styles/settings.css')
  })

  it('retires the small activity tab sheets and every owner import', () => {
    const retiredSheets = [
      'src/components/activity/skills/SkillsTab.css',
      'src/components/chat/styles/cron-tab.css',
      'src/components/chat/styles/files-tab.css',
      'src/components/chat/styles/mcp-tab.css',
      'src/components/chat/styles/pipelines-tab.css',
      'src/components/chat/styles/rules-tab.css',
      'src/components/chat/styles/traces-tab.css',
    ] as const
    const retiredImports = [
      ['src/components/activity/ActivityMcpTab.tsx', '../chat/styles/mcp-tab.css'],
      ['src/components/activity/RulesTab.tsx', '../chat/styles/rules-tab.css'],
      ['src/components/activity/FilesTab.tsx', '../chat/styles/files-tab.css'],
      ['src/components/activity/CronTab.tsx', '../chat/styles/cron-tab.css'],
      ['src/components/activity/TracesTab.tsx', '../chat/styles/traces-tab.css'],
      ['src/components/activity/PipelinesTab.tsx', '../chat/styles/pipelines-tab.css'],
      ['src/components/activity/SkillsTab.tsx', '../chat/styles/rules-tab.css'],
      ['src/components/activity/SkillsTab.tsx', './skills/SkillsTab.css'],
      [
        'src/components/activity/integrations/IntegrationsFilterPanel.tsx',
        '../../chat/styles/rules-tab.css',
      ],
    ] as const

    for (const sheet of retiredSheets) {
      expect(existsSync(join(resolveWebPackageRoot(), sheet))).toBe(false)
    }
    for (const [owner, stylesheet] of retiredImports) {
      expect(importSpecifiers(readSource(owner))).not.toContain(stylesheet)
    }

    const chatPageImports = importSpecifiers(readSource('src/components/chat/ChatPage.tsx'))
    expect(chatPageImports).not.toContain('./styles.css')
  })

  it('retires the activity panel and sessions sheets in favor of component utilities', () => {
    const activityPanelSource = readSource('src/components/activity/ActivityPanel.tsx')
    const sessionsTabSource = readSource('src/components/activity/SessionsTab.tsx')

    for (const sheet of ['activity-panel.css', 'sessions-tab.css']) {
      expect(
        existsSync(join(resolveWebPackageRoot(), 'src/components/chat/styles', sheet)),
      ).toBe(false)
    }
    expect(importSpecifiers(activityPanelSource)).not.toContain(
      '../chat/styles/activity-panel.css',
    )
    expect(importSpecifiers(sessionsTabSource)).not.toContain(
      '../chat/styles/sessions-tab.css',
    )
  })

  it('retires the task execution and detail sheets with their owner imports', () => {
    for (const sheet of [
      'src/components/tasks/task-execution.css',
      'src/components/activity/taskdetail/task-detail.css',
    ]) {
      expect(existsSync(join(resolveWebPackageRoot(), sheet))).toBe(false)
    }

    expect(
      importSpecifiers(readSource('src/components/tasks/TaskBadges.tsx')),
    ).not.toContain('./task-execution.css')
    expect(
      importSpecifiers(readSource('src/components/activity/TasksTabDetailPanel.tsx')),
    ).not.toContain('./taskdetail/task-detail.css')
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

  it('keeps the top app chrome compact on narrow screens and touch-sized for coarse pointers', async () => {
    const appSource = readSource('src/App.tsx')
    const projectSelectorSource = readSource('src/components/ProjectSelector.tsx')
    const segmentedControlSource = readSource('src/components/ui/SegmentedControl.tsx')
    const themeToggleSource = readSource('src/components/ThemeToggle.tsx')
    const activityActionsSource = readSource('src/components/activity/ActivityActionsContext.tsx')

    for (const token of [
      'relative',
      'z-[100]',
      'justify-between',
      'gap-3',
      'border-b',
      'border-border',
      'px-4',
      'py-3',
      '[@media(max-width:768px)]:gap-2',
      '[@media(max-width:768px)]:px-3',
      '[@media(max-width:768px)]:py-2.5',
      '[@media(max-width:768px)]:text-[length:var(--text-2xl)]',
      '[--control-row-height:var(--status-bar-control-height)]',
      'pointer-coarse:[--control-row-height:2.75rem]',
      'flex-nowrap',
      'pointer-coarse:min-w-11',
      '[--app-brand-logo-size:2.75rem]',
      '[@media(max-width:768px)]:[--app-brand-logo-size:1.875rem]',
    ]) {
      expectClassToken(appSource, token)
    }
    expectStringAttribute(appSource, 'aria-label', 'Log out')
    expectStringAttribute(appSource, 'size', 'var(--app-brand-logo-size)')
    for (const hook of [
      'app-header',
      'app-brand',
      'app-brand-logo',
      'app-brand-title',
      'app-header-actions',
      'app-health-badge',
      'app-settings-cog',
      'app-logout-btn',
    ]) {
      expectNoClassToken(appSource, hook)
    }

    expectStringAttribute(themeToggleSource, 'size', 'icon')
    expectClassToken(themeToggleSource, 'shrink-0')
    expectClassToken(themeToggleSource, 'pointer-coarse:min-w-11')
    expectNoClassToken(themeToggleSource, 'app-theme-toggle')

    for (const token of [
      'relative',
      'min-w-0',
      'mobile:w-25',
      'mobile:hidden',
      'hidden',
      'h-[var(--control-row-height)]',
      'min-h-[var(--control-row-height)]',
      'mobile:inline-flex',
      'w-full',
      'py-0',
      '[font-family:inherit]',
      'overflow-hidden',
      'text-ellipsis',
      'whitespace-nowrap',
    ]) {
      expectClassToken(projectSelectorSource, token)
    }
    expect(projectSelectorSource).not.toContain('coarseTouchTarget={false}')
    for (const hook of [
      'project-selector',
      'project-selector-segmented-wrap',
      'project-selector-compact-wrap',
      'project-selector-compact-trigger',
      'project-selector-compact-label',
    ]) {
      expectNoClassToken(projectSelectorSource, hook)
    }
    for (const token of [
      '[--segmented-option-px:0.75rem]',
      'text-[length:var(--text-base)]',
      'max-[768px]:[--segmented-option-px:0.55rem]',
      'max-[768px]:text-[length:var(--text-sm)]',
      'px-[var(--segmented-option-px)]',
      'pointer-coarse:min-h-11',
      'pointer-coarse:min-w-11',
    ]) {
      expectClassToken(segmentedControlSource, token)
    }
    expect(segmentedControlSource).toContain(
      "controlHeight === 'sm' ? 'var(--control-row-height-sm)' : 'var(--control-row-height)'",
    )
    expect(segmentedControlSource).toContain('style={{ height: heightVar }}')
    expect(activityActionsSource).toContain(
      '@max-[479px]/activity-panel:[&>.segmented-control__option]:[--segmented-option-px:0.5rem]',
    )
    const denseIconTokens = ['min-h-8', 'w-8', 'pointer-coarse:min-w-11'] as const
    document.body.innerHTML = `<button data-dense-header-icon class="${denseIconTokens.join(' ')}"></button>`
    const style = await injectUtilityStyles(denseIconTokens, true)
    const denseIconStyle = getComputedStyle(
      document.querySelector('[data-dense-header-icon]')!,
    )
    expect(cssLengthToPixels(denseIconStyle.width)).toBe(32)
    expect(cssLengthToPixels(denseIconStyle.minWidth)).toBe(44)
    expect(cssLengthToPixels(denseIconStyle.minHeight)).toBe(32)

    style.remove()
    document.body.replaceChildren()
    document.documentElement.style.removeProperty('--spacing')
  })

  it('keeps segmented-control and dropdown-caret computed geometry pixel-neutral', async () => {
    const segmentedControlSource = readSource('src/components/ui/SegmentedControl.tsx')
    const dropdownCaretSource = readSource('src/components/ui/DropdownCaret.tsx')
    const rootTokens = [
      'inline-flex',
      '[--segmented-option-px:0.75rem]',
      'text-[length:var(--text-base)]',
      'pointer-coarse:min-h-11',
    ] as const
    const optionTokens = [
      'px-[var(--segmented-option-px)]',
      'pointer-coarse:min-h-11',
      'pointer-coarse:min-w-11',
    ] as const
    const caretTokens = ['inline-flex', 'items-center', 'justify-center', 'shrink-0', 'opacity-70']

    for (const token of [...rootTokens, ...optionTokens]) {
      expectClassToken(segmentedControlSource, token)
    }
    for (const token of [...caretTokens, 'size-3']) {
      expectClassToken(dropdownCaretSource, token)
    }

    document.body.innerHTML = `
      <div data-segmented-root class="${rootTokens.join(' ')}">
        <button data-segmented-option class="${optionTokens.join(' ')}">One</button>
      </div>
      <span data-caret class="${caretTokens.join(' ')}"><svg class="size-3"></svg></span>
    `
    const candidates = [...rootTokens, ...optionTokens, ...caretTokens, 'size-3']
    const fineStyle = await injectUtilityStyles(candidates)
    const rootStyle = getComputedStyle(document.querySelector('[data-segmented-root]')!)
    const optionStyle = getComputedStyle(document.querySelector('[data-segmented-option]')!)
    const caretStyle = getComputedStyle(document.querySelector('[data-caret]')!)
    const caretSvgStyle = getComputedStyle(document.querySelector('[data-caret] svg')!)

    expect(rootStyle.getPropertyValue('--segmented-option-px').trim()).toBe('0.75rem')
    expect(rootStyle.fontSize).toBe('var(--text-base)')
    expect(optionStyle.paddingInline).toBe('var(--segmented-option-px)')
    expect(cssLengthToPixels(optionStyle.minWidth)).toBe(0)
    expect(cssLengthToPixels(optionStyle.minHeight)).toBe(0)
    expect(caretStyle.display).toBe('inline-flex')
    expect(caretStyle.alignItems).toBe('center')
    expect(caretStyle.justifyContent).toBe('center')
    expect(caretStyle.flexShrink).toBe('0')
    const caretOpacity = Number.parseFloat(caretStyle.opacity)
    expect(caretStyle.opacity.endsWith('%') ? caretOpacity / 100 : caretOpacity).toBe(0.7)
    expect(cssLengthToPixels(caretSvgStyle.width)).toBe(12)
    expect(cssLengthToPixels(caretSvgStyle.height)).toBe(12)

    fineStyle.remove()
    const coarseStyle = await injectUtilityStyles(candidates, true)
    const coarseRoot = getComputedStyle(document.querySelector('[data-segmented-root]')!)
    const coarseOption = getComputedStyle(document.querySelector('[data-segmented-option]')!)
    expect(cssLengthToPixels(coarseRoot.minHeight)).toBeGreaterThanOrEqual(44)
    expect(cssLengthToPixels(coarseOption.minWidth)).toBeGreaterThanOrEqual(44)
    expect(cssLengthToPixels(coarseOption.minHeight)).toBeGreaterThanOrEqual(44)

    coarseStyle.remove()
    document.body.replaceChildren()
    document.documentElement.style.removeProperty('--spacing')
  })

  it('keeps activity panel filter toolbars to one compact row', () => {
    const activityPanelSource = readSource('src/components/activity/ActivityPanel.tsx')
    const quickMenuSource = readSource('src/components/activity/QuickMenu.tsx')

    expect(activityPanelSource).toContain(
      '[&_.activity-panel-toolbar]:[--control-row-height-sm:var(--status-bar-control-height)]',
    )
    expect(activityPanelSource).toContain('[&_.activity-panel-toolbar]:flex-nowrap')
    expect(activityPanelSource).toContain('[&_.activity-panel-search]:flex-[1_1_9rem]')
    expect(activityPanelSource).toContain('[&_.activity-panel-search]:min-w-0')
    expect(activityPanelSource).toContain(
      '[&_.activity-panel-search]:min-h-[var(--control-row-height-sm)]',
    )
    expect(activityPanelSource).toContain(
      'pointer-coarse:[&_.activity-panel-search]:min-h-11',
    )
    expect(activityPanelSource).toContain('max-[768px]:min-h-11')
    expect(activityPanelSource).toContain(
      'pointer-coarse:[&_.activity-filter-dropdown__item]:min-h-11',
    )
    expect(quickMenuSource).toContain('pointer-coarse:min-h-11')
  })

  it('shows an accent focus-visible ring on activity panel search inputs', () => {
    const activityPanelSource = readSource('src/components/activity/ActivityPanel.tsx')

    expect(activityPanelSource).toContain(
      '[&_.activity-panel-search:focus-visible]:outline-2',
    )
    expect(activityPanelSource).toContain(
      '[&_.activity-panel-search:focus-visible]:outline-accent',
    )
    expect(activityPanelSource).toContain(
      '[&_.activity-panel-search:focus-visible]:outline-offset-1',
    )
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
