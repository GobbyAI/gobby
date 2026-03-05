import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { GobbyTask } from '../../../hooks/useTasks'

// Mock pragmatic-drag-and-drop (heavy DOM dependency)
vi.mock('@atlaskit/pragmatic-drag-and-drop/element/adapter', () => ({
  draggable: () => () => {},
  dropTargetForElements: () => () => {},
  monitorForElements: () => () => {},
}))

// Mock sub-components
vi.mock('../TaskBadges', () => ({
  StatusDot: ({ status }: { status: string }) => <span data-testid="status-dot">{status}</span>,
  PriorityBadge: ({ priority }: { priority: number }) => <span data-testid="priority">{priority}</span>,
  TypeBadge: ({ type }: { type: string }) => <span data-testid="type">{type}</span>,
  BlockedIndicator: () => <span data-testid="blocked">blocked</span>,
  PRIORITY_STYLES: {
    0: { color: '#ff0000' },
    1: { color: '#ff8800' },
    2: { color: '#ffcc00' },
    3: { color: '#00cc00' },
    4: { color: '#888888' },
  },
}))

vi.mock('../TaskStatusStrip', () => ({
  TaskStatusStrip: () => null,
}))

vi.mock('../RiskBadges', () => ({
  classifyTaskRisk: () => 'low',
  RiskBadge: () => null,
}))

vi.mock('../ActivityPulse', () => ({
  ActivityPulse: () => null,
}))

vi.mock('../AssigneePicker', () => ({
  AssigneeBadge: () => null,
}))

import { KanbanBoard } from '../KanbanBoard'

const makeTask = (overrides: Partial<GobbyTask> & { id: string; title: string }): GobbyTask => ({
  ref: `#${overrides.id.replace('task-', '')}`,
  status: 'open',
  priority: 2,
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
  ...overrides,
})

const SAMPLE_TASKS: GobbyTask[] = [
  makeTask({ id: 'task-1', title: 'Open task', status: 'open' }),
  makeTask({ id: 'task-2', title: 'In progress task', status: 'in_progress' }),
  makeTask({ id: 'task-3', title: 'Review task', status: 'needs_review' }),
  makeTask({ id: 'task-4', title: 'Closed task', status: 'closed' }),
]

describe('KanbanBoard', () => {
  const defaultProps = {
    tasks: SAMPLE_TASKS,
    onSelectTask: vi.fn(),
  }

  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders all 6 columns', () => {
    render(<KanbanBoard {...defaultProps} />)

    expect(screen.getByText('Backlog')).toBeTruthy()
    expect(screen.getByText('In Progress')).toBeTruthy()
    expect(screen.getByText('Review')).toBeTruthy()
    expect(screen.getByText('Blocked')).toBeTruthy()
    expect(screen.getByText('Ready')).toBeTruthy()
    expect(screen.getByText('Closed')).toBeTruthy()
  })

  it('places tasks in correct columns', () => {
    render(<KanbanBoard {...defaultProps} />)

    expect(screen.getByText('Open task')).toBeTruthy()
    expect(screen.getByText('In progress task')).toBeTruthy()
    expect(screen.getByText('Review task')).toBeTruthy()
    expect(screen.getByText('Closed task')).toBeTruthy()
  })

  it('renders task ref badges', () => {
    render(<KanbanBoard {...defaultProps} />)

    expect(screen.getByText('#1')).toBeTruthy()
    expect(screen.getByText('#2')).toBeTruthy()
  })

  it('calls onSelectTask when card is clicked', async () => {
    const onSelect = vi.fn()
    render(<KanbanBoard {...defaultProps} onSelectTask={onSelect} />)

    await userEvent.click(screen.getByText('Open task'))
    expect(onSelect).toHaveBeenCalledWith('task-1')
  })

  it('shows column counts', () => {
    const { container } = render(<KanbanBoard {...defaultProps} />)

    const counts = container.querySelectorAll('.kanban-column-count')
    // One of the columns should have count "1" for each task
    const countTexts = Array.from(counts).map(el => el.textContent)
    expect(countTexts).toContain('1')
  })

  it('shows empty state in columns with no tasks', () => {
    render(<KanbanBoard {...defaultProps} />)

    // Blocked and Ready columns should have "No tasks"
    const empties = screen.getAllByText('No tasks')
    expect(empties.length).toBeGreaterThanOrEqual(2)
  })

  it('renders swimlane toolbar', () => {
    render(<KanbanBoard {...defaultProps} />)

    expect(screen.getByText('Group by:')).toBeTruthy()
    expect(screen.getByText('None')).toBeTruthy()
    expect(screen.getByText('Assignee')).toBeTruthy()
    expect(screen.getByText('Priority')).toBeTruthy()
    expect(screen.getByText('Parent')).toBeTruthy()
  })

  it('switches swimlane mode on click', async () => {
    render(<KanbanBoard {...defaultProps} />)

    await userEvent.click(screen.getByText('Priority'))

    // Should show priority swimlane headers
    expect(screen.getByText('Medium')).toBeTruthy()
  })

  it('renders with empty task list', () => {
    render(<KanbanBoard tasks={[]} onSelectTask={vi.fn()} />)

    // All columns should show "No tasks"
    const empties = screen.getAllByText('No tasks')
    expect(empties).toHaveLength(6)
  })

  it('shows advance button when onUpdateStatus is provided', () => {
    render(
      <KanbanBoard
        {...defaultProps}
        onUpdateStatus={vi.fn()}
      />,
    )

    // Open task should have a "→" advance button
    const advanceButtons = screen.getAllByTitle(/Move to/)
    expect(advanceButtons.length).toBeGreaterThanOrEqual(1)
  })

  it('calls onUpdateStatus when advance button clicked', async () => {
    const onUpdate = vi.fn()
    render(
      <KanbanBoard
        {...defaultProps}
        onUpdateStatus={onUpdate}
      />,
    )

    const advanceBtn = screen.getAllByTitle(/Move to/)[0]
    await userEvent.click(advanceBtn)
    expect(onUpdate).toHaveBeenCalled()
  })
})
