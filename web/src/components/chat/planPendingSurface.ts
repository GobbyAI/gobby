/**
 * Shared color treatment for the "awaiting your approval" surface.
 *
 * Used by both approval surfaces — the Plans panel header
 * (`PlanReviewCard`) and the status-bar strip (`PlanPendingActionStrip`) — so
 * the color direction lives in exactly one place and is a single-line swap.
 *
 * Two candidate treatments were built for #15693; the final pick is made from
 * screenshots of both:
 * - `amber` — neutral surface, amber icon/label only. Keeps the `.impeccable.md`
 *   warning=awaiting-approval semantic with no colored background wash.
 * - `info` — calm blue (hue 250): a subtle `--color-info-soft` tint plus a blue
 *   icon/label. (Changing the hue mapping should be recorded via the
 *   `impeccable` skill's `teach` mode.)
 *
 * Each consuming surface keeps its own shape/spacing/border-side classes and
 * applies these color fragments on top via `cn(...)`.
 */
export type PlanPendingVariant = 'amber' | 'info'

/**
 * The active treatment. Flip this single constant to switch the approval
 * surface color across every surface at once.
 */
export const PLAN_PENDING_VARIANT: PlanPendingVariant = 'info'

interface PlanPendingColors {
  /** Background fill of the surface band/strip. */
  surfaceBg: string
  /** Border color utility — pair with a `border` / `border-b` shape class. */
  borderColor: string
  /** Icon stroke + heading/label text color (the deutan-safe state cue). */
  accentText: string
}

const VARIANTS: Record<PlanPendingVariant, PlanPendingColors> = {
  amber: {
    surfaceBg: 'bg-muted/50',
    borderColor: 'border-[var(--border)]',
    accentText: 'text-[var(--color-warning-foreground)]',
  },
  info: {
    surfaceBg: 'bg-[var(--color-info-soft)]',
    borderColor: 'border-[color-mix(in_srgb,var(--color-info)_22%,transparent)]',
    accentText: 'text-[var(--color-info)]',
  },
}

export const planPendingColors: PlanPendingColors = VARIANTS[PLAN_PENDING_VARIANT]
