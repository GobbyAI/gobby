import type React from 'react'
import { describe, it, expect, vi, beforeAll, beforeEach, afterEach } from 'vitest'
import { fireEvent, render, screen, waitFor, act } from '@testing-library/react'
import { TasksTab } from '../TasksTab'
import { createMockFetch, type MockFetchInstance } from '../../../test/mocks/fetch'

// Capture the handler passed to useWebSocketEvent so tests can simulate events
let wsHandler: ((data: Record<string, unknown>) => void) | null = null
vi.mock('../../../hooks/useWebSocketEvent', () => ({
  useWebSocketEvent: (_eventType: string, handler: (data: Record<string, unknown>) => void) => {
    wsHandler = handler
  },
}))

interface TaskTreeNode {
  id: string
  task: { title: string }
  children?: TaskTreeNode[]
}

beforeAll(() => {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
})

function renderNodes(nodes: TaskTreeNode[]): React.ReactNode {
  return nodes.map((node) => (
    <div key={node.id}>
      <div>{node.task.title}</div>
      {node.children?.length ? renderNodes(node.children) : null}
    </div>
  ))
}

vi.mock('react-arborist', () => ({
  Tree: ({ data, height }: { data: TaskTreeNode[]; height?: number }) => (
    <div data-testid="task-tree" data-height={height ?? ''}>{renderNodes(data)}</div>
  ),
}))

vi.mock('../../chat/artifacts/ResizeHandle', () => ({
  ResizeHandle: () => <div data-testid="resize-handle" />,
}))

let mockFetch: MockFetchInstance

const taskList = [
  {
    id: 'task-review',
    ref: '#401',
    title: 'Review approved task',
    status: 'review_approved',
    priority: 2,
    task_type: 'task',
    parent_task_id: null,
    created_at: '2026-04-12T00:00:00Z',
    updated_at: '2026-04-12T00:00:00Z',
    seq_num: 401,
    path_cache: '401',
    requires_user_review: false,
    assignee: null,
    agent_name: null,
    sequence_order: null,
    start_date: null,
    due_date: null,
    project_id: 'proj-1',
  },
  ...Array.from({ length: 10 }, (_, index) => ({
    id: `task-${index + 1}`,
    ref: `#${410 + index}`,
    title: `Open task ${index + 1}`,
    status: 'open',
    priority: 2,
    task_type: 'task',
    parent_task_id: null,
    created_at: `2026-04-${String(11 - index).padStart(2, '0')}T00:00:00Z`,
    updated_at: `2026-04-${String(11 - index).padStart(2, '0')}T00:00:00Z`,
    seq_num: 410 + index,
    path_cache: String(410 + index),
    requires_user_review: false,
    assignee: null,
    agent_name: null,
    sequence_order: null,
    start_date: null,
    due_date: null,
    project_id: 'proj-1',
  })),
  {
    id: 'task-closed',
    ref: '#499',
    title: 'Closed task',
    status: 'closed',
    priority: 2,
    task_type: 'task',
    parent_task_id: null,
    created_at: '2026-04-13T00:00:00Z',
    updated_at: '2026-04-13T00:00:00Z',
    seq_num: 499,
    path_cache: '499',
    requires_user_review: false,
    assignee: null,
    agent_name: null,
    sequence_order: null,
    start_date: null,
    due_date: null,
    project_id: 'proj-1',
  },
]

