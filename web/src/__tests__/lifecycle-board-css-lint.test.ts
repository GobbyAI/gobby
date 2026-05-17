import { existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

const lifecycleStylesheet = join(process.cwd(), 'src/styles/lifecycle-board.css')
const lifecycleSources = [
  join(process.cwd(), 'src/components/tasks/LifecycleBoard.tsx'),
  join(process.cwd(), 'src/components/tasks/LifecycleLane.tsx'),
  join(process.cwd(), 'src/components/tasks/StageColumn.tsx'),
  join(process.cwd(), 'src/components/tasks/StageCard.tsx'),
  join(process.cwd(), 'src/components/tasks/lifecycleBoardStyles.ts'),
]

describe('Lifecycle board Tailwind contract', () => {
  it('test_no_legacy_lifecycle_stylesheet_or_import', () => {
    expect(existsSync(lifecycleStylesheet)).toBe(false)

    const offenders = lifecycleSources.filter(file =>
      readFileSync(file, 'utf8').includes('styles/lifecycle-board.css'),
    )
    expect(offenders).toEqual([])
  })

  it('test_no_accent_stripes_or_gradient_text', () => {
    const source = lifecycleSources.map(file => readFileSync(file, 'utf8')).join('\n')

    expect(source).not.toMatch(/(?:border-(?:left|right)|border(?:Left|Right))/)
    expect(source).not.toMatch(/gradient|bg-clip-text|text-transparent/)
  })
})
