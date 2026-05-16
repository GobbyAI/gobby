import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ProjectSummary } from '../ProjectSummary'
import type { ProjectWithStats } from '../../../hooks/useProjects'

const BASE_PROJECT: ProjectWithStats = {
  id: 'project-1',
  name: 'gobby',
  display_name: 'Gobby',
  repo_path: '/tmp/gobby',
  github_url: null,
  github_repo: null,
  linear_team_id: null,
  linear_project_id: null,
  approval_rules: [],
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  session_count: 0,
  open_task_count: 0,
  last_activity_at: null,
}

function renderSummary(lastActivityAt: string) {
  vi.stubGlobal(
    'fetch',
    vi.fn(() =>
      Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ stats: null, total: 0 }),
      }),
    ),
  )
  render(<ProjectSummary project={{ ...BASE_PROJECT, last_activity_at: lastActivityAt }} />)
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('ProjectSummary', () => {
  it('includes the year for last activity outside the current calendar year', () => {
    const previousYear = new Date().getFullYear() - 1

    renderSummary(`${previousYear}-12-31T12:00:00Z`)

    expect(screen.getByRole('group', { name: /Last activity:/ })).toHaveTextContent(
      String(previousYear),
    )
  })

  it('keeps current-year last activity compact', () => {
    const currentYear = new Date().getFullYear()

    renderSummary(`${currentYear}-01-02T12:00:00Z`)

    expect(screen.getByRole('group', { name: /Last activity:/ })).not.toHaveTextContent(
      String(currentYear),
    )
  })
})