describe('TasksTab', () => {
  beforeEach(() => {
    mockFetch = createMockFetch()
    mockFetch.mockJsonResponse(/\/api\/tasks\?/, { tasks: taskList })
  })

  afterEach(() => {
    mockFetch.restore()
    vi.restoreAllMocks()
    // Clear the captured WebSocket handler so it doesn't leak between tests
    wsHandler = null
  })

  it('includes review-approved tasks by default and paginates the list in batches of ten', async () => {
    render(<TasksTab projectId="proj-1" />)

    await waitFor(() => {
      expect(screen.getByText('Review approved task')).toBeTruthy()
      expect(screen.getByText('Open task 2')).toBeTruthy()
      expect(screen.getByText('Load more')).toBeTruthy()
    })

    expect(screen.queryByText('Open task 10')).toBeNull()
    expect(screen.queryByText('Closed task')).toBeNull()
    expect(screen.getByTestId('task-tree')).toHaveAttribute('data-height', '300')

    fireEvent.click(screen.getByText('Load more'))

    expect(screen.getByText('Open task 10')).toBeTruthy()
  })

  it('renders canonical state tasks and groups the filter menu by lifecycle and status', async () => {
    mockFetch.resetRoutes()
    mockFetch.mockJsonResponse(/\/api\/tasks\?/, {
      tasks: [
        {
          id: 'task-needs-review',
          ref: '#601',
          title: 'Canonical needs review task',
          priority: 2,
          task_type: 'task',
          parent_task_id: null,
          created_at: '2026-04-13T00:00:00Z',
          updated_at: '2026-04-13T00:00:00Z',
          seq_num: 601,
          path_cache: '601',
          project_id: 'proj-1',
          state: {
            owner_session_id: 'session-1',
            lifecycle_stage: 'needs_review',
            is_claimed: true,
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
          },
        },
      ],
    })

    render(<TasksTab projectId="proj-1" />)

    await waitFor(() => {
      expect(screen.getByText('Canonical needs review task')).toBeTruthy()
    })

    fireEvent.click(screen.getByTitle('Filter by task state'))

    expect(screen.getByText('Lifecycle')).toBeTruthy()
    expect(screen.getByText('Status')).toBeTruthy()
    expect(screen.getByText('Needs Review')).toBeTruthy()
    expect(screen.getByText('Merge Ready')).toBeTruthy()
    expect(screen.getByText('Closed')).toBeTruthy()
  })

  it('adds a new task when a task_created WebSocket event fires', async () => {
    render(<TasksTab projectId="proj-1" />)

    await waitFor(() => {
      expect(screen.getByText('Open task 1')).toBeTruthy()
    })

    expect(screen.queryByText('WS created task')).toBeNull()

    act(() => {
      wsHandler?.({
        type: 'task_event',
        event: 'task_created',
        task_id: 'task-ws-new',
        task: {
          id: 'task-ws-new',
          ref: '#900',
          title: 'WS created task',
          status: 'open',
          priority: 2,
          task_type: 'task',
          parent_task_id: null,
          created_at: '2026-04-09T00:00:00Z',
          updated_at: '2026-04-09T00:00:00Z',
          seq_num: 900,
          path_cache: '900',
          project_id: 'proj-1',
        },
      })
    })

    await waitFor(() => {
      expect(screen.getByText('WS created task')).toBeTruthy()
    })
  })

  it('removes a task when a task_deleted WebSocket event fires', async () => {
    render(<TasksTab projectId="proj-1" />)

    await waitFor(() => {
      expect(screen.getByText('Open task 1')).toBeTruthy()
    })

    act(() => {
      wsHandler?.({
        type: 'task_event',
        event: 'task_deleted',
        task_id: 'task-1',
        task: { id: 'task-1' },
      })
    })

    await waitFor(() => {
      expect(screen.queryByText('Open task 1')).toBeNull()
    })
  })

  it('ignores WebSocket events for other projects', async () => {
    render(<TasksTab projectId="proj-1" />)

    await waitFor(() => {
      expect(screen.getByText('Open task 1')).toBeTruthy()
    })

    act(() => {
      wsHandler?.({
        type: 'task_event',
        event: 'task_created',
        task_id: 'task-other',
        task: {
          id: 'task-other',
          ref: '#999',
          title: 'Other project task',
          status: 'open',
          priority: 2,
          task_type: 'task',
          parent_task_id: null,
          created_at: '2026-04-09T00:00:00Z',
          updated_at: '2026-04-09T00:00:00Z',
          seq_num: 999,
          path_cache: '999',
          project_id: 'proj-other',
        },
      })
    })

    expect(screen.queryByText('Other project task')).toBeNull()
  })
})
