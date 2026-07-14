import { fireEvent } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { ToolCall } from '../../../types/chat'
import { classifyTool } from '../../../types/chat'
import { renderWithProviders, screen } from '../../../test/helpers'
import { ToolCallCards } from '../ToolCallCard'

function makeCall(overrides: Partial<ToolCall> & { id: string; tool_name: string }): ToolCall {
  return {
    server_name: 'builtin',
    status: 'completed',
    tool_type: classifyTool(overrides.tool_name),
    ...overrides,
  }
}

describe('ToolCallCard interactions', () => {
  it('dispatches approval once and disables every decision button after success', () => {
    const onRespondToApproval = vi.fn(() => true)

    renderWithProviders(
      <ToolCallCards
        toolCalls={[
          makeCall({ id: 'approval-1', tool_name: 'Bash', status: 'pending_approval' }),
        ]}
        onRespondToApproval={onRespondToApproval}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))

    expect(onRespondToApproval).toHaveBeenCalledOnce()
    expect(onRespondToApproval).toHaveBeenCalledWith('approval-1', 'approve')
    expect(screen.getByRole('button', { name: 'Approve' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Always Approve' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Reject' })).toBeDisabled()

    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    fireEvent.click(screen.getByRole('button', { name: 'Reject' }))
    expect(onRespondToApproval).toHaveBeenCalledOnce()
  })

  it('shows a disconnect error and leaves decision buttons available for retry', () => {
    const onRespondToApproval = vi.fn(() => false)

    renderWithProviders(
      <ToolCallCards
        toolCalls={[
          makeCall({ id: 'approval-2', tool_name: 'Bash', status: 'pending_approval' }),
        ]}
        onRespondToApproval={onRespondToApproval}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Reject' }))

    expect(onRespondToApproval).toHaveBeenCalledWith('approval-2', 'reject')
    expect(screen.getByText('Disconnected — reconnecting...')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Approve' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Reject' })).toBeEnabled()
  })

  it('submits multi-select and Other answers once, then disables the choices', () => {
    const onRespond = vi.fn(() => true)
    const questions = [
      {
        header: 'Tools',
        question: 'Which tools?',
        multiSelect: true,
        options: [
          { label: 'Read', description: 'Inspect files' },
          { label: 'Edit', description: 'Change files' },
        ],
      },
      {
        header: 'Note',
        question: 'Anything else?',
        multiSelect: false,
        options: [{ label: 'Nothing', description: 'No extra note' }],
      },
    ]

    renderWithProviders(
      <ToolCallCards
        toolCalls={[
          makeCall({
            id: 'question-1',
            tool_name: 'AskUserQuestion',
            status: 'calling',
            arguments: { questions },
          }),
        ]}
        onRespond={onRespond}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: /Read/ }))
    fireEvent.click(screen.getByRole('button', { name: /Edit/ }))
    fireEvent.click(screen.getAllByRole('button', { name: 'Other' })[1])
    fireEvent.change(screen.getByPlaceholderText('Type your answer...'), {
      target: { value: 'Use the indexed search first' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Submit' }))

    expect(onRespond).toHaveBeenCalledOnce()
    expect(onRespond).toHaveBeenCalledWith('question-1', {
      'Which tools?': 'Read, Edit',
      'Anything else?': 'Use the indexed search first',
    })
    expect(screen.getByRole('button', { name: /Read/ })).toBeDisabled()
    expect(screen.getAllByRole('button', { name: 'Other' })[0]).toBeDisabled()
    expect(screen.queryByRole('button', { name: 'Submit' })).not.toBeInTheDocument()
  })

  it('renders array-valued answers from completed question results', () => {
    renderWithProviders(
      <ToolCallCards
        toolCalls={[
          makeCall({
            id: 'question-2',
            tool_name: 'AskUserQuestion',
            status: 'completed',
            arguments: {
              questions: [
                {
                  header: 'Tools',
                  question: 'Which tools?',
                  multiSelect: true,
                  options: [{ label: 'Read' }, { label: 'Edit' }],
                },
              ],
            },
            result: {
              kind: 'json',
              content: { answers: { 'Which tools?': ['Read', 'Edit'] } },
              truncated: false,
            },
          }),
        ]}
      />,
    )

    expect(screen.getByText('Answered')).toBeInTheDocument()
    expect(screen.getByText('Read').parentElement).toHaveClass('border-accent')
    expect(screen.getByText('Edit').parentElement).toHaveClass('border-accent')
  })
})
