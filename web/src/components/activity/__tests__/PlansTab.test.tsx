import { describe, it, expect, vi } from 'vitest'
import { type ComponentProps } from 'react'
import { render, screen, fireEvent } from '@testing-library/react'
import { PlansTab } from '../PlansTab'
import type { Artifact } from '../../../types/artifacts'

// Render the markdown as plain text so plan-content assertions are deterministic.
vi.mock('../../chat/Markdown', () => ({
  Markdown: ({ content }: { content: string }) => <div data-testid="markdown">{content}</div>,
}))

function makePlan(contents: string[]): Artifact {
  return {
    id: 'plan-1',
    type: 'text',
    title: 'Plan',
    versions: contents.map((content, i) => ({
      content,
      timestamp: new Date(1_700_000_000_000 + i * 1000),
    })),
    currentVersionIndex: contents.length - 1,
    isPlan: true,
  }
}

function renderPlansTab(
  plan: Artifact,
  overrides: Partial<ComponentProps<typeof PlansTab>> = {},
) {
  const artifacts = new Map<string, Artifact>([[plan.id, plan]])
  const props: ComponentProps<typeof PlansTab> = {
    artifacts,
    artifact: plan,
    onOpenArtifact: vi.fn(),
    onClose: vi.fn(),
    onUpdateContent: vi.fn(),
    onSetVersion: vi.fn(),
    onApprovePlan: vi.fn(),
    onRequestPlanChanges: vi.fn(),
    planPendingApproval: true,
    ...overrides,
  }
  return { ...render(<PlansTab {...props} />), props }
}

describe('PlansTab', () => {
  it('renders the empty state when there are no plans', () => {
    render(
      <PlansTab
        artifacts={new Map()}
        artifact={null}
        onOpenArtifact={vi.fn()}
        onSetVersion={vi.fn()}
      />,
    )
    expect(screen.getByText('Plans')).toBeInTheDocument()
  })

  it('renders the pending approval card with plan text and actions', () => {
    renderPlansTab(makePlan(['# Plan\n\nStep 1 details']))

    const status = screen.getByTestId('plan-review-status')
    expect(status).toHaveAttribute('data-status', 'pending')
    expect(screen.getByText('Awaiting your approval')).toBeInTheDocument()
    expect(screen.getByTestId('markdown')).toHaveTextContent('Step 1 details')
    expect(screen.getByTestId('plan-review-approve')).toBeInTheDocument()
    expect(screen.getByTestId('plan-review-request-changes')).toBeInTheDocument()

    // BAN 1: no left/right side-stripe accent on the card.
    expect(status.className).not.toContain('border-l')
    expect(status.className).not.toContain('border-r')
    // Grayscale-legible: state carried by an icon + the warning token, not hue alone.
    expect(status.querySelector('svg')).toBeTruthy()
    expect(status.className).toContain('--color-warning-foreground')
  })

  it('fires onApprovePlan when approve is clicked', () => {
    const { props } = renderPlansTab(makePlan(['plan body']))
    fireEvent.click(screen.getByTestId('plan-review-approve'))
    expect(props.onApprovePlan).toHaveBeenCalledTimes(1)
  })

  it('fires onRequestPlanChanges with the entered feedback', () => {
    const { props } = renderPlansTab(makePlan(['plan body']))

    fireEvent.click(screen.getByTestId('plan-review-request-changes'))
    fireEvent.change(screen.getByTestId('plan-review-feedback'), {
      target: { value: 'Tighten step 2' },
    })
    fireEvent.click(screen.getByTestId('plan-review-send'))

    expect(props.onRequestPlanChanges).toHaveBeenCalledWith('Tighten step 2')
  })

  it('shows revision history across reject -> revise cycles and navigates versions', () => {
    const { props } = renderPlansTab(makePlan(['v1', 'v2', 'v3']))

    expect(screen.getByRole('region', { name: /revision history/i })).toBeInTheDocument()
    expect(screen.getByText('Revision 1')).toBeInTheDocument()
    expect(screen.getByText('Revision 3')).toBeInTheDocument()

    fireEvent.click(screen.getByText('Revision 1'))
    expect(props.onSetVersion).toHaveBeenCalledWith('plan-1', 0)
  })

  it('shows an approved state after the plan is approved', () => {
    const { props, rerender } = renderPlansTab(makePlan(['plan body']), {
      planPendingApproval: true,
    })
    expect(screen.getByTestId('plan-review-status')).toHaveAttribute('data-status', 'pending')

    rerender(<PlansTab {...props} planPendingApproval={false} />)

    const status = screen.getByTestId('plan-review-status')
    expect(status).toHaveAttribute('data-status', 'approved')
    expect(screen.getByText('Plan approved')).toBeInTheDocument()
    expect(screen.queryByTestId('plan-review-approve')).not.toBeInTheDocument()
    // Approved state is also grayscale-legible (check icon) and stripe-free.
    expect(status.querySelector('svg')).toBeTruthy()
    expect(status.className).not.toContain('border-l')
  })
})
