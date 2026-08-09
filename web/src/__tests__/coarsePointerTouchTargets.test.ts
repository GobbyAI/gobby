import { afterEach, describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { compile } from '@tailwindcss/node'
import postcss from 'postcss'
import { buttonVariants } from '../components/ui/buttonVariants'
import { coarseHitAreaCls } from '../components/ui/controlStyles'

const targets = [
  'primary action',
  'queued-file remove',
  'code copy',
  'session actions',
  'task actions',
  'task expand',
  'activity menu item',
  'filter option',
  'session role filter',
  'error dismiss',
] as const

const srcRoot = dirname(dirname(fileURLToPath(import.meta.url)))

async function emulateCoarsePointer(): Promise<HTMLStyleElement> {
  const tailwind = await compile('@import "tailwindcss";', {
    base: srcRoot,
    onDependency() {},
  })
  const tailwindCss = tailwind.build([
    'flex',
    'h-4',
    'w-4',
    'h-[36px]',
    'w-[36px]',
    'pointer-coarse:h-11',
    'pointer-coarse:w-11',
    'pointer-coarse:min-h-11',
    'pointer-coarse:min-w-11',
  ])
  const cssSources = [
    tailwindCss,
    readFileSync(join(srcRoot, 'components/chat/styles/sessions-tab.css'), 'utf8'),
    readFileSync(join(srcRoot, 'components/chat/styles/activity-panel.css'), 'utf8'),
    readFileSync(join(srcRoot, 'components/tasks/task-execution.css'), 'utf8'),
    readFileSync(join(srcRoot, 'components/activity/taskdetail/task-detail.css'), 'utf8'),
  ]
  const coarseRules: string[] = []
  let spacing: string | undefined
  for (const css of cssSources) {
    const root = postcss.parse(css)
    root.walkDecls('--spacing', declaration => {
      spacing ??= declaration.value
    })
    root.walkAtRules('media', rule => {
      if (/pointer\s*:\s*coarse/.test(rule.params)) {
        if (rule.parent?.type === 'rule') {
          coarseRules.push(rule.parent.clone({ nodes: rule.nodes }).toString())
        } else {
          for (const nestedRule of rule.nodes ?? []) coarseRules.push(nestedRule.toString())
        }
      }
    })
  }

  expect(coarseRules.length).toBeGreaterThan(0)
  expect(spacing).toBeDefined()
  document.documentElement.style.setProperty('--spacing', spacing!)
  const style = document.createElement('style')
  style.dataset.testCoarsePointer = 'true'
  style.textContent = coarseRules.join('\n')
  document.head.append(style)
  return style
}

/**
 * Compile Tailwind for the given candidates and inject both the base rules
 * (so ladder min-heights resolve) and the unwrapped pointer-coarse rules
 * (appended last, so the coarse promotion wins where both match). JSDOM
 * ignores rules wrapped in @layer or @media, so both sets are flattened to
 * plain selector blocks before injection.
 */
async function emulateButtonStyles(candidates: readonly string[]): Promise<HTMLStyleElement> {
  const tailwind = await compile('@import "tailwindcss";', {
    base: srcRoot,
    onDependency() {},
  })
  const css = tailwind.build([...new Set(candidates)])
  const root = postcss.parse(css)
  let spacing: string | undefined
  root.walkDecls('--spacing', declaration => {
    spacing ??= declaration.value
  })

  const flatRules: string[] = []
  root.walkRules(rule => {
    // Keep rules whose only at-rule ancestors are @layer; conditional rules
    // (@media, @supports) are handled by the coarse extraction below.
    let conditional = false
    for (
      let parent: typeof rule.parent = rule.parent;
      parent && parent.type !== 'root';
      parent = parent.parent as typeof rule.parent
    ) {
      if (parent.type === 'atrule' && (parent as postcss.AtRule).name !== 'layer') {
        conditional = true
        break
      }
    }
    if (conditional) return
    const declarations = (rule.nodes ?? [])
      .filter(node => node.type === 'decl')
      .map(String)
      .join('; ')
    if (declarations) flatRules.push(`${rule.selector} { ${declarations} }`)
  })

  const coarseRules: string[] = []
  root.walkAtRules('media', rule => {
    if (/pointer\s*:\s*coarse/.test(rule.params)) {
      if (rule.parent?.type === 'rule') {
        coarseRules.push(rule.parent.clone({ nodes: rule.nodes }).toString())
      } else {
        for (const nestedRule of rule.nodes ?? []) coarseRules.push(nestedRule.toString())
      }
    }
  })

  expect(spacing).toBeDefined()
  document.documentElement.style.setProperty('--spacing', spacing!)
  const style = document.createElement('style')
  style.dataset.testCoarsePointer = 'true'
  style.textContent = [...flatRules, ...coarseRules].join('\n')
  document.head.append(style)
  return style
}

/**
 * Compile the shared control hit-area recipe and split it into the parts the
 * primitive contract cares about: base (unconditional) rules, coarse-pointer
 * rules that target the host element itself, and the declarations of the
 * coarse-pointer `::before` expansion. The pseudo declarations are re-homed
 * onto a `.pseudo-probe` class so a real element can measure the box jsdom
 * cannot compute for pseudo-elements.
 */
async function emulateHitAreaStyles(): Promise<{
  style: HTMLStyleElement
  coarseHostDeclarations: string[]
}> {
  const tailwind = await compile('@import "tailwindcss";', {
    base: srcRoot,
    onDependency() {},
  })
  const css = tailwind.build([...coarseHitAreaCls.split(/\s+/), 'h-9'])
  const root = postcss.parse(css)
  let spacing: string | undefined
  root.walkDecls('--spacing', declaration => {
    spacing ??= declaration.value
  })

  const baseRules: string[] = []
  root.walkRules(rule => {
    let conditional = false
    for (
      let parent: typeof rule.parent = rule.parent;
      parent && parent.type !== 'root';
      parent = parent.parent as typeof rule.parent
    ) {
      if (parent.type === 'atrule' && (parent as postcss.AtRule).name !== 'layer') {
        conditional = true
        break
      }
    }
    if (conditional || rule.selector.includes('::before')) return
    const declarations = (rule.nodes ?? [])
      .filter(node => node.type === 'decl')
      .map(String)
      .join('; ')
    if (declarations) baseRules.push(`${rule.selector} { ${declarations} }`)
  })

  const pseudoDeclarations: string[] = []
  const coarseHostDeclarations: string[] = []
  root.walkAtRules('media', atRule => {
    if (!/pointer\s*:\s*coarse/.test(atRule.params)) return
    atRule.walkRules(rule => {
      const declarations = (rule.nodes ?? [])
        .filter(node => node.type === 'decl')
        .map(String)
      if (rule.selector.includes('::before')) pseudoDeclarations.push(...declarations)
      else coarseHostDeclarations.push(...declarations)
    })
  })

  expect(spacing).toBeDefined()
  expect(pseudoDeclarations.length).toBeGreaterThan(0)
  document.documentElement.style.setProperty('--spacing', spacing!)
  const style = document.createElement('style')
  style.dataset.testCoarsePointer = 'true'
  style.textContent = [
    ...baseRules,
    `.pseudo-probe { ${pseudoDeclarations.join('; ')} }`,
  ].join('\n')
  document.head.append(style)
  return { style, coarseHostDeclarations }
}

function cssLengthToPixels(value: string): number {
  if (!value || value === 'auto') return 0
  const number = parseFloat(value)
  if (value.endsWith('px')) return number
  if (value.endsWith('rem')) return number * 16

  const spacingMultiple = value.match(
    /^calc\(var\(--spacing\)\s*\*\s*([\d.]+)\)$/,
  )?.[1]
  if (spacingMultiple) {
    const spacing = getComputedStyle(document.documentElement).getPropertyValue('--spacing')
    return cssLengthToPixels(spacing) * Number(spacingMultiple)
  }
  return 0
}

function computedFloor(style: CSSStyleDeclaration, axis: 'width' | 'height'): number {
  const minimum = axis === 'width' ? style.minWidth : style.minHeight
  return Math.max(cssLengthToPixels(style[axis]), cssLengthToPixels(minimum))
}

describe('coarse-pointer touch targets', () => {
  afterEach(() => {
    document.body.replaceChildren()
    document.querySelector('[data-test-coarse-pointer]')?.remove()
    document.documentElement.style.removeProperty('--spacing')
  })

  it('promotes compact chat and activity controls to at least 44px', async () => {
    await emulateCoarsePointer()
    document.body.innerHTML = `
      <button data-target="primary action" class="h-[36px] w-[36px] pointer-coarse:h-11 pointer-coarse:w-11">Send</button>
      <button data-target="queued-file remove" class="h-4 w-4 pointer-coarse:h-11 pointer-coarse:w-11">×</button>
      <button data-target="code copy" class="pointer-coarse:min-h-11 pointer-coarse:min-w-11">Copy</button>
      <button data-target="session actions" class="session-more-btn">⋮</button>
      <button data-target="task actions" class="task-more-btn">⋮</button>
      <button data-target="task expand" class="activity-task-row-toggle">›</button>
      <button data-target="activity menu item" class="activity-panel-mobile-menu__item">Sessions</button>
      <label data-target="filter option" class="flex pointer-coarse:min-h-11 pointer-coarse:min-w-11"><input type="checkbox"> Filter</label>
      <label data-target="session role filter" class="flex pointer-coarse:min-h-11 pointer-coarse:min-w-11"><input type="checkbox"> Parent</label>
      <button data-target="error dismiss" class="activity-task-detail-edit-error__dismiss">×</button>
    `

    for (const target of targets) {
      const element = document.querySelector<HTMLElement>(`[data-target="${target}"]`)
      expect(element).not.toBeNull()
      const style = getComputedStyle(element!)
      expect(computedFloor(style, 'width'), `${target} width`).toBeGreaterThanOrEqual(44)
      expect(computedFloor(style, 'height'), `${target} height`).toBeGreaterThanOrEqual(44)
    }
  })

  it('expands control primitives to an invisible ≥44×44 coarse hit area', async () => {
    await emulateHitAreaStyles()
    document.body.innerHTML = '<span class="pseudo-probe"></span>'
    const probe = getComputedStyle(document.querySelector('.pseudo-probe')!)
    // The expansion overlays the control (absolute, centered) instead of
    // growing it, and floors both axes at the 44px touch target.
    expect(probe.position).toBe('absolute')
    expect(computedFloor(probe, 'width')).toBeGreaterThanOrEqual(44)
    expect(computedFloor(probe, 'height')).toBeGreaterThanOrEqual(44)
  })

  it('keeps the visible control box on the 36px ladder under coarse pointers', async () => {
    const { coarseHostDeclarations } = await emulateHitAreaStyles()
    // No coarse-pointer rule may size the host element itself — the whole
    // point of the pseudo-element expansion is unchanged rendered visuals.
    for (const declaration of coarseHostDeclarations) {
      expect(declaration).not.toMatch(/(?:^|\s)(?:min-)?(?:width|height)\s*:/)
    }
    document.body.innerHTML = `<input class="${coarseHitAreaCls} h-9">`
    const host = getComputedStyle(document.querySelector('input')!)
    expect(computedFloor(host, 'height')).toBe(36)
    expect(computedFloor(host, 'height')).toBeLessThan(44)
  })

  it('promotes every Button size to 44px on coarse pointers unless dense', async () => {
    const sizes = ['sm', 'md', 'lg', 'icon'] as const
    const denseStates = [false, true] as const
    const ladder: Record<(typeof sizes)[number], number> = { sm: 28, md: 32, lg: 40, icon: 32 }

    const candidates = sizes.flatMap(size =>
      denseStates.flatMap(dense =>
        buttonVariants({ variant: 'secondary', size, dense }).split(/\s+/),
      ),
    )
    await emulateButtonStyles(candidates)

    for (const size of sizes) {
      for (const dense of denseStates) {
        document.body.innerHTML = `<button class="${buttonVariants({ variant: 'secondary', size, dense })}">Go</button>`
        const style = getComputedStyle(document.body.querySelector('button')!)
        const height = computedFloor(style, 'height')
        const width = computedFloor(style, 'width')
        if (dense) {
          expect(height, `${size} dense keeps the ladder height`).toBe(ladder[size])
          expect(height, `${size} dense must not promote`).toBeLessThan(44)
          if (size === 'icon') expect(width, 'icon dense width').toBe(ladder.icon)
        } else {
          expect(height, `${size} height`).toBeGreaterThanOrEqual(44)
          expect(width, `${size} width`).toBeGreaterThanOrEqual(44)
        }
      }
    }
  })
})
