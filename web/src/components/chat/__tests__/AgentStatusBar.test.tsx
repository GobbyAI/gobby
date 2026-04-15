import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { AgentStatusBar } from '../AgentStatusBar'

describe('AgentStatusBar', () => {
  it('renders session metadata with explicit mode semantics', () => {
    render(
      <AgentStatusBar
        viewingMeta={{
          ref: '#77',
          source: 'codex',
          title: 'Observed Session',
          status: 'active',
          model: 'gpt-5.4',
          externalId: 'ext-77',
          chatMode: 'plan',
          gitBranch: null,
          contextWindow: 200000,
          agentRunId: null,
          workflowName: null,
          agentName: 'triage-agent',
          sessionType: 'terminal',
        }}
        interactionMode="observe"
      />,
    )

    expect(screen.getByText('Codex')).toHaveClass('chat-session-status__source')
    expect(screen.getByText('gpt-5.4')).toBeInTheDocument()
    expect(screen.getByText('tmux')).toBeInTheDocument()
    expect(screen.getByText('Mode: Plan')).toBeInTheDocument()
    expect(screen.getByText('triage-agent')).toBeInTheDocument()
  })

  it('uses the shared primary session action styling for resume', async () => {
    const onAttach = vi.fn()
    const onDetach = vi.fn()

    render(
      <AgentStatusBar
        viewingMeta={{
          ref: '#88',
          source: 'claude',
          title: 'Observed Session',
          status: 'paused',
          model: 'sonnet',
          externalId: 'ext-88',
          chatMode: 'accept_edits',
          gitBranch: null,
          contextWindow: null,
          agentRunId: null,
          workflowName: null,
          agentName: null,
          sessionType: 'terminal',
        }}
        interactionMode="observe"
        onAttach={onAttach}
        onDetach={onDetach}
      />,
    )

    const resumeButton = screen.getByRole('button', { name: 'Resume' })
    expect(resumeButton).toHaveClass('session-pane-action--primary')

    await userEvent.click(resumeButton)
    await userEvent.click(screen.getByRole('button', { name: 'Back' }))

    expect(onAttach).toHaveBeenCalledTimes(1)
    expect(onDetach).toHaveBeenCalledTimes(1)
  })
})
