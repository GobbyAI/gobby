import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import type { Artifact } from '../../../types/artifacts'
import { PlanReviewCard } from '../../activity/PlanReviewCard'
import { PlanPendingActionStrip } from '../PlanPendingActionStrip'

// Render markdown as plain text so the pending banner is the only thing under test.
vi.mock('../Markdown', () => ({
  Markdown: ({ content }: { content: string }) => <div>{content}</div>,
}))

const here = dirname(fileURLToPath(import.meta.url))
const webSrc = join(here, '..', '..', '..')

function makePlan(): Artifact {
  return {
    id: 'plan-1',
    type: 'text',
    title: 'Plan',
    versions: [{ content: '# Plan', timestamp: new Date(1_700_000_000_000) }],
    currentVersionIndex: 0,
    isPlan: true,
  }
}

/**
 * Design-fix guards for #15637. These pin the regressions this epic introduced
 * (.impeccable.md: awaiting-approval = warning hue 75; state read by
 * lightness/icon first, never hue alone; tokens, never hardcoded colors):
 *  - the awaiting-approval bar uses a warning SURFACE token for its background
 *    (the muddy-brown bug came from misusing the *foreground* token as a fill);
 *  - the status bars stay pinned to --activity-panel-bar-height with no inner
 *    redeclaration, and in-bar plan controls to --status-bar-control-height.
 */
describe('plan-approval design fixes (#15637)', () => {
  it('PlanReviewCard pending bar fills with the warning SURFACE token, not foreground', () => {
    render(
      <PlanReviewCard
        plan={makePlan()}
        planPendingApproval
        onSetVersion={vi.fn()}
      />,
    )
    const banner = screen.getByTestId('plan-review-status')
    expect(banner.getAttribute('data-status')).toBe('pending')
    // Background is the amber surface wash...
    expect(banner.className).toContain('bg-[var(--color-warning-soft)]')
    // ...not the foreground/text token misused as a fill (the brown bug).
    expect(banner.className).not.toContain('bg-[color-mix(in_srgb,var(--color-warning-foreground)')
  })

  it('PlanReviewCard keeps the warning FOREGROUND token for the icon + label', () => {
    render(
      <PlanReviewCard
        plan={makePlan()}
        planPendingApproval
        onSetVersion={vi.fn()}
      />,
    )
    const banner = screen.getByTestId('plan-review-status')
    // The semantic warning hue still carries the icon/label (grayscale-legible:
    // an icon plus the label, never hue alone).
    expect(banner.querySelector('svg')).toBeTruthy()
    expect(banner.innerHTML).toContain('--color-warning-foreground')
  })

  it('PlanPendingActionStrip fills with the warning SURFACE token, not foreground', () => {
    render(
      <PlanPendingActionStrip onApprove={vi.fn()} onRequestChanges={vi.fn()} onView={vi.fn()} />,
    )
    const strip = screen.getByTestId('plan-pending-strip')
    expect(strip.className).toContain('bg-[var(--color-warning-soft)]')
    expect(strip.className).not.toContain('bg-[color-mix(in_srgb,var(--color-warning-foreground)')
  })

  it('status bars share --activity-panel-bar-height and never redeclare it on an inner scope', () => {
    const inputCss = readFileSync(join(webSrc, 'components/chat/styles/input.css'), 'utf8')
    const layoutCss = readFileSync(join(webSrc, 'components/chat/styles/layout.css'), 'utf8')
    const activityCss = readFileSync(join(webSrc, 'components/chat/styles/activity-panel.css'), 'utf8')

    // All three bars key off the single source-of-truth token.
    expect(inputCss).toMatch(/\.agent-status-bar\s*\{[^}]*min-height:\s*var\(--activity-panel-bar-height/)
    expect(layoutCss).toMatch(/\.command-bar\s*\{[^}]*min-height:\s*var\(--activity-panel-bar-height/)
    expect(activityCss).toMatch(/\.activity-panel-tabs\s*\{[^}]*min-height:\s*var\(--activity-panel-bar-height/)

    // The exact failure activity-panel.css warns against: redeclaring the var
    // on an inner scope shadows :root and drifts the bars out of sync.
    expect(activityCss).not.toMatch(/--activity-panel-bar-height\s*:/)
  })

  it('in-bar plan controls are pinned to --status-bar-control-height so they cannot stretch the bar', () => {
    const inputCss = readFileSync(join(webSrc, 'components/chat/styles/input.css'), 'utf8')
    expect(inputCss).toMatch(
      /\.agent-status-bar__plan button\s*\{[^}]*height:\s*var\(--status-bar-control-height/,
    )
  })
})
