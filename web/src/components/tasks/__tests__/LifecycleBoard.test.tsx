import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const dnd = vi.hoisted(() => ({
  draggables: [] as Array<Record<string, any>>,
  dropTargets: [] as Array<Record<string, any>>,
  monitors: [] as Array<Record<string, any>>,
}))

vi.mock('@atlaskit/pragmatic-drag-and-drop/element/adapter', () => ({
  draggable: vi.fn((args: Record<string, any>) => {
    dnd.draggables.push(args)
    return vi.fn()
  }),
  dropTargetForElements: vi.fn((args: Record<string, any>) => {
    dnd.dropTargets.push(args)
    return vi.fn()
  }),
  monitorForElements: vi.fn((args: Record<string, any>) => {
    dnd.monitors.push(args)
    return vi.fn()
  }),
}))

type StageState =
  | 'ready'
  | 'in_progress'
  | 'needs_review'
  | 'review_approved'
  | 'done'
type ReviewPolicy = 'required' | 'none' | 'optional'

interface StageFixture {
  name: string
  display_name: string
  category: string
  state: StageState
  review_policy: ReviewPolicy
  updated_at: string
  position?: number
}

const registry = [
  {
    name: 'build',
    display_name: 'Build',
    category: 'delivery',
    review_policy: 'required',
    sequence_order: 10,
  },
  {
    name: 'test',
    display_name: 'Test',
    category: 'quality',
    review_policy: 'none',
    sequence_order: 20,
  },
  {
    name: 'deploy',
    display_name: 'Deploy',
    category: 'release',
    review_policy: 'optional',
    sequence_order: 30,
  },
] as const

function stage(
  name: string,
  state: StageState,
  reviewPolicy: ReviewPolicy = 'required',
): StageFixture {
  const entry = registry.find(item => item.name === name)
  return {
    name,
    display_name: entry?.display_name ?? name,
    category: entry?.category ?? 'delivery',
    state,
    review_policy: reviewPolicy,
    updated_at: '2026-05-02T00:00:00Z',
  }
}

function task(
  id: string,
  title: string,
  taskType: string,
  stages: ReturnType<typeof stage>[],
  overrides: Record<string, unknown> = {},
) {
  return {
    id,
    title,
    task_type: taskType,
    stages,
    ...overrides,
  }
}

async function loadLifecycleBoard() {
  const modulePath = '../LifecycleBoard'
  return import(/* @vite-ignore */ modulePath)
}

async function renderBoard(
  tasks: ReturnType<typeof task>[],
  onMoveTaskToStage = vi.fn(),
) {
  const { LifecycleBoard } = await loadLifecycleBoard()
  render(
    <LifecycleBoard
      tasks={tasks}
      stagesRegistry={registry}
      onSelectTask={vi.fn()}
      onMoveTaskToStage={onMoveTaskToStage}
    />,
  )
  return { onMoveTaskToStage }
}

function dropCard(taskId: string, sourceStageName: string, targetStageName: string) {
  dnd.monitors[dnd.monitors.length - 1]?.onDrop?.({
    source: {
      data: {
        type: 'lifecycle-stage-card',
        taskId,
        sourceStageName,
      },
    },
    location: {
      current: {
        dropTargets: [
          {
            data: {
              type: 'lifecycle-stage-column',
              stageName: targetStageName,
            },
          },
        ],
      },
    },
  })
}

