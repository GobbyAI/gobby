import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import { describe, expect, it } from 'vitest'

const stageActionsPath = join(process.cwd(), 'src/lib/stageActions.ts')

async function loadStageActions() {
  const modulePath = '../stageActions'
  return import(/* @vite-ignore */ modulePath)
}

function readStageActionsSource() {
  expect(existsSync(stageActionsPath)).toBe(true)
  return readFileSync(stageActionsPath, 'utf8')
}

function sourceFiles(dir: string): string[] {
  return readdirSync(dir).flatMap(entry => {
    const path = join(dir, entry)
    if (path.includes('node_modules')) return []
    if (statSync(path).isDirectory()) return sourceFiles(path)
    if (!/\.(ts|tsx)$/.test(path)) return []
    return [path]
  })
}

describe('stageActions shared helper contract', () => {
  it('test_resolve_advance_required_chain', async () => {
    const { resolveAdvanceAction } = await loadStageActions()

    expect(resolveAdvanceAction('ready', 'required')).toBe('start')
    expect(resolveAdvanceAction('in_progress', 'required')).toBe('submit_for_review')
    expect(resolveAdvanceAction('needs_review', 'required')).toBe('approve_review')
    expect(resolveAdvanceAction('review_approved', 'required')).toBe('complete')
  })

  it('test_resolve_advance_none_chain', async () => {
    const { resolveAdvanceAction } = await loadStageActions()

    expect(resolveAdvanceAction('ready', 'none')).toBe('start')
    expect(resolveAdvanceAction('in_progress', 'none')).toBe('complete')
  })

  it('test_resolve_advance_optional_chain', async () => {
    const { resolveAdvanceAction } = await loadStageActions()

    expect(resolveAdvanceAction('ready', 'optional')).toBe('start')
    expect(resolveAdvanceAction('in_progress', 'optional')).toBe('complete')
  })

  it('test_resolve_advance_returns_null_on_done', async () => {
    const { resolveAdvanceAction } = await loadStageActions()

    expect(resolveAdvanceAction('done', 'required')).toBeNull()
    expect(resolveAdvanceAction('done', 'none')).toBeNull()
    expect(resolveAdvanceAction('done', 'optional')).toBeNull()
  })

  it('test_stage_row_state_aliases_stage_state5', () => {
    const source = readStageActionsSource()

    expect(source).toMatch(
      /export\s+type\s+StageState5\s*=\s*['"]ready['"]\s*\|\s*['"]in_progress['"]\s*\|\s*['"]needs_review['"]\s*\|\s*['"]review_approved['"]\s*\|\s*['"]done['"]/,
    )
    expect(source).toMatch(/export\s+type\s+StageRowState\s*=\s*StageState5\b/)
  })

  it('test_stage_state_view_shape', () => {
    const source = readStageActionsSource()

    expect(source).toMatch(/export\s+interface\s+StageStateView\s*{[\s\S]*name:\s*string/)
    expect(source).toMatch(/StageStateView\s*{[\s\S]*display_name:\s*string/)
    expect(source).toMatch(/StageStateView\s*{[\s\S]*category:\s*string/)
    expect(source).toMatch(/StageStateView\s*{[\s\S]*state:\s*StageState5/)
    expect(source).toMatch(/StageStateView\s*{[\s\S]*review_policy:\s*ReviewPolicy/)
    expect(source).toMatch(/StageStateView\s*{[\s\S]*updated_at:\s*string\s*\|\s*null/)
  })

  it('test_lifecycle_task_minimal_shape', () => {
    const source = readStageActionsSource()

    expect(source).toMatch(/export\s+interface\s+LifecycleTask\s*{[\s\S]*id:\s*string/)
    expect(source).toMatch(/LifecycleTask\s*{[\s\S]*title:\s*string/)
    expect(source).toMatch(/LifecycleTask\s*{[\s\S]*task_type:\s*string/)
    expect(source).toMatch(/LifecycleTask\s*{[\s\S]*stages:\s*StageStateView\[\]/)
  })

  it('test_stage_advance_action_union', () => {
    const source = readStageActionsSource()

    expect(source).toMatch(
      /export\s+type\s+StageAdvanceAction\s*=\s*['"]start['"]\s*\|\s*['"]submit_for_review['"]\s*\|\s*['"]approve_review['"]\s*\|\s*['"]complete['"]/,
    )
  })

  it('test_task_helpers_consume_lifecycle_task', async () => {
    const { currentStage, taskAtStage, taskStateAt } = await loadStageActions()
    const lifecycleTask = {
      id: 'task-1',
      title: 'Build task',
      task_type: 'feature',
      stages: [
        {
          name: 'build',
          display_name: 'Build',
          category: 'delivery',
          state: 'in_progress',
          review_policy: 'required',
          updated_at: '2026-05-02T00:00:00Z',
        },
      ],
    }

    expect(taskAtStage(lifecycleTask, 'build')).toBe(true)
    expect(taskAtStage(lifecycleTask, 'deploy')).toBe(false)
    expect(taskStateAt(lifecycleTask, 'build')).toBe('in_progress')
    expect(currentStage(lifecycleTask)).toEqual(lifecycleTask.stages[0])
  })

  it('test_module_is_only_authoring_site', () => {
    readStageActionsSource()

    const forbiddenExports = [
      /export\s+function\s+resolveAdvanceAction\b/,
      /export\s+type\s+StageAdvanceAction\b/,
      /export\s+interface\s+StageStateView\b/,
      /export\s+interface\s+LifecycleTask\b/,
    ]
    const offenders = sourceFiles(join(process.cwd(), 'src'))
      .filter(file => file !== stageActionsPath)
      .flatMap(file => {
        const source = readFileSync(file, 'utf8')
        return forbiddenExports.some(pattern => pattern.test(source))
          ? [relative(process.cwd(), file)]
          : []
      })

    expect(offenders).toEqual([])
  })
})
