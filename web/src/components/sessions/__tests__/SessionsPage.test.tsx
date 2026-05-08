import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { SessionsPage } from '../SessionsPage'
import type { GobbySession } from '../../../types/sessions'

vi.mock('../../../hooks/useNow', () => ({
  useNow: () => new Date('2026-04-14T13:00:00Z').getTime(),
}))

vi.mock('../../../hooks/useSessionDetail', () => ({
  useSessionDetail: () => ({
    session: null,
    messages: [],
    totalMessages: 0,
    isLoading: false,
    generateSummary: vi.fn(),
    isGeneratingSummary: false,
  }),
}))

function createSession(overrides: Partial<GobbySession> = {}): GobbySession {
  return {
    id: 'session-1',
    ref: '#101',
    external_id: 'ext-101',
    source: 'droid',
    project_id: 'project-1',
    title: 'Droid Session',
    status: 'active',
    model: 'gpt-5.4',
    message_count: 3,
    created_at: '2026-04-14T12:00:00Z',
    updated_at: '2026-04-14T12:00:00Z',
    seq_num: 101,
    summary_markdown: null,
    digest_markdown: null,
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

describe('SessionsPage', () => {
  it('renders Droid source filter and Droid session icon', () => {
    const { container } = render(
      <SessionsPage
        sessions={[createSession()]}
        filters={{ source: null, projectId: null, search: '', sortOrder: 'newest' }}
        onFiltersChange={vi.fn()}
        isLoading={false}
      />,
    )

    expect(screen.getByRole('option', { name: 'Droid' })).toBeTruthy()
    expect(container.querySelector('.source-icon-droid')).toBeTruthy()
  })
})
