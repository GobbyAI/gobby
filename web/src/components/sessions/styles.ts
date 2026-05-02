export const MODEL_BADGE_CLS =
  'rounded-sm bg-[var(--bg-tertiary)] px-1 py-px font-[inherit] text-[length:var(--text-2xs)] text-[var(--text-muted)]'

export const META_COUNT_CLS =
  'font-[inherit] text-[length:var(--text-xs)] text-[var(--text-muted)]'

export const STATUS_BADGE_CLS =
  'rounded-full px-2 py-0.5 text-[length:var(--text-xs)] font-medium uppercase tracking-[0.03em]'

export const STATUS_BADGE_BG: Record<string, string> = {
  active: 'bg-[var(--color-success-soft)] text-[var(--color-success-foreground)]',
  archived: 'bg-[var(--bg-tertiary)] text-[var(--text-muted)]',
  handoff_ready: 'bg-[var(--color-warning-soft)] text-[var(--color-warning-foreground)]',
  expired: 'bg-[var(--color-error-soft)] text-[var(--color-error)]',
}