describe('LifecycleBoard Phase 6 contracts', () => {
  beforeEach(() => {
    dnd.draggables.length = 0
    dnd.dropTargets.length = 0
    dnd.monitors.length = 0
  })

  it('test_registry_columns_remain_visible_as_empty_drop_targets', async () => {
    await renderBoard([
      task('task-1', 'Build manifest', 'feature', [stage('build', 'ready')]),
      task('task-2', 'Run tests', 'bug', [stage('test', 'in_progress', 'none')]),
    ])

    expect(screen.getAllByRole('region', { name: /build/i })).toHaveLength(2)
    expect(screen.getAllByRole('region', { name: /test/i })).toHaveLength(2)
    expect(screen.getAllByRole('region', { name: /deploy/i })).toHaveLength(2)
    expect(dnd.dropTargets.some(target => target.getData().stageName === 'deploy')).toBe(true)
  })

  it('test_retired_stage_is_hidden_from_columns', async () => {
    const { LifecycleBoard } = await loadLifecycleBoard()
    render(
      <LifecycleBoard
        tasks={[
          task('task-1', 'Build manifest', 'feature', [
            stage('build', 'ready'),
            {
              name: 'test_arch',
              display_name: 'Test Architecture',
              category: 'quality',
              state: 'ready',
              review_policy: 'required',
              updated_at: '2026-05-02T00:00:00Z',
            },
          ]),
        ]}
        stagesRegistry={[
          ...registry,
          {
            name: 'test_arch',
            display_name: 'Test Architecture',
            category: 'quality',
            review_policy: 'required',
            sequence_order: 15,
          },
        ]}
        onSelectTask={vi.fn()}
        onAdvanceStage={vi.fn()}
      />,
    )

    expect(screen.getByRole('region', { name: /build/i })).toBeTruthy()
    expect(screen.queryByRole('region', { name: /test architecture/i })).toBeNull()
  })

  it('test_hide_blocked_toggle_persists', async () => {
    window.localStorage.clear()
    const blockedTask = task('task-1', 'Blocked task', 'bug', [stage('build', 'ready')], {
      is_blocked: true,
      blocked_reason: 'Blocked by #42',
    })
    const openTask = task('task-2', 'Open task', 'feature', [stage('build', 'ready')])

    const { LifecycleBoard } = await loadLifecycleBoard()
    const { rerender } = render(
      <LifecycleBoard
        tasks={[blockedTask, openTask]}
        stagesRegistry={registry}
        onSelectTask={vi.fn()}
        onAdvanceStage={vi.fn()}
      />,
    )

    expect(screen.getByText('Blocked task')).toBeTruthy()

    await userEvent.click(screen.getByRole('switch', { name: /hide blocked/i }))

    expect(screen.queryByText('Blocked task')).toBeNull()
    expect(screen.getByText('Open task')).toBeTruthy()
    expect(window.localStorage.getItem('lifecycle-board:hide-blocked')).toBe('true')

    rerender(
      <LifecycleBoard
        tasks={[blockedTask, openTask]}
        stagesRegistry={registry}
        onSelectTask={vi.fn()}
        onAdvanceStage={vi.fn()}
      />,
    )

    expect(screen.queryByText('Blocked task')).toBeNull()
  })

  it('test_one_canonical_card_per_task_at_current_stage', async () => {
    await renderBoard([
      task('task-1', 'Move me once', 'feature', [
        { ...stage('build', 'done'), position: 0 },
        { ...stage('test', 'ready', 'none'), position: 1 },
        { ...stage('deploy', 'ready', 'optional'), position: 2 },
      ]),
    ])

    expect(screen.getAllByText('Move me once')).toHaveLength(1)
    expect(within(screen.getByRole('region', { name: /test/i })).getByText('Move me once')).toBeTruthy()
    expect(within(screen.getByRole('region', { name: /build/i })).queryByText('Move me once')).toBeNull()
  })

  it('test_fully_done_task_renders_in_final_stage_done_group', async () => {
    await renderBoard([
      task('task-1', 'Done once', 'feature', [
        { ...stage('build', 'done'), position: 0 },
        { ...stage('test', 'done', 'none'), position: 1 },
        { ...stage('deploy', 'done', 'optional'), position: 2 },
      ]),
    ])

    expect(screen.getAllByText('Done once')).toHaveLength(1)
    expect(within(screen.getByRole('region', { name: /deploy/i })).getByText('Done once')).toBeTruthy()
  })

  it('test_drop_forward_calls_move_to_stage', async () => {
    const onMoveTaskToStage = vi.fn()
    await renderBoard(
      [task('task-1', 'Ready task', 'feature', [stage('build', 'ready')])],
      onMoveTaskToStage,
    )

    dropCard('task-1', 'build', 'deploy')

    expect(onMoveTaskToStage).toHaveBeenCalledWith('task-1', 'deploy')
  })

  it('test_drop_backward_calls_move_to_stage', async () => {
    const onMoveTaskToStage = vi.fn()
    await renderBoard(
      [
        task('task-1', 'Review task', 'feature', [
          { ...stage('build', 'done'), position: 0 },
          { ...stage('test', 'done', 'none'), position: 1 },
          { ...stage('deploy', 'ready', 'optional'), position: 2 },
        ]),
      ],
      onMoveTaskToStage,
    )

    dropCard('task-1', 'deploy', 'test')

    expect(onMoveTaskToStage).toHaveBeenCalledWith('task-1', 'test')
  })

  it('test_same_stage_drop_is_noop', async () => {
    const onMoveTaskToStage = vi.fn()
    await renderBoard(
      [task('task-1', 'Ready task', 'feature', [stage('build', 'ready')])],
      onMoveTaskToStage,
    )

    dropCard('task-1', 'build', 'build')

    expect(onMoveTaskToStage).not.toHaveBeenCalled()
  })

  it('test_pending_move_guard_dedupes_task_moves', async () => {
    let resolveMove!: () => void
    const onMoveTaskToStage = vi.fn(() => new Promise<void>(resolve => {
      resolveMove = resolve
    }))
    await renderBoard(
      [task('task-1', 'Ready task', 'feature', [stage('build', 'ready')])],
      onMoveTaskToStage,
    )

    dropCard('task-1', 'build', 'test')
    dropCard('task-1', 'build', 'deploy')
    expect(onMoveTaskToStage).toHaveBeenCalledTimes(1)

    resolveMove()
    await waitFor(() => {
      dropCard('task-1', 'build', 'deploy')
      expect(onMoveTaskToStage).toHaveBeenCalledTimes(2)
    })
  })

  it('test_move_failure_logs_context_and_clears_pending_guard', async () => {
    const moveError = new Error('move failed')
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    const onMoveTaskToStage = vi
      .fn()
      .mockRejectedValueOnce(moveError)
      .mockResolvedValueOnce(undefined)
    await renderBoard(
      [task('task-1', 'Ready task', 'feature', [stage('build', 'ready')])],
      onMoveTaskToStage,
    )

    dropCard('task-1', 'build', 'test')

    await waitFor(() => {
      expect(consoleError).toHaveBeenCalledWith('Failed to move lifecycle task to stage', {
        taskId: 'task-1',
        targetStageName: 'test',
        error: moveError,
      })
    })

    dropCard('task-1', 'build', 'deploy')

    await waitFor(() => {
      expect(onMoveTaskToStage).toHaveBeenCalledTimes(2)
    })
  })

  it('test_keyboard_move_fallback_calls_move_to_stage', async () => {
    const onMoveTaskToStage = vi.fn()
    await renderBoard(
      [task('task-1', 'Ready task', 'feature', [stage('build', 'ready')])],
      onMoveTaskToStage,
    )

    await userEvent.selectOptions(
      screen.getByLabelText(/move ready task to stage/i),
      'deploy',
    )

    expect(onMoveTaskToStage).toHaveBeenCalledWith('task-1', 'deploy')
  })

  it('test_swimlanes', async () => {
    await renderBoard([
      task('task-1', 'Feature task', 'feature', [stage('build', 'ready')]),
      task('task-2', 'Bug task', 'bug', [stage('build', 'ready')]),
    ])

    expect(screen.getByRole('rowgroup', { name: /feature/i })).toBeTruthy()
    expect(screen.getByRole('rowgroup', { name: /bug/i })).toBeTruthy()
    expect(screen.queryByRole('rowgroup', { name: /chore/i })).toBeNull()
  })

  it('test_category_filter_hides_columns', async () => {
    await renderBoard([
      task('task-1', 'Build task', 'feature', [stage('build', 'ready')]),
      task('task-2', 'Test task', 'feature', [stage('test', 'ready', 'none')]),
    ])

    await userEvent.click(screen.getByRole('checkbox', { name: /quality/i }))

    expect(screen.getByRole('region', { name: /build/i })).toBeTruthy()
    expect(screen.queryByRole('region', { name: /test/i })).toBeNull()

    await userEvent.click(screen.getByRole('checkbox', { name: /delivery/i }))

    const board = screen.getByRole('region', { name: /lifecycle board/i })
    expect(within(board).queryByRole('region', { name: /build/i })).toBeNull()
    expect(within(board).queryByRole('region', { name: /test/i })).toBeNull()
  })
})
