import { describe, it, expect, vi, beforeAll, beforeEach, afterEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { TasksTab } from '../TasksTab'
import { createMockFetch, type MockFetchInstance } from '../../../test/mocks/fetch'

beforeAll(() => {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
})

function renderNodes(nodes: Array<{ id: string; task: { title: string }; children?: any[] }>) {
  return nodes.map((node) => (
    <div key={node.id}>
      <div>{node.task.title}</div>
      {node.children?.length ? renderNodes(node.children) : null}
    </div>
  ))
}

vi.mock('react-arborist', () => ({
  Tree: ({ data }: { data: Array<{ id: string; task: { title: string }; children?: any[] }> }) => (
    <div data-testid="task-tree">{renderNodes(data)}</div>
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

    fireEvent.click(screen.getByText('Load more'))

    expect(screen.getByText('Open task 10')).toBeTruthy()
  })
})
