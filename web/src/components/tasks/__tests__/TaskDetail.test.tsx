import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { TaskDetail } from '../TaskDetail'
import type { GobbyTaskDetail } from '../../../hooks/useTasks'

// Mock CSS imports
vi.mock('../task-detail.css', () => ({}))
vi.mock('../task-execution.css', () => ({}))
vi.mock('../task-advanced.css', () => ({}))

// Mock heavy sub-components
vi.mock('../ReasoningTimeline', () => ({ ReasoningTimeline: () => null }))
vi.mock('../ActionFeed', () => ({ ActionFeed: () => null }))
vi.mock('../SessionViewer', () => ({ SessionViewer: () => null }))
vi.mock('../CapabilityScope', () => ({ CapabilityScope: () => null }))
vi.mock('../RawTraceView', () => ({ RawTraceView: () => null }))
vi.mock('../OversightSelector', () => ({ OversightSelector: () => null }))
vi.mock('../EscalationCard', () => ({ EscalationCard: () => null }))
vi.mock('../TaskResults', () => ({ TaskResults: () => null }))
vi.mock('../CostTracker', () => ({ CostTracker: () => null }))
vi.mock('../TaskMemories', () => ({ TaskMemories: () => null }))
vi.mock('../AssigneePicker', () => ({ AssigneePicker: () => null }))
vi.mock('../TaskComments', () => ({ TaskComments: () => null }))
vi.mock('../PermissionOverrides', () => ({ PermissionOverrides: () => null }))
vi.mock('../TaskHandoff', () => ({ TaskHandoff: () => null }))
vi.mock('../LaunchAgentDialog', () => ({ LaunchAgentDialog: () => null }))

const SAMPLE_TASK: GobbyTaskDetail = {
  id: 'task-1',
  ref: '#100',
  title: 'Fix the bug',
  status: 'open',
  priority: 1,
  type: 'bug',
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
  description: 'A detailed bug description',
  labels: ['backend'],
  category: 'fix',
  validation_status: null,
  validation_feedback: null,
  validation_criteria: null,
  validation_fail_count: 0,
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
      updateTask: vi.fn().mockResolvedValue(null),
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
})
