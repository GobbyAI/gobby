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
  it('labels the artifact action and gives it a coarse-pointer floor', () => {
    renderWithProviders(
      <ToolCallCards
        toolCalls={[
          makeCall({
            id: 'read-artifact',
            tool_name: 'Read',
            status: 'completed',
            arguments: { file_path: '/tmp/example.txt' },
            result: { content: '1→artifact content', kind: 'text', truncated: false },
          }),
        ]}
      />,
    )

    expect(screen.getByRole('button', { name: 'Open file in artifacts panel' })).toHaveClass(
      'pointer-coarse:min-h-11',
      'pointer-coarse:min-w-11',
    )
  })

  it('expands a single tool card with the keyboard and exposes its result', () => {
    renderWithProviders(
      <ToolCallCards
        toolCalls={[
          makeCall({
            id: 'read-1',
            tool_name: 'Read',
            status: 'completed',
            arguments: { file_path: '/tmp/example.txt' },
            result: { content: 'keyboard-accessible result', kind: 'text', truncated: false },
          }),
        ]}
      />,
    )

    const header = screen.getByRole('button', { name: /Read/ })
    expect(header).toHaveAttribute('tabindex', '0')
    expect(header).toHaveAttribute('aria-expanded', 'false')

    header.focus()
    fireEvent.keyDown(header, { key: 'Enter' })

    expect(header).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByText('keyboard-accessible result')).toBeInTheDocument()
  })

  it('toggles a grouped tool-call header with Space and Enter', () => {
    const calls = ['one', 'two', 'three'].map((content, index) => makeCall({
      id: `read-group-${index}`,
      tool_name: 'Read',
      status: 'completed',
      result: { content, kind: 'text', truncated: false },
    }))
    renderWithProviders(<ToolCallCards toolCalls={calls} />)

    const header = screen.getByRole('button', { name: /Read.*×3/ })
    expect(header).toHaveAttribute('tabindex', '0')
    expect(header).toHaveAttribute('aria-expanded', 'true')

    header.focus()
    fireEvent.keyDown(header, { key: ' ' })
    expect(header).toHaveAttribute('aria-expanded', 'false')

    fireEvent.keyDown(header, { key: 'Enter' })
    expect(header).toHaveAttribute('aria-expanded', 'true')
  })

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
    expect(screen.queryByText('Render error')).not.toBeInTheDocument()
  })
})
