import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { AgentPortfolioPage } from '../AgentPortfolioPage'

const wsHandlers = vi.hoisted(
  () => ({}) as Record<string, (data: Record<string, unknown>) => void>,
)
vi.mock('../../../hooks/useWebSocketEvent', () => ({
  useWebSocketEvent: (
    eventType: string,
    handler: (data: Record<string, unknown>) => void,
  ) => {
    wsHandlers[eventType] = handler
  },
}))

describe('AgentPortfolioPage', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.useRealTimers()
    for (const key of Object.keys(wsHandlers)) delete wsHandlers[key]
  })

  it('has no manual refresh control; toolbar and cards keep positioned wrappers', () => {
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(() => undefined)))

    render(<AgentPortfolioPage />)

    // WS-driven refetch replaces the manual Refresh button (#20048).
    expect(screen.queryByRole('button', { name: 'Refresh agents' })).toBeNull()
    for (const select of screen.getAllByRole('combobox')) {
      expect(select.parentElement).toHaveClass('relative')
    }
    expect(screen.getByText('Agent Types').parentElement).toHaveClass('rounded-lg')
  })

  it('coalesces task/session events into one background refetch', async () => {
    vi.useFakeTimers()
    const fetchMock = vi.fn(() =>
      Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ sessions: [], tasks: [] }),
      } as unknown as Response),
    )
    vi.stubGlobal('fetch', fetchMock)

    render(<AgentPortfolioPage />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(fetchMock).toHaveBeenCalledTimes(2) // initial sessions + tasks

    act(() => {
      wsHandlers.task_event({})
      wsHandlers.session_event({})
      wsHandlers.task_event({})
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000)
    })
    // One coalesced refetch: sessions + tasks once, not once per event.
    expect(fetchMock).toHaveBeenCalledTimes(4)
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
