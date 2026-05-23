import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { SystemHealthCard } from '../SystemHealthCard'
import type { AdminStatus } from '../../../hooks/useDashboard'

const BASE_STATUS: AdminStatus = {
  status: 'running',
  project_id: 'project-1',
  server: { port: 60887, uptime_seconds: 3600, running: true },
  process: { memory_rss_mb: 100, memory_vms_mb: 200, cpu_percent: 5, num_threads: 10 },
  background_tasks: { active: 0, total: 5, completed: 5, failed: 0 },
  mcp_servers: {},
  sessions: { active: 2, paused: 0, handoff_ready: 0, total: 5 },
  tasks: {
    open: 3,
    in_progress: 1,
    closed: 10,
    needs_review: 0,
    review_approved: 0,
    escalated: 0,
    ready: 0,
    blocked: 0,
    closed_24h: 2,
  },
  memory: {
    count: 42,
    by_type: { fact: 20 },
    recent_count: 3,
    falkordb: { configured: true, installed: true, healthy: true },
  },
  skills: { total: 15 },
  pipelines: { running: 0, waiting_approval: 0, completed: 3, failed: 1, total: 4 },
  savings: {
    today_tokens_saved: 0,
    today_events: 0,
    cumulative_tokens_saved: 0,
    categories: {},
  },
}

describe('SystemHealthCard', () => {
  it('renders FalkorDB health from the dashboard status payload', () => {
    render(<SystemHealthCard data={BASE_STATUS} />)

    expect(screen.getByText('FalkorDB connected')).toBeInTheDocument()
  })
})
