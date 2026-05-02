import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const { useTasksMock } = vi.hoisted(() => ({
  useTasksMock: vi.fn(),
}))
const { useStagesRegistryMock } = vi.hoisted(() => ({
  useStagesRegistryMock: vi.fn(),
}))

vi.mock('../../../hooks/useTasks', () => ({
  useTasks: useTasksMock,
}))
vi.mock('../../../hooks/useStagesRegistry', () => ({
  useStagesRegistry: useStagesRegistryMock,
}))

import { TasksPage } from '../TasksPage'

function baseTask() {
  return {
    id: 'task-1',
    ref: '#1',
    title: 'Manifest task',
    status: 'open',
    priority: 2,
    task_type: 'feature',
    parent_task_id: null,
    created_at: '2026-05-02T00:00:00Z',
    updated_at: '2026-05-02T00:00:00Z',
    seq_num: 1,
    path_cache: '1',
    requires_user_review: false,
    assignee: null,
    agent_name: null,
    sequence_order: null,
    start_date: null,
    due_date: null,
    project_id: 'proj-1',
  }
}

function useTasksResult() {
  const tasks = [baseTask()]
  return {
    allTasks: tasks,
    tasks,
    stats: {
      ready: 1,
      in_progress: 0,
      needs_review: 0,
      blocked: 0,
      review_approved: 0,
      closed: 0,
    },
    hasMore: false,
    isLoadingMore: false,
    loadMore: vi.fn(),
    isLoading: false,
    error: null,
    filters: {
      status: null,
      priority: null,
      taskType: null,
      assignee: null,
      label: null,
      parentTaskId: null,
      search: '',
      projectId: null,
    },
    setFilters: vi.fn(),
    refreshTasks: vi.fn(),
    getTask: vi.fn(),
    createTask: vi.fn(),
    updateTask: vi.fn(),
    claimTask: vi.fn(),
    releaseTaskClaim: vi.fn(),
    markTaskNeedsReview: vi.fn(),
    markTaskReviewApproved: vi.fn(),
    escalateTask: vi.fn(),
    deEscalateTask: vi.fn(),
    advanceStage: vi.fn(),
    failStage: vi.fn(),
    startStage: vi.fn(),
    closeTask: vi.fn(),
    reopenTask: vi.fn(),
    deleteTask: vi.fn(),
    getDependencies: vi.fn(),
    getSubtasks: vi.fn(),
  }
}

describe('TasksPage lifecycle board integration', () => {
  beforeEach(() => {
    useTasksMock.mockReturnValue(useTasksResult())
    useStagesRegistryMock.mockReturnValue({
      registry: [],
      isLoading: false,
      error: null,
    })
  })

  it('test_kanban_mode_renders_lifecycle_board', () => {
    render(<TasksPage />)

    fireEvent.click(screen.getByTitle('Kanban view'))

    expect(screen.getByRole('region', { name: /lifecycle board/i })).toBeTruthy()
  })
})
