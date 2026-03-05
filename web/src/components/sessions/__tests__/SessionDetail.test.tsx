import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { SessionDetail } from '../SessionDetail'
import type { GobbySession } from '../../../hooks/useSessions'

// Mock sub-components
vi.mock('../SessionTranscript', () => ({
  SessionTranscript: () => <div data-testid="transcript">Transcript</div>,
}))
vi.mock('../SessionLineage', () => ({
  SessionLineage: () => <div data-testid="lineage">Lineage</div>,
}))
vi.mock('../../chat/ui/ConfirmDialog', () => ({
  ConfirmDialog: () => null,
}))
vi.mock('../../shared/SourceIcon', () => ({
  SourceIcon: ({ source }: { source: string }) => <span data-testid="source-icon">{source}</span>,
}))
vi.mock('../../shared/Icons', () => ({
  BranchIcon: () => <span>branch</span>,
  ChatIcon: () => <span>chat</span>,
  SummaryIcon: () => <span>summary</span>,
}))
vi.mock('../../shared/MemoizedMarkdown', () => ({
  MemoizedMarkdown: ({ content }: { content: string }) => <div data-testid="markdown">{content}</div>,
}))

const SAMPLE_SESSION: GobbySession = {
  id: 'sess-1',
  ref: '#100',
  external_id: 'ext-1',
  source: 'claude',
  project_id: 'proj-1',
  title: 'Test Session',
  status: 'active',
  model: 'claude-4',
  message_count: 42,
  created_at: '2026-03-01T00:00:00Z',
  updated_at: '2026-03-01T12:00:00Z',
  seq_num: 100,
  summary_markdown: null,
  git_branch: 'feature/test',
  usage_input_tokens: 10000,
  usage_output_tokens: 5000,
  usage_total_cost_usd: 0.50,
  had_edits: true,
  agent_depth: 0,
  chat_mode: null,
  parent_session_id: null,
}

describe('SessionDetail', () => {
  const defaultProps = {
    session: SAMPLE_SESSION,
    messages: [],
    totalMessages: 0,
    isLoading: false,
    onGenerateSummary: vi.fn(),
    isGeneratingSummary: false,
    allSessions: [SAMPLE_SESSION],
    onSelectSession: vi.fn(),
  }

  it('renders session title', () => {
    render(<SessionDetail {...defaultProps} />)
    expect(screen.getByText('Test Session')).toBeTruthy()
  })

  it('renders fallback title when title is null', () => {
    render(
      <SessionDetail
        {...defaultProps}
        session={{ ...SAMPLE_SESSION, title: null }}
      />,
    )
    expect(screen.getByText(/Session #/)).toBeTruthy()
  })

  it('renders source icon', () => {
    render(<SessionDetail {...defaultProps} />)
    expect(screen.getByTestId('source-icon')).toBeTruthy()
  })

  it('renders session stats', () => {
    render(<SessionDetail {...defaultProps} />)
    // Should show message count, token usage, cost, etc.
    expect(screen.getByText(/42 msgs/)).toBeTruthy()
  })

  it('renders git branch when present', () => {
    render(<SessionDetail {...defaultProps} />)
    expect(screen.getByText('feature/test')).toBeTruthy()
  })

  it('renders transcript component', () => {
    render(<SessionDetail {...defaultProps} />)
    expect(screen.getByTestId('transcript')).toBeTruthy()
  })

  it('renders summary when available', () => {
    render(
      <SessionDetail
        {...defaultProps}
        session={{ ...SAMPLE_SESSION, summary_markdown: '# Summary\n\nDid some work' }}
      />,
    )
    expect(screen.getByTestId('markdown')).toBeTruthy()
  })

  it('shows loading state', () => {
    render(<SessionDetail {...defaultProps} isLoading={true} />)
    // Should still render the header
    expect(screen.getByText('Test Session')).toBeTruthy()
  })
})
