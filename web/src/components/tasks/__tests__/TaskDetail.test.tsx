import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { TaskDetail } from '../TaskDetail'
import type { GobbyTaskDetail } from '../../../hooks/useTasks'

// Mock CSS imports
vi.mock('../task-execution.css', () => ({}))

// Mock heavy sub-components
vi.mock('../ReasoningTimeline', () => ({ ReasoningTimeline: () => null }))
vi.mock('../ActionFeed', () => ({ ActionFeed: () => null }))
vi.mock('../SessionViewer', () => ({ SessionViewer: () => null }))
vi.mock('../CapabilityScope', () => ({ CapabilityScope: () => null }))
vi.mock('../EscalationCard', () => ({
  EscalationCard: ({
    onResolve,
    targetStatus,
  }: {
    onResolve: (decision: string) => void
    targetStatus?: string | null
  }) => (
    <button onClick={() => onResolve('mock decision')}>
      {`Resolve escalation (${targetStatus ?? 'none'})`}
    </button>
  ),
}))
vi.mock('../TaskResults', () => ({ TaskResults: () => null }))
vi.mock('../TokenTracker', () => ({ TokenTracker: () => null }))
vi.mock('../TaskMemories', () => ({ TaskMemories: () => null }))
vi.mock('../TaskComments', () => ({ TaskComments: () => null }))
vi.mock('../PermissionOverrides', () => ({ PermissionOverrides: () => null }))

const SAMPLE_TASK: GobbyTaskDetail = {
  id: 'task-1',
  ref: '#100',
  title: 'Fix the bug',
  status: 'open',
  priority: 1,
  task_type: 'bug',
  parent_task_id: null,
  created_at: '2026-03-01T00:00:00Z',
  updated_at: '2026-03-01T12:00:00Z',
  seq_num: 100,
  path_cache: '100',
  requires_user_review: false,
  assignee: null,
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
  escalated_at: null,
  escalation_reason: null,
  created_in_session_id: null,
  closed_in_session_id: null,
  complexity_score: null,
  is_expanded: false,
  expansion_status: 'none',
  github_pr_number: null,
  github_repo: null,
}

describe('TaskDetail', () => {
  const defaultProps = {
    taskId: 'task-1',
    getTask: vi.fn().mockResolvedValue(SAMPLE_TASK),
    getDependencies: vi.fn().mockResolvedValue(null),
    getSubtasks: vi.fn().mockResolvedValue([]),
    actions: {
      claimTask: vi.fn().mockResolvedValue(null),
      releaseTaskClaim: vi.fn().mockResolvedValue(null),
      advanceStage: vi.fn().mockResolvedValue(undefined),
      escalateTask: vi.fn().mockResolvedValue(null),
      deEscalateTask: vi.fn().mockResolvedValue(null),
      closeTask: vi.fn().mockResolvedValue(null),
      reopenTask: vi.fn().mockResolvedValue(null),
    },
    onSelectTask: vi.fn(),
    onClose: vi.fn(),
  }

  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders loading state then task details', async () => {
    render(<TaskDetail {...defaultProps} />)

    await waitFor(() => {
      expect(screen.getByText('Fix the bug')).toBeTruthy()
    })

    expect(defaultProps.getTask).toHaveBeenCalledWith('task-1')
  })

  it('renders nothing when taskId is null', () => {
    const { container } = render(<TaskDetail {...defaultProps} taskId={null} />)
    expect(container.textContent).toBe('')
  })

  it('renders task description', async () => {
    render(<TaskDetail {...defaultProps} />)

    await waitFor(() => {
      expect(screen.getByText('A detailed bug description')).toBeTruthy()
    })
  })

  it('calls onClose when close button clicked', async () => {
    render(<TaskDetail {...defaultProps} />)

    await waitFor(() => {
      expect(screen.getByText('Fix the bug')).toBeTruthy()
    })

    // Find and click the close button (×)
    const closeBtn = screen.getByTitle(/close/i) || screen.getByLabelText(/close/i)
    if (closeBtn) {
      await userEvent.click(closeBtn)
      expect(defaultProps.onClose).toHaveBeenCalled()
    }
  })

  it('fetches dependencies and subtasks', async () => {
    render(<TaskDetail {...defaultProps} />)

    await waitFor(() => {
      expect(defaultProps.getDependencies).toHaveBeenCalledWith('task-1')
      expect(defaultProps.getSubtasks).toHaveBeenCalledWith('task-1')
    })
  })

  it('re-fetches when taskId changes', async () => {
    const { rerender } = render(<TaskDetail {...defaultProps} />)

    await waitFor(() => {
      expect(defaultProps.getTask).toHaveBeenCalledWith('task-1')
    })

    rerender(<TaskDetail {...defaultProps} taskId="task-2" />)

    await waitFor(() => {
      expect(defaultProps.getTask).toHaveBeenCalledWith('task-2')
    })
  })

  it('uses the server-provided pre-escalation status when resuming a task', async () => {
    const escalatedTask: GobbyTaskDetail = {
      ...SAMPLE_TASK,
      status: 'escalated',
      escalated_at: '2026-03-02T00:00:00Z',
      escalation_reason: 'Blocked on user input',
      pre_escalation_status: 'needs_review',
    }

    render(
      <TaskDetail
        {...defaultProps}
        getTask={vi.fn().mockResolvedValue(escalatedTask)}
      />
    )

    await waitFor(() => {
      expect(screen.getByText('Fix the bug')).toBeTruthy()
    })

    await userEvent.selectOptions(
      screen.getByLabelText('Change status'),
      'resume',
    )
    await userEvent.click(screen.getByRole('button', { name: 'Update' }))

    await waitFor(() => {
      expect(defaultProps.actions.deEscalateTask).toHaveBeenCalledWith(
        'task-1',
        'Resumed from task detail',
        'needs_review',
      )
    })
  })

  it('refreshes after escalation-card resolution instead of de-escalating twice', async () => {
    const escalatedTask: GobbyTaskDetail = {
      ...SAMPLE_TASK,
      status: 'escalated',
      escalated_at: '2026-03-02T00:00:00Z',
      escalation_reason: 'Blocked on user input',
      pre_escalation_status: 'needs_review',
    }
    const getTask = vi.fn().mockResolvedValue(escalatedTask)

    render(
      <TaskDetail
        {...defaultProps}
        getTask={getTask}
      />
    )

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Resolve escalation (needs_review)' })).toBeTruthy()
    })

    await userEvent.click(screen.getByRole('button', { name: 'Resolve escalation (needs_review)' }))

    await waitFor(() => {
      expect(getTask).toHaveBeenCalledTimes(2)
    })
    expect(defaultProps.actions.deEscalateTask).not.toHaveBeenCalled()
  })

  it('offers Release Claim for claimed non-escalated tasks', async () => {
    const claimedTask: GobbyTaskDetail = {
      ...SAMPLE_TASK,
      status: 'in_progress',
      claimed_by_session_id: 'sess-1',
    }

    render(
      <TaskDetail
        {...defaultProps}
        getTask={vi.fn().mockResolvedValue(claimedTask)}
      />
    )

    await waitFor(() => {
      expect(screen.getByText('Fix the bug')).toBeTruthy()
    })

    await userEvent.selectOptions(
      screen.getByLabelText('Change status'),
      'release_claim',
    )
    await userEvent.click(screen.getByRole('button', { name: 'Update' }))

    await waitFor(() => {
      expect(defaultProps.actions.releaseTaskClaim).toHaveBeenCalledWith('task-1')
    })
  })
})
