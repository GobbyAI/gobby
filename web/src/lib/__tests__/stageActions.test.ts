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
      /export\s+type\s+StageAdvanceAction\s*=[\s\S]*['"]start['"][\s\S]*['"]submit_for_review['"][\s\S]*['"]approve_review['"][\s\S]*['"]reject_review['"][\s\S]*['"]complete['"]/,
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

  it('test_optimistic_move_matches_backend_row_rules', async () => {
    const { canonicalBoardStage, optimisticMoveTaskToStage } = await loadStageActions()
    const lifecycleTask = {
      id: 'task-1',
      title: 'Build task',
      task_type: 'feature',
      stages: [
        {
          name: 'build',
          display_name: 'Build',
          category: 'delivery',
          state: 'done',
          review_policy: 'required',
          position: 0,
          updated_at: '2026-05-02T00:00:00Z',
        },
        {
          name: 'deploy',
          display_name: 'Deploy',
          category: 'release',
          state: 'done',
          review_policy: 'optional',
          position: 1,
          completed_commit_sha: 'abc123',
          artifact_refs: { result: 'stale' },
          notes: 'stale',
          updated_at: '2026-05-02T00:00:00Z',
        },
      ],
    }

    const moved = optimisticMoveTaskToStage(
      lifecycleTask,
      'deploy',
      '2026-05-03T00:00:00Z',
    )

    expect(
      moved.stages.map((stage: { name: string; state: string }) => [stage.name, stage.state]),
    ).toEqual([
      ['build', 'done'],
      ['deploy', 'ready'],
    ])
    expect(moved.stages[1]).toMatchObject({
      completed_commit_sha: null,
      artifact_refs: null,
      notes: null,
      updated_at: '2026-05-03T00:00:00Z',
    })
    expect(canonicalBoardStage(moved)?.name).toBe('deploy')
  })

  it('test_optimistic_move_clears_backend_task_reset_fields', async () => {
    const { optimisticMoveTaskToStage } = await loadStageActions()
    const moved = optimisticMoveTaskToStage(
      {
        id: 'task-1',
        title: 'Build task',
        task_type: 'feature',
        assignee: 'agent-1',
        claimed_by_session_id: 'session-1',
        closed_at: '2026-05-01T00:00:00Z',
        closed_reason: 'completed',
        closed_in_session_id: 'session-1',
        closed_commit_sha: 'abc123',
        escalated_at: '2026-05-01T00:00:00Z',
        escalation_reason: 'blocked',
        is_escalated: true,
        validation_fail_count: 3,
        dispatch_failure_count: 2,
        state: {
          owner_session_id: 'session-1',
          owner_session_ref: { session_id: 'session-1', ref: '#9', source: 'codex' },
          is_claimed: true,
          is_closed: true,
          is_escalated: true,
          closed_at: '2026-05-01T00:00:00Z',
          closed_reason: 'completed',
          closed_in_session_id: 'session-1',
          closed_commit_sha: 'abc123',
          escalated_at: '2026-05-01T00:00:00Z',
          escalation_reason: 'blocked',
        },
        stages: [
          {
            name: 'build',
            display_name: 'Build',
            category: 'delivery',
            state: 'done',
            review_policy: 'required',
            position: 0,
            updated_at: '2026-05-02T00:00:00Z',
          },
        ],
      },
      'build',
      '2026-05-03T00:00:00Z',
    )

    expect(moved).toMatchObject({
      assignee: null,
      claimed_by_session_id: null,
      closed_at: null,
      closed_reason: null,
      closed_in_session_id: null,
      closed_commit_sha: null,
      escalated_at: null,
      escalation_reason: null,
      is_escalated: false,
      validation_fail_count: 0,
      dispatch_failure_count: 0,
      state: {
        owner_session_id: null,
        owner_session_ref: null,
        is_claimed: false,
        is_closed: false,
        is_escalated: false,
        closed_at: null,
        closed_reason: null,
        closed_in_session_id: null,
        closed_commit_sha: null,
        escalated_at: null,
        escalation_reason: null,
      },
    })
  })

  it('test_optimistic_move_exposes_typed_result_without_generic_cast', () => {
    const source = readStageActionsSource()

    expect(source).toMatch(/export\s+type\s+OptimisticMoveResult\b/)
    expect(source).not.toMatch(/as\s+T\b/)
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
