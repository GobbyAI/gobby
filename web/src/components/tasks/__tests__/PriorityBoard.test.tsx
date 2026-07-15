import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { GobbyTask } from '../../../types/tasks'
import { PriorityBoard } from '../PriorityBoard'

function task(id: string, title: string, updatedAt: string): GobbyTask {
  return {
    id,
    ref: `#${id}`,
    title,
    status: 'ready',
    priority: 1,
    task_type: 'task',
    parent_task_id: null,
    created_at: updatedAt,
    updated_at: updatedAt,
    seq_num: Number(id),
    path_cache: id,
    agent_name: null,
    sequence_order: null,
    start_date: null,
    due_date: null,
    project_id: 'project-1',
    current_stage: null,
    stages: [],
  }
}

describe('PriorityBoard', () => {
  it('sorts missing timestamp sentinels after valid timestamps', () => {
    render(
      <PriorityBoard
        tasks={[
          task('1', 'Missing timestamp', ''),
          task('2', 'Older valid task', '2026-01-01T00:00:00Z'),
          task('3', 'Newer valid task', '2026-02-01T00:00:00Z'),
        ]}
        onSelectTask={vi.fn()}
      />,
    )

    expect(screen.getAllByRole('button').map(button => button.textContent)).toEqual([
      expect.stringContaining('Newer valid task'),
      expect.stringContaining('Older valid task'),
      expect.stringContaining('Missing timestamp'),
    ])
  })
})
