import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { AgentStatusBar } from '../AgentStatusBar'

describe('AgentStatusBar', () => {
  it('renders only the remaining session metadata chips', () => {
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

    expect(screen.getByText('gpt-5.4')).toBeInTheDocument()
    expect(screen.getByText('tmux')).toBeInTheDocument()
    expect(screen.getByText('triage-agent')).toBeInTheDocument()
    expect(screen.queryByText('Watching live')).toBeNull()
    expect(screen.queryByText('Codex')).toBeNull()
    expect(screen.queryByText('Mode: Plan')).toBeNull()
    expect(screen.queryByText('#77')).toBeNull()
    expect(screen.queryByText('Observed Session')).toBeNull()
  })

  it('uses the shared neutral session action styling for resume', async () => {
    const onAttach = vi.fn()
    const onResume = vi.fn()
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
        onResume={onResume}
        onDetach={onDetach}
      />,
    )

    const attachButton = screen.getByRole('button', { name: 'Attach' })
    const resumeButton = screen.getByRole('button', { name: 'Resume' })
    expect(attachButton).toHaveClass('session-pane-action')
    expect(attachButton).not.toHaveClass('session-pane-action--primary')
    expect(resumeButton).toHaveClass('session-pane-action')
    expect(resumeButton).not.toHaveClass('session-pane-action--primary')
    expect(screen.queryByText('#88')).toBeNull()
    expect(screen.queryByText('Observed Session')).toBeNull()

    await userEvent.click(attachButton)
    await userEvent.click(resumeButton)

    expect(onAttach).toHaveBeenCalledTimes(1)
    expect(onResume).toHaveBeenCalledTimes(1)
    expect(onDetach).not.toHaveBeenCalled()
  })

  it('shows only Detach while attached', () => {
    const onDetach = vi.fn()

    render(
      <AgentStatusBar
        viewingMeta={{
          ref: '#89',
          source: 'claude',
          title: 'Attached Session',
          status: 'active',
          model: 'sonnet',
          externalId: 'ext-89',
          chatMode: 'accept_edits',
          gitBranch: null,
          contextWindow: null,
          agentRunId: null,
          workflowName: null,
          agentName: null,
          sessionType: 'terminal',
        }}
        interactionMode="proxy"
        isAttached={true}
        onAttach={vi.fn()}
        onResume={vi.fn()}
        onDetach={onDetach}
      />,
    )

    expect(screen.queryByRole('button', { name: 'Attach' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Resume' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Detach' })).toBeInTheDocument()
  })
})
