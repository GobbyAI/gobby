import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { AgentStatusBar } from '../AgentStatusBar'

describe('AgentStatusBar', () => {
  it('renders only the lower-bar state and transport chip', () => {
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

    expect(screen.getByText('Watching live')).toBeInTheDocument()
    expect(screen.getByText('TMUX')).toBeInTheDocument()
    expect(screen.queryByText('#77')).toBeNull()
    expect(screen.queryByText('Observed Session')).toBeNull()
    expect(screen.queryByText('gpt-5.4')).toBeNull()
    expect(screen.queryByText('triage-agent')).toBeNull()
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
    expect(attachButton).toHaveClass('btn', 'btn-secondary', 'btn-sm')
    expect(attachButton).not.toHaveClass('btn-primary')
    expect(resumeButton).toHaveClass('btn', 'btn-secondary', 'btn-sm')
    expect(resumeButton).not.toHaveClass('btn-primary')
    expect(screen.queryByText('#88')).toBeNull()
    expect(screen.queryByText('Observed Session')).toBeNull()

    await userEvent.click(attachButton)
    await userEvent.click(resumeButton)

    expect(onAttach).toHaveBeenCalledTimes(1)
    expect(onResume).toHaveBeenCalledTimes(1)
    expect(onDetach).not.toHaveBeenCalled()
  })

  it('hides the session badge for null session types while keeping the state text', () => {
    render(
      <AgentStatusBar
        viewingMeta={{
          ref: '#91',
          source: 'claude',
          title: 'Observed Session',
          status: 'active',
          model: 'sonnet',
          externalId: 'ext-91',
          chatMode: 'accept_edits',
          gitBranch: null,
          contextWindow: null,
          agentRunId: null,
          workflowName: null,
          agentName: null,
          sessionType: null,
        }}
        interactionMode="observe"
      />,
    )

    expect(screen.getByText('Watching live')).toBeInTheDocument()
    expect(screen.queryByText('TMUX')).toBeNull()
    expect(screen.queryByText('WEB')).toBeNull()
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
    expect(screen.getByText('Attached')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Detach' })).toBeInTheDocument()
  })

  it('renders a fallback activity-panel toggle when requested', async () => {
    const onTogglePanel = vi.fn()

    render(
      <AgentStatusBar
        viewingMeta={{
          ref: '#90',
          source: 'claude',
          title: 'Observed Session',
          status: 'active',
          model: 'sonnet',
          externalId: 'ext-90',
          chatMode: 'accept_edits',
          gitBranch: null,
          contextWindow: null,
          agentRunId: null,
          workflowName: null,
          agentName: null,
          sessionType: 'terminal',
        }}
        interactionMode="observe"
        onTogglePanel={onTogglePanel}
        isPanelPinned={true}
      />,
    )

    await userEvent.click(screen.getByRole('button', { name: 'Hide activity panel' }))

    expect(onTogglePanel).toHaveBeenCalledTimes(1)
  })
})
