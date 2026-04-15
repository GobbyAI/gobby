import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { SessionSidebar } from '../SessionSidebar'
import type { GobbySession } from '../../../hooks/useSessions'

function createTestSession(overrides: Partial<GobbySession> = {}): GobbySession {
  return {
    id: 'session-1',
    ref: 'abc123',
    external_id: 'ext-abc123',
    source: 'claude',
    project_id: 'project-1',
    title: '   ',
    status: 'active',
    model: null,
    message_count: 0,
    created_at: '2026-04-14T12:00:00Z',
    updated_at: '2026-04-14T12:00:00Z',
    seq_num: null,
    summary_markdown: null,
    git_branch: null,
    usage_input_tokens: 0,
    usage_output_tokens: 0,
    had_edits: false,
    agent_depth: 0,
    chat_mode: null,
    agent_run_id: null,
    parent_session_id: null,
    session_type: 'terminal',
    terminal_context: null,
    ...overrides,
  }
}

describe('SessionSidebar', () => {
  it('preserves the session ref for untitled sessions', () => {
    render(
      <SessionSidebar
        sessions={[createTestSession()]}
        projects={[]}
        filters={{ source: null, projectId: null, search: '', sortOrder: 'newest' }}
        onFiltersChange={vi.fn()}
        activeSessionId={null}
        isLoading={false}
        onNewChat={vi.fn()}
        onSelectSession={vi.fn()}
        isOpen={true}
        onToggle={vi.fn()}
      />,
    )

    expect(screen.getByText('abc123: New Session')).toBeTruthy()
  })
})
