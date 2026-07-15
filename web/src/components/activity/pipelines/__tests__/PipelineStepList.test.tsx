import { useState } from 'react'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import type { PipelineStep } from '../PipelineEditor.types'
import { PipelineStepList } from '../PipelineStepList'

function Harness({ initialSteps }: { initialSteps: PipelineStep[] }) {
  const [steps, setSteps] = useState(initialSteps)
  const [expandedIndex, setExpandedIndex] = useState<number | null>(null)

  return (
    <PipelineStepList
      steps={steps}
      expandedIndex={expandedIndex}
      onExpandedIndexChange={setExpandedIndex}
      onUpdateStep={(index, updates) =>
        setSteps((current) =>
          current.map((step, stepIndex) =>
            stepIndex === index ? { ...step, ...updates } : step,
          ),
        )
      }
      onDeleteStep={() => undefined}
      onMoveStep={() => undefined}
      onChangeStepType={() => undefined}
      onAddStep={() => undefined}
    />
  )
}

describe('PipelineStepList row identity', () => {
  it('keeps a step expanded and its ID input focused while editing', async () => {
    const user = userEvent.setup()
    render(<Harness initialSteps={[{ id: 'old-id', exec: 'true' }]} />)

    await user.click(screen.getByRole('button', { name: /old-id/ }))
    const input = screen.getByLabelText('Step ID')
    await user.clear(input)

    expect(input).toHaveValue('')
    expect(input).toHaveFocus()

    await user.type(input, 'new-id')

    expect(input).toHaveValue('new-id')
    expect(input).toHaveFocus()
    expect(screen.getByRole('button', { name: /new-id/ })).toHaveAttribute(
      'aria-expanded',
      'true',
    )
  })

  it('renders and independently expands steps with duplicate IDs', async () => {
    const user = userEvent.setup()
    render(
      <Harness
        initialSteps={[
          { id: 'duplicate', exec: 'first' },
          { id: 'duplicate', exec: 'second' },
        ]}
      />,
    )

    const headers = screen.getAllByRole('button', { name: /duplicate/ })
    expect(headers).toHaveLength(2)

    await user.click(headers[1])

    expect(headers[0]).toHaveAttribute('aria-expanded', 'false')
    expect(headers[1]).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByLabelText('Step ID')).toHaveValue('duplicate')
  })
})

describe('PipelineStepList step-type badge', () => {
  it('renders a tokenized tinted background for the step-type badge', () => {
    render(
      <PipelineStepList
        steps={[{ id: 'run-command', exec: 'echo hello' }]}
        expandedIndex={null}
        onExpandedIndexChange={vi.fn()}
        onUpdateStep={vi.fn()}
        onDeleteStep={vi.fn()}
        onMoveStep={vi.fn()}
        onChangeStepType={vi.fn()}
        onAddStep={vi.fn()}
      />,
    )

    const badge = screen.getByText('exec')
    expect(badge.style.color).toBe('var(--step-type-exec)')
    expect(badge.style.background).toBe(
      'color-mix(in srgb, var(--step-type-exec) 12%, transparent)',
    )
  })
})
