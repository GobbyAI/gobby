import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'

// Mock the dashboard hook
vi.mock('../../../hooks/useDashboard', () => ({
  useDashboard: vi.fn(),
}))

// Mock CSS
vi.mock('../DashboardPage.css', () => ({}))

// Mock sub-components
vi.mock('../SystemHealthCard', () => ({
  SystemHealthCard: () => <div data-testid="system-health">Health</div>,
}))
vi.mock('../TasksCard', () => ({
  TasksCard: () => <div data-testid="tasks-card">Tasks</div>,
}))
vi.mock('../SessionsCard', () => ({
  SessionsCard: () => <div data-testid="sessions-card">Sessions</div>,
}))
vi.mock('../McpHealthCard', () => ({
  McpHealthCard: () => <div data-testid="mcp-card">MCP</div>,
}))
vi.mock('../MemorySkillsCard', () => ({
  MemorySkillsCard: () => <div data-testid="memory-card">Memory</div>,
}))

import { DashboardPage } from '../DashboardPage'
import { useDashboard } from '../../../hooks/useDashboard'
import type { AdminStatus } from '../../../hooks/useDashboard'

const mockUseDashboard = vi.mocked(useDashboard)

const SAMPLE_DATA: AdminStatus = {
  status: 'running',
  server: { port: 60887, uptime_seconds: 3600, running: true },
  process: { memory_rss_mb: 100, memory_vms_mb: 200, cpu_percent: 5, num_threads: 10 },
  background_tasks: { active: 0, total: 5, completed: 5, failed: 0 },
  mcp_servers: {},
  sessions: { active: 2, paused: 0, handoff_ready: 0, total: 5 },
  tasks: { open: 3, in_progress: 1, closed: 10, ready: 0, blocked: 0 },
  memory: { count: 42 },
  skills: { total: 15 },
}

const mockRefresh = vi.fn()

describe('DashboardPage', () => {
  it('renders loading state', () => {
    mockUseDashboard.mockReturnValue({
      data: null,
      isLoading: true,
      error: null,
      lastUpdated: null,
      refresh: mockRefresh,
    })

    render(<DashboardPage />)

    expect(screen.getByText('Loading dashboard...')).toBeTruthy()
  })

  it('renders error state', () => {
    mockUseDashboard.mockReturnValue({
      data: null,
      isLoading: false,
      error: 'Connection refused',
      lastUpdated: null,
      refresh: mockRefresh,
    })

    render(<DashboardPage />)

    expect(screen.getByText(/Failed to load/)).toBeTruthy()
  })

  it('renders dashboard cards when data is available', () => {
    mockUseDashboard.mockReturnValue({
      data: SAMPLE_DATA,
      isLoading: false,
      error: null,
      lastUpdated: new Date(),
      refresh: mockRefresh,
    })

    render(<DashboardPage />)

    expect(screen.getByTestId('system-health')).toBeTruthy()
    expect(screen.getByTestId('tasks-card')).toBeTruthy()
    expect(screen.getByTestId('sessions-card')).toBeTruthy()
    expect(screen.getByTestId('mcp-card')).toBeTruthy()
    expect(screen.getByTestId('memory-card')).toBeTruthy()
  })

  it('renders title', () => {
    mockUseDashboard.mockReturnValue({
      data: null,
      isLoading: true,
      error: null,
      lastUpdated: null,
      refresh: mockRefresh,
    })

    render(<DashboardPage />)

    expect(screen.getByText('Dashboard')).toBeTruthy()
  })

  it('shows last updated time', () => {
    const now = new Date('2026-03-05T12:30:00Z')
    mockUseDashboard.mockReturnValue({
      data: SAMPLE_DATA,
      isLoading: false,
      error: null,
      lastUpdated: now,
      refresh: mockRefresh,
    })

    render(<DashboardPage />)

    expect(screen.getByText(/Updated/)).toBeTruthy()
  })
})
