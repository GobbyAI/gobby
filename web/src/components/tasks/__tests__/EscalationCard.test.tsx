import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { EscalationCard } from '../EscalationCard'
import type { GobbyTaskDetail } from '../../../hooks/useTasks'

const SAMPLE_TASK: GobbyTaskDetail = {
  id: 'task-1',
  ref: '#100',
  title: 'Fix the bug',
  status: 'escalated',
  priority: 1,
  task_type: 'bug',
  parent_task_id: null,
  created_at: '2026-03-01T00:00:00Z',
  updated_at: '2026-03-01T12:00:00Z',
  seq_num: 100,
  path_cache: '100',
  requires_user_review: false,
  assignee: 'session-1',
  agent_name: null,
  sequence_order: null,
  start_date: null,
  due_date: null,
  project_id: 'proj-1',
  current_stage: null,
  stages: [],
  description: 'A detailed bug description',
  labels: ['backend'],
  category: 'fix',
  validation_status: null,
  validation_feedback: null,
  validation_criteria: null,
  validation_fail_count: 0,
  validation_override_reason: null,
  closed_at: null,
  closed_reason: null,
  closed_commit_sha: null,
  commits: null,
  escalated_at: '2026-03-02T00:00:00Z',
  escalation_reason: JSON.stringify({
    question: 'What should the agent do next?',
    options: [
      {
        label: 'Continue review',
        description: 'Keep the current review state',
      },
    ],
  }),
  pre_escalation_status: 'needs_review',
  created_in_session_id: null,
  closed_in_session_id: null,
  complexity_score: null,
  is_expanded: false,
  expansion_status: 'none',
  github_pr_number: null,
  github_repo: null,
}

describe('EscalationCard', () => {
  const fetchMock = vi.fn()

  beforeEach(() => {
    fetchMock.mockResolvedValue({
      ok: true,
      text: vi.fn().mockResolvedValue(''),
    })
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('posts the target status when returning the task to the agent', async () => {
    const onResolve = vi.fn()

    render(
      <EscalationCard
        task={SAMPLE_TASK}
        targetStatus="needs_review"
        onResolve={onResolve}
      />
    )

    await userEvent.click(screen.getByRole('button', { name: /Continue review/i }))
    await userEvent.click(screen.getByRole('button', { name: /Return to Agent/i }))

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledTimes(1)
    })

    const [, request] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(request.method).toBe('POST')
    expect(JSON.parse(String(request.body))).toEqual({
      decision_context: 'Selected: Continue review — Keep the current review state',
      target_status: 'needs_review',
    })
    expect(onResolve).toHaveBeenCalledWith(
      'Selected: Continue review — Keep the current review state',
    )
  })
})
