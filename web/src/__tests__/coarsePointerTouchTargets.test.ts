import { afterEach, describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { compile } from '@tailwindcss/node'
import postcss from 'postcss'

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
})
