import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

type StageState =
  | 'ready'
  | 'in_progress'
  | 'needs_review'
  | 'review_approved'
  | 'done'
type ReviewPolicy = 'required' | 'none' | 'optional'

const requiredStage = {
  name: 'build',
  display_name: 'Build',
  category: 'delivery',
  review_policy: 'required',
  sequence_order: 10,
}

const noneStage = {
  ...requiredStage,
  name: 'test',
  display_name: 'Test',
  review_policy: 'none',
}

function row(state: StageState, reviewPolicy: ReviewPolicy) {
  return {
    name: 'build',
    display_name: 'Build',
    category: 'delivery',
    state,
    review_policy: reviewPolicy,
    updated_at: '2026-05-02T00:00:00Z',
  }
}

function task(id: string, title: string, state: StageState, reviewPolicy: ReviewPolicy) {
  return {
    id,
    title,
    task_type: 'feature',
    stages: [row(state, reviewPolicy)],
  }
}

async function loadStageColumn() {
  const modulePath = '../StageColumn'
  return import(/* @vite-ignore */ modulePath)
}

async function renderColumn(
  stage: typeof requiredStage,
  tasks: ReturnType<typeof task>[],
  onAdvanceStage = vi.fn(),
) {
  const { StageColumn } = await loadStageColumn()
  render(
    <StageColumn
      stage={stage}
      tasks={tasks}
      onSelectTask={vi.fn()}
      onAdvanceStage={onAdvanceStage}
      availableStages={[requiredStage, noneStage]}
    />,
  )
  return { onAdvanceStage }
}

describe('StageColumn Phase 6 contracts', () => {
  it('test_5_state_grouping_for_required_policy', async () => {
    await renderColumn(requiredStage, [
      task('task-1', 'Ready task', 'ready', 'required'),
      task('task-2', 'Started task', 'in_progress', 'required'),
      task('task-3', 'Review task', 'needs_review', 'required'),
      task('task-4', 'Approved task', 'review_approved', 'required'),
      task('task-5', 'Done task', 'done', 'required'),
    ])

    const groupStates = screen
      .getAllByTestId(/stage-group-/)
      .map(group => group.getAttribute('data-state'))

    expect(groupStates).toEqual([
      'ready',
      'in_progress',
      'needs_review',
      'review_approved',
      'done',
    ])
  })

  it('test_3_state_grouping_for_none_policy', async () => {
    await renderColumn(noneStage, [
      task('task-1', 'Ready task', 'ready', 'none'),
      task('task-2', 'Started task', 'in_progress', 'none'),
      task('task-3', 'Malformed review task', 'needs_review', 'none'),
      task('task-4', 'Done task', 'done', 'none'),
    ])

    const groupStates = screen
      .getAllByTestId(/stage-group-/)
      .map(group => group.getAttribute('data-state'))

    expect(groupStates).toEqual(['ready', 'in_progress', 'done'])
    expect(screen.queryByText('Malformed review task')).toBeNull()
  })

  it('test_done_group_collapsed_by_default', async () => {
    await renderColumn(requiredStage, [
      task('task-1', 'Done one', 'done', 'required'),
      task('task-2', 'Done two', 'done', 'required'),
    ])

    expect(screen.getByRole('button', { name: /done \(2\)/i })).toBeTruthy()
    expect(screen.queryByText('Done one')).toBeNull()

    await userEvent.click(screen.getByRole('button', { name: /done \(2\)/i }))

    expect(screen.getByText('Done one')).toBeTruthy()
    expect(screen.getByText('Done two')).toBeTruthy()
  })

  it.each([
    ['ready', 'start'],
    ['in_progress', 'submit_for_review'],
    ['needs_review', 'approve_review'],
    ['review_approved', 'complete'],
  ] as const)('test_advance_button_required_policy_walks_full_chain %s', async (state, action) => {
    const onAdvanceStage = vi.fn()
    await renderColumn(
      requiredStage,
      [task(`task-${state}`, `${state} task`, state, 'required')],
      onAdvanceStage,
    )

    await userEvent.click(screen.getByRole('button', { name: /advance/i }))

    expect(onAdvanceStage).toHaveBeenCalledWith(`task-${state}`, 'build', action)
  })

  it.each([
    ['ready', 'start'],
    ['in_progress', 'complete'],
  ] as const)('test_advance_button_none_policy_walks_3_state_chain %s', async (state, action) => {
    const onAdvanceStage = vi.fn()
    await renderColumn(
      noneStage,
      [task(`task-${state}`, `${state} task`, state, 'none')],
      onAdvanceStage,
    )

    await userEvent.click(screen.getByRole('button', { name: /advance/i }))

    expect(onAdvanceStage).toHaveBeenCalledWith(`task-${state}`, 'build', action)
  })

  it('test_illegal_drag_surfaces_tooltip', async () => {
    const onAdvanceStage = vi.fn(() => {
      throw {
        name: 'IllegalStageTransitionError',
        reason: 'Review approval is required before completion',
      }
    })

    await renderColumn(
      requiredStage,
      [task('task-1', 'Illegal task', 'review_approved', 'required')],
      onAdvanceStage,
    )

    await userEvent.click(screen.getByRole('button', { name: /advance/i }))

    expect(
      await screen.findByRole('tooltip', {
        name: /review approval is required before completion/i,
      }),
    ).toBeTruthy()
  })
})
