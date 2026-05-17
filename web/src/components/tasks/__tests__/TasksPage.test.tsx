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
import type { GobbyTask } from '../../../hooks/useTasks'
import type { CanonicalTaskState } from '../../../lib/taskState'

function makeState(overrides: Partial<CanonicalTaskState> = {}): CanonicalTaskState {
  return {
    owner_session_id: null,
    owner_session_ref: null,
    current_stage: null,
    is_claimed: false,
    is_closed: false,
    is_escalated: false,
    is_blocked: false,
    is_merge_ready: false,
    closed_at: null,
    closed_reason: null,
    closed_in_session_id: null,
    closed_commit_sha: null,
    escalated_at: null,
    escalation_reason: null,
    ...overrides,
  }
}

function baseTask(): GobbyTask {
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
    state: null,
    assignee: null,
    agent_name: null,
    sequence_order: null,
    start_date: null,
    due_date: null,
    project_id: 'proj-1',
    current_stage: null,
    stages: [],
  }
}

function useTasksResult(tasks = [baseTask()]) {
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
    advanceStage: vi.fn(),
    escalateTask: vi.fn(),
    deEscalateTask: vi.fn(),
    failStage: vi.fn(),
    moveTaskToStage: vi.fn(),
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

  it('groups same-prefix owner sessions by full owner id', () => {
    const ownerA = 'abcdefgh-1111-4444-8888-aaaaaaaaaaaa'
    const ownerB = 'abcdefgh-2222-4444-8888-bbbbbbbbbbbb'
    useTasksMock.mockReturnValue(useTasksResult([
      {
        ...baseTask(),
        id: 'task-1',
        ref: '#1',
        title: 'First task',
        agent_name: 'Reviewer',
        state: makeState({ owner_session_id: ownerA }),
      },
      {
        ...baseTask(),
        id: 'task-2',
        ref: '#2',
        seq_num: 2,
        title: 'Second task',
        agent_name: 'Reviewer',
        state: makeState({ owner_session_id: ownerB }),
      },
    ]))

    render(<TasksPage />)
    fireEvent.click(screen.getByText('By Agent'))

    const duplicateLabelGroups = screen.getAllByText((_content, node) =>
      node?.textContent === 'Reviewer #abcdefgh (1)'
    )
    expect(duplicateLabelGroups).toHaveLength(2)
  })

  it('uses shared Button loading state for load more', () => {
    useTasksMock.mockReturnValue({
      ...useTasksResult(),
      hasMore: true,
      isLoadingMore: true,
    })

    render(<TasksPage />)

    const button = screen.getByRole('button', { name: /load more/i })
    expect(button).toBeDisabled()
    expect(button).toHaveAttribute('aria-busy', 'true')
  })
})
