import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { AgentStepsEditor, type WorkflowStep } from '../AgentStepsEditor'

describe('AgentStepsEditor advanced JSON fields', () => {
  it('remaps every transition targeting a renamed step', () => {
    const steps: WorkflowStep[] = [
      {
        name: 'source-one',
        allowed_tools: [],
        blocked_tools: [],
        allowed_mcp_tools: [],
        blocked_mcp_tools: [],
        transitions: [{ to: 'target', when: 'first' }],
      },
      {
        name: 'target',
        allowed_tools: [],
        blocked_tools: [],
        allowed_mcp_tools: [],
        blocked_mcp_tools: [],
        transitions: [{ to: 'target', when: 'loop' }],
      },
      {
        name: 'source-two',
        allowed_tools: [],
        blocked_tools: [],
        allowed_mcp_tools: [],
        blocked_mcp_tools: [],
        transitions: [
          { to: 'target', when: 'second' },
          { to: 'source-one', when: 'unchanged' },
        ],
      },
    ]
    const onChange = vi.fn()

    render(<AgentStepsEditor steps={steps} onChange={onChange} />)
    fireEvent.click(screen.getByRole('button', { name: /target/ }))
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'renamed' } })

    expect(onChange).toHaveBeenCalledWith([
      { ...steps[0], transitions: [{ to: 'renamed', when: 'first' }] },
      { ...steps[1], name: 'renamed', transitions: [{ to: 'renamed', when: 'loop' }] },
      {
        ...steps[2],
        transitions: [
          { to: 'renamed', when: 'second' },
          { to: 'source-one', when: 'unchanged' },
        ],
      },
    ])
  })

  it('expands and collapses a step card from the keyboard', async () => {
    const user = userEvent.setup()
    const steps: WorkflowStep[] = [{
      name: 'step-one',
      allowed_tools: [],
      blocked_tools: [],
      allowed_mcp_tools: [],
      blocked_mcp_tools: [],
      transitions: [],
    }]

    render(<AgentStepsEditor steps={steps} onChange={vi.fn()} />)

    const disclosure = screen.getByRole('button', { name: /step-one/ })
    expect(disclosure).toHaveAttribute('aria-expanded', 'false')
    disclosure.focus()
    await user.keyboard('{Enter}')
    expect(disclosure).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByRole('button', { name: 'Delete' })).toBeInTheDocument()

    await user.keyboard(' ')
    expect(disclosure).toHaveAttribute('aria-expanded', 'false')
  })

  it('preserves draft text across rerenders and commits valid arrays on blur', () => {
    const steps: WorkflowStep[] = [{
      name: 'step-one',
      allowed_tools: [],
      blocked_tools: [],
      allowed_mcp_tools: [],
      blocked_mcp_tools: [],
      transitions: [],
    }]
    const onChange = vi.fn()
    const { rerender } = render(<AgentStepsEditor steps={steps} onChange={onChange} />)

    fireEvent.click(screen.getByText('step-one'))
    fireEvent.click(screen.getByRole('button', { name: /Advanced/ }))

    const editor = screen.getByLabelText('on_enter')
    fireEvent.change(editor, { target: { value: '[{' } })
    rerender(<AgentStepsEditor steps={steps} onChange={onChange} />)

    expect(editor).toHaveValue('[{')
    expect(onChange).not.toHaveBeenCalled()

    fireEvent.blur(editor)
    expect(screen.getByRole('alert')).toHaveTextContent('Invalid JSON')
    expect(editor).toHaveAttribute('aria-invalid', 'true')
    expect(onChange).not.toHaveBeenCalled()

    fireEvent.change(editor, { target: { value: '{}' } })
    fireEvent.blur(editor)
    expect(screen.getByRole('alert')).toHaveTextContent('Value must be a JSON array')
    expect(onChange).not.toHaveBeenCalled()

    fireEvent.change(editor, { target: { value: '[{"tool":"Read"}]' } })
    fireEvent.blur(editor)

    expect(screen.queryByRole('alert')).toBeNull()
    expect(onChange).toHaveBeenCalledWith([
      { ...steps[0], on_enter: [{ tool: 'Read' }] },
    ])
  })
})
