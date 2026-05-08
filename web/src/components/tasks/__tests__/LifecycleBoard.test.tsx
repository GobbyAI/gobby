import { fireEvent, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

type StageState =
  | 'ready'
  | 'in_progress'
  | 'needs_review'
  | 'review_approved'
  | 'done'
type ReviewPolicy = 'required' | 'none' | 'optional'

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

function stage(name: string, state: StageState, reviewPolicy: ReviewPolicy = 'required') {
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

function dragRight(element: HTMLElement) {
  fireEvent.pointerDown(element, { clientX: 0, clientY: 0, pointerId: 1 })
  fireEvent.pointerMove(element, { clientX: 160, clientY: 0, pointerId: 1 })
  fireEvent.pointerUp(element, { clientX: 160, clientY: 0, pointerId: 1 })
}

async function loadLifecycleBoard() {
  const modulePath = '../LifecycleBoard'
  return import(/* @vite-ignore */ modulePath)
}

async function loadStageActions() {
  const modulePath = '../../../lib/stageActions'
  return import(/* @vite-ignore */ modulePath)
}

async function renderBoard(
  tasks: ReturnType<typeof task>[],
  onAdvanceStage = vi.fn(),
) {
  const { LifecycleBoard } = await loadLifecycleBoard()
  render(
    <LifecycleBoard
      tasks={tasks}
      stagesRegistry={registry}
      onSelectTask={vi.fn()}
      onAdvanceStage={onAdvanceStage}
    />,
  )
  return { onAdvanceStage }
}

describe('LifecycleBoard Phase 6 contracts', () => {
  it('test_visible_stage_filtering', async () => {
    await renderBoard([
      task('task-1', 'Build manifest', 'feature', [stage('build', 'ready')]),
      task('task-2', 'Run tests', 'bug', [stage('test', 'in_progress', 'none')]),
    ])

    expect(screen.getByRole('region', { name: /build/i })).toBeTruthy()
    expect(screen.getByRole('region', { name: /test/i })).toBeTruthy()
    expect(screen.queryByRole('region', { name: /deploy/i })).toBeNull()
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

  it('test_drag_advance_calls_three_arg_signature', async () => {
    const onAdvanceStage = vi.fn()
    await renderBoard(
      [task('task-1', 'Ready task', 'feature', [stage('build', 'ready')])],
      onAdvanceStage,
    )

    dragRight(screen.getByRole('button', { name: /ready task/i }))

    expect(onAdvanceStage).toHaveBeenCalledWith('task-1', 'build', 'start')
  })

  it('test_drag_advance_resolves_action_per_policy', async () => {
    const onAdvanceStage = vi.fn()
    await renderBoard(
      [
        task('task-1', 'Required review', 'feature', [
          stage('build', 'in_progress', 'required'),
        ]),
        task('task-2', 'No review', 'feature', [stage('test', 'in_progress', 'none')]),
      ],
      onAdvanceStage,
    )

    dragRight(screen.getByRole('button', { name: /required review/i }))
    dragRight(screen.getByRole('button', { name: /no review/i }))

    expect(onAdvanceStage).toHaveBeenCalledWith(
      'task-1',
      'build',
      'submit_for_review',
    )
    expect(onAdvanceStage).toHaveBeenCalledWith('task-2', 'test', 'complete')
  })

  it('test_drag_advance_disabled_when_resolver_returns_null', async () => {
    const onAdvanceStage = vi.fn()
    await renderBoard(
      [task('task-1', 'Done task', 'feature', [stage('build', 'done')])],
      onAdvanceStage,
    )

    const card = screen.getByRole('button', { name: /done task/i })
    expect(card.getAttribute('aria-disabled')).toBe('true')

    dragRight(card)

    expect(onAdvanceStage).not.toHaveBeenCalled()
  })

  it('test_drag_advance_action_arg_matches_resolver_output', async () => {
    const { resolveAdvanceAction } = await loadStageActions()
    const onAdvanceStage = vi.fn()
    await renderBoard(
      [
        task('task-1', 'Approved review', 'feature', [
          stage('build', 'review_approved', 'required'),
        ]),
      ],
      onAdvanceStage,
    )

    dragRight(screen.getByRole('button', { name: /approved review/i }))

    expect(onAdvanceStage).toHaveBeenCalledWith(
      'task-1',
      'build',
      resolveAdvanceAction('review_approved', 'required'),
    )
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
