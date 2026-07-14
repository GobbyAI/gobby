import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { AgentPortfolioPage } from '../AgentPortfolioPage'

describe('AgentPortfolioPage', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('renders the escalated task metric once with danger styling', async () => {
    const fetchMock = vi.fn((url: string) => {
      const body = url.includes('/api/sessions')
        ? {
            sessions: [
              {
                id: 'session-1',
                ref: '#1',
                source: 'codex',
                title: null,
                status: 'active',
                model: null,
                message_count: 1,
                created_at: '2026-07-14T00:00:00Z',
                updated_at: '2026-07-14T00:00:00Z',
                usage_input_tokens: 0,
                usage_output_tokens: 0,
                had_edits: false,
                agent_depth: 0,
                parent_session_id: null,
              },
            ],
          }
        : {
            tasks: [
              {
                id: 'task-1',
                ref: '#1',
                title: 'Escalated task',
                status: 'escalated',
                state: {
                  owner_session_id: 'session-1',
                  is_escalated: true,
                },
                priority: 1,
                type: 'bug',
                category: 'code',
                agent_name: null,
                created_at: '2026-07-14T00:00:00Z',
                updated_at: '2026-07-14T00:00:00Z',
                closed_at: null,
                closed_in_session_id: null,
                created_in_session_id: null,
                validation_fail_count: 0,
                escalated_at: '2026-07-14T00:00:00Z',
              },
            ],
          }

      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve(body),
      } as Response)
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<AgentPortfolioPage />)

    const agentButton = await screen.findByRole('button', { name: /Codex/ })
    fireEvent.click(agentButton)

    await waitFor(() => {
      expect(screen.getAllByText('Escalated')).toHaveLength(1)
    })

    const escalatedLabel = screen.getByText('Escalated')
    const row = escalatedLabel.closest('.agent-detail-row')
    expect(row).not.toBeNull()
    expect(within(row as HTMLElement).getByText('1')).toHaveClass(
      'agent-detail-value--danger',
    )
  })
})
