import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { SessionsTab } from '../SessionsTab'
import type { GobbySession } from '../../../types/sessions'

type LocalSession = GobbySession & {
  is_local?: boolean | null
}

function makeSession(overrides: Partial<LocalSession> = {}): LocalSession {
  return {
    id: 'session-1',
    ref: '#201',
    external_id: 'external-1',
    source: 'claude',
    project_id: 'project-1',
    title: 'Terminal Session',
    title_source: null,
    status: 'active',
    model: 'claude-sonnet-4-5',
    message_count: 0,
    created_at: '2026-04-08T12:00:00Z',
    updated_at: '2026-04-08T12:00:00Z',
    seq_num: 201,
    transcript_path: null,
    summary_path: null,
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
    terminal_context: { tmux_pane: '%1' },
    sandbox_enabled: false,
    ...overrides,
  }
}

describe('SessionsTab Phase 1 chip contract', () => {
  beforeEach(() => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: true,
        json: async () => ({ agents: [] }),
      })),
    )
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('renders session badges with the shared chip class', async () => {
    render(
      <SessionsTab
        sessions={[makeSession({ sandbox_enabled: true })]}
      />,
    )

    await waitFor(() => expect(screen.getByText('#201: Terminal Session')).toBeInTheDocument())

    expect(screen.getByText('tmux')).toHaveClass('chip', 'chip--tmux')
    expect(screen.getByText('SB')).toHaveClass('chip', 'chip--sandbox')
  })

  it('shows LOCAL only for sessions marked as local', async () => {
    render(
      <SessionsTab
        sessions={[
          makeSession({
            id: 'local-session',
            ref: '#301',
            title: 'Local Session',
            seq_num: 301,
            is_local: true,
          }),
          makeSession({
            id: 'cloud-session',
            ref: '#302',
            title: 'Cloud Session',
            seq_num: 302,
            is_local: false,
          }),
        ]}
      />,
    )

    await waitFor(() => expect(screen.getByText('#301: Local Session')).toBeInTheDocument())

    const localBadge = screen.getByText('LOCAL')
    expect(localBadge).toHaveClass('chip', 'chip--local')
    expect(screen.getByText('#302: Cloud Session').parentElement).not.toHaveTextContent('LOCAL')
  })
})
