import { useState } from 'react'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import { CommonFields, McpFields } from '../PipelineStepFields'
import type { PipelineStep } from '../PipelineEditor.types'

function McpHarness({ initialStep }: { initialStep: PipelineStep }) {
  const [step, setStep] = useState(initialStep)

  return (
    <>
      <McpFields
        step={step}
        onChange={(updates) => setStep((current) => ({ ...current, ...updates }))}
      />
      <output aria-label="Step state">{JSON.stringify(step)}</output>
    </>
  )
}

function CommonHarness() {
  const [step, setStep] = useState<PipelineStep>({ id: 'prompt', prompt: '' })

  return (
    <>
      <CommonFields
        step={step}
        type="prompt"
        onChange={(updates) => setStep((current) => ({ ...current, ...updates }))}
      />
      <output aria-label="Step state">{JSON.stringify(step)}</output>
    </>
  )
}

function stepState(): PipelineStep {
  return JSON.parse(screen.getByRole('status', { name: 'Step state' }).textContent ?? '{}')
}

describe('PipelineStepFields drafts', () => {
  it('keeps a newly added argument row while its value is typed before its key', async () => {
    const user = userEvent.setup()
    render(<McpHarness initialStep={{ id: 'mcp', mcp: { arguments: {} } }} />)

    await user.click(screen.getByRole('button', { name: 'Add Arguments row' }))
    const keyInput = screen.getByRole('textbox', { name: 'Arguments key 1' })
    const valueInput = screen.getByRole('textbox', { name: 'Arguments value 1' })

    await user.type(valueInput, 'secret')

    expect(keyInput).toBeInTheDocument()
    expect(valueInput).toHaveValue('secret')
    expect(stepState().mcp).toEqual({ arguments: {} })
  })

  it('keeps a newly added argument row while its key is typed', async () => {
    const user = userEvent.setup()
    render(<McpHarness initialStep={{ id: 'mcp', mcp: { arguments: {} } }} />)

    await user.click(screen.getByRole('button', { name: 'Add Arguments row' }))
    const keyInput = screen.getByRole('textbox', { name: 'Arguments key 1' })
    const valueInput = screen.getByRole('textbox', { name: 'Arguments value 1' })

    await user.type(keyInput, 'token')

    expect(keyInput).toHaveValue('token')
    expect(valueInput).toBeInTheDocument()
    expect(stepState().mcp).toEqual({ arguments: {} })

    await user.tab()

    expect(stepState().mcp).toEqual({ arguments: { token: '' } })
  })

  it('keeps an existing argument value until an empty key is committed', async () => {
    const user = userEvent.setup()
    render(
      <McpHarness
        initialStep={{ id: 'mcp', mcp: { arguments: { token: 'secret' } } }}
      />,
    )

    const keyInput = screen.getByRole('textbox', { name: 'Arguments key 1' })
    const valueInput = screen.getByRole('textbox', { name: 'Arguments value 1' })

    await user.clear(keyInput)

    expect(keyInput).toHaveValue('')
    expect(valueInput).toHaveValue('secret')
    expect(stepState().mcp).toEqual({ arguments: { token: 'secret' } })

    await user.tab()

    expect(screen.queryByRole('textbox', { name: 'Arguments key 1' })).not.toBeInTheDocument()
    expect(stepState().mcp).toEqual({ arguments: {} })
  })

  it('preserves condition and tools keystrokes until blur normalizes them', async () => {
    const user = userEvent.setup()
    render(<CommonHarness />)

    const conditionInput = screen.getByLabelText('Condition')
    await user.type(conditionInput, 'inputs.ready ')

    expect(conditionInput).toHaveValue('inputs.ready ')
    expect(stepState().condition).toBeUndefined()

    await user.tab()

    expect(screen.getByLabelText('Condition')).toHaveValue('inputs.ready')
    expect(stepState().condition).toBe('${{ inputs.ready }}')

    const toolsInput = screen.getByLabelText('Tools')
    await user.type(toolsInput, 'read, write ')

    expect(toolsInput).toHaveValue('read, write ')
    expect(stepState().tools).toBeUndefined()

    await user.tab()

    expect(screen.getByLabelText('Tools')).toHaveValue('read, write')
    expect(stepState().tools).toEqual(['read', 'write'])
  })
})
