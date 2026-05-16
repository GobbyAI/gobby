import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

function stage() {
  return {
    name: 'build',
    display_name: 'Build',
    category: 'delivery',
    state: 'ready',
    review_policy: 'required',
    updated_at: '2026-05-02T00:00:00Z',
  }
}

function task(overrides: Record<string, unknown> = {}) {
  return {
    id: 'task-1',
    title: 'Blocked task',
    task_type: 'bug',
    stages: [stage()],
    ...overrides,
  }
}

async function loadStageCard() {
  const modulePath = '../StageCard'
  return import(/* @vite-ignore */ modulePath)
}

async function renderCard(
  lifecycleTask: ReturnType<typeof task>,
  onAdvanceStage = vi.fn(),
  onMoveTaskToStage = vi.fn(),
) {
  const { StageCard } = await loadStageCard()
  render(
    <StageCard
      task={lifecycleTask}
      stageName="build"
      state="ready"
      reviewPolicy="required"
      onSelectTask={vi.fn()}
      onAdvanceStage={onAdvanceStage}
      onMoveTaskToStage={onMoveTaskToStage}
      availableStages={[
        { name: 'build', display_name: 'Build', category: 'delivery', review_policy: 'required' },
        { name: 'deploy', display_name: 'Deploy', category: 'release', review_policy: 'optional' },
      ]}
    />,
  )
  return { onAdvanceStage, onMoveTaskToStage }
}

describe('StageCard Phase 6 contracts', () => {
  it('test_blocked_badge_default_visible', async () => {
    await renderCard(
      task({
        is_blocked: true,
        blocked_reason: 'Blocked by open upstream dependency #13801',
      }),
    )

    const badge = screen.getByText('Blocked')
    expect(badge).toBeTruthy()

    await userEvent.hover(badge)

    expect(
      await screen.findByRole('tooltip', {
        name: /blocked by open upstream dependency #13801/i,
      }),
    ).toBeTruthy()
  })

  it('test_blocked_manual_move_available', async () => {
    const onAdvanceStage = vi.fn()
    const onMoveTaskToStage = vi.fn()
    await renderCard(
      task({
        is_blocked: true,
        blocked_reason: 'Escalated: waiting on operator decision',
      }),
      onAdvanceStage,
      onMoveTaskToStage,
    )

    await userEvent.selectOptions(
      screen.getByLabelText(/move blocked task to stage/i),
      'deploy',
    )

    expect(onMoveTaskToStage).toHaveBeenCalledWith('task-1', 'deploy')
    await userEvent.hover(screen.getByText('Blocked'))
    expect(
      await screen.findByRole('tooltip', {
        name: /escalated: waiting on operator decision/i,
      }),
    ).toBeTruthy()
  })
})
