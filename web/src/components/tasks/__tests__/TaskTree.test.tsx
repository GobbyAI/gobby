import React from 'react'
import { describe, it, expect, vi, beforeAll } from 'vitest'
import { render, screen } from '@testing-library/react'
import type { GobbyTask } from '../../../hooks/useTasks'

// ResizeObserver is not available in jsdom
beforeAll(() => {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
})

// Mock react-arborist since it's heavy and requires DOM measurements
vi.mock('react-arborist', () => ({
  Tree: React.forwardRef(({ data }: { data: unknown[] }, _ref) => (
    <div data-testid="tree">
      {data.map((node: any) => (
        <div key={node.id} data-testid={`tree-node-${node.id}`}>
          {node.task.title}
        </div>
      ))}
    </div>
  )),
}))

vi.mock('../TaskBadges', () => ({
  StatusDot: ({ status }: { status: string }) => <span data-testid="status-dot">{status}</span>,
  PriorityBadge: ({ priority }: { priority: number }) => <span data-testid="priority">{priority}</span>,
  TypeBadge: ({ type }: { type: string }) => <span data-testid="type">{type}</span>,
}))

vi.mock('../TaskStatusStrip', () => ({
  TaskStatusStrip: () => null,
}))

// Import after mocks
import { TaskTree } from '../TaskTree'

const SAMPLE_TASKS: GobbyTask[] = [
  {
    id: 'task-1',
    ref: '#100',
    title: 'Parent task',
    status: 'open',
    priority: 1,
    type: 'task',
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
  },
  {
    id: 'task-2',
    ref: '#101',
    title: 'Child task',
    status: 'open',
    priority: 2,
    type: 'task',
    parent_task_id: 'task-1',
    created_at: '2026-03-01T00:00:00Z',
    updated_at: '2026-03-01T12:00:00Z',
    seq_num: 101,
    path_cache: '100.101',
    requires_user_review: false,
    assignee: null,
    agent_name: null,
    sequence_order: null,
    start_date: null,
    due_date: null,
    project_id: 'proj-1',
  },
  {
    id: 'task-3',
    ref: '#102',
    title: 'Closed task',
    status: 'closed',
    priority: 3,
    type: 'task',
    parent_task_id: null,
    created_at: '2026-03-01T00:00:00Z',
    updated_at: '2026-03-01T12:00:00Z',
    seq_num: 102,
    path_cache: '102',
    requires_user_review: false,
    assignee: null,
    agent_name: null,
    sequence_order: null,
    start_date: null,
    due_date: null,
    project_id: 'proj-1',
  },
]

describe('TaskTree', () => {
  it('renders a tree with tasks', () => {
    render(
      <TaskTree
        tasks={SAMPLE_TASKS}

        onSelectTask={vi.fn()}
      />,
    )

    expect(screen.getByTestId('tree')).toBeTruthy()
  })

  it('builds tree hierarchy from flat tasks', () => {
    render(
      <TaskTree
        tasks={SAMPLE_TASKS}

        onSelectTask={vi.fn()}
      />,
    )

    // Parent task should be rendered as root node
    expect(screen.getByText('Parent task')).toBeTruthy()
  })

  it('renders with empty task list', () => {
    render(
      <TaskTree
        tasks={[]}

        onSelectTask={vi.fn()}
      />,
    )

    expect(screen.getByTestId('tree')).toBeTruthy()
  })
})
