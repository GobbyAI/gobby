import { describe, it, expect, vi, beforeEach } from 'vitest'
import { type ComponentProps } from 'react'
import { render, screen, fireEvent } from '@testing-library/react'
import { PlansTab } from '../PlansTab'
import type { Artifact } from '../../../types/artifacts'

// Render the markdown as plain text so plan-content assertions are deterministic.
vi.mock('../../chat/Markdown', () => ({
  Markdown: ({ content }: { content: string }) => <div data-testid="markdown">{content}</div>,
}))

// Plan-approval actions live on the agent status bar on desktop and ALSO in the
// Plans activity panel on mobile (#15634). Drive useIsMobile via matchMedia +
// innerWidth so the panel renders its actions only in the mobile viewport.
function setViewport(mobile: boolean) {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: mobile,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }))
  Object.defineProperty(window, 'innerWidth', {
    value: mobile ? 480 : 1280,
    configurable: true,
    writable: true,
  })
}

beforeEach(() => setViewport(false))

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

  it('renders the pending card with plan text but no panel actions on desktop', () => {
    renderPlansTab(makePlan(['# Plan\n\nStep 1 details']))

    const status = screen.getByTestId('plan-review-status')
    expect(status).toHaveAttribute('data-status', 'pending')
    expect(screen.getByText('Awaiting your approval')).toBeInTheDocument()
    expect(screen.getByTestId('markdown')).toHaveTextContent('Step 1 details')
    // Desktop: approve / request-changes live on the agent status bar, not here.
    expect(screen.queryByTestId('plan-review-approve')).not.toBeInTheDocument()
    expect(screen.queryByTestId('plan-review-request-changes')).not.toBeInTheDocument()

    // BAN 1: no left/right side-stripe accent on the card.
    expect(status.className).not.toContain('border-l')
    expect(status.className).not.toContain('border-r')
    // Grayscale-legible: state carried by an icon + the warning token, not hue alone.
    expect(status.querySelector('svg')).toBeTruthy()
    expect(status.className).toContain('--color-warning-foreground')
  })

  it('renders approve / request-changes in the panel on mobile', () => {
    setViewport(true)
    renderPlansTab(makePlan(['# Plan\n\nStep 1 details']))
    expect(screen.getByTestId('plan-review-approve')).toBeInTheDocument()
    expect(screen.getByTestId('plan-review-request-changes')).toBeInTheDocument()
  })

  it('fires onApprovePlan when approve is clicked (mobile)', () => {
    setViewport(true)
    const { props } = renderPlansTab(makePlan(['plan body']))
    fireEvent.click(screen.getByTestId('plan-review-approve'))
    expect(props.onApprovePlan).toHaveBeenCalledTimes(1)
  })

  it('fires onRequestPlanChanges with the entered feedback (mobile)', () => {
    setViewport(true)
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

    // Backend plan_approved => pending clears AND the authoritative
    // planApproved signal flips true.
    rerender(<PlansTab {...props} planPendingApproval={false} planApproved={true} />)

    const status = screen.getByTestId('plan-review-status')
    expect(status).toHaveAttribute('data-status', 'approved')
    expect(screen.getByText('Plan approved')).toBeInTheDocument()
    expect(screen.queryByTestId('plan-review-approve')).not.toBeInTheDocument()
    // Approved state is also grayscale-legible (check icon) and stripe-free.
    expect(status.querySelector('svg')).toBeTruthy()
    expect(status.className).not.toContain('border-l')
  })

  it('does NOT show approved when Request Changes clears pending from the status bar (#15681)', () => {
    const { props, rerender } = renderPlansTab(makePlan(['plan body']), {
      planPendingApproval: true,
    })
    expect(screen.getByTestId('plan-review-status')).toHaveAttribute('data-status', 'pending')

    // Desktop Request Changes lives on the agent status bar, not the card, so
    // it clears pending WITHOUT an approval. The card must fall back to idle —
    // a rejection must never render as "Plan approved" (sibling of #15663).
    rerender(<PlansTab {...props} planPendingApproval={false} planApproved={false} />)

    expect(screen.queryByText('Plan approved')).not.toBeInTheDocument()
    expect(screen.queryByTestId('plan-review-status')).not.toBeInTheDocument()
  })
})
