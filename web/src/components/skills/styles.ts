export const SOURCE_BADGE_CLS =
  'inline-flex items-center rounded px-1.5 py-px text-[length:var(--text-2xs)] font-medium uppercase tracking-[0.3px]'

export const SOURCE_BADGE_BG: Record<string, string> = {
  filesystem:
    'bg-[color-mix(in_srgb,var(--color-success-foreground)_15%,transparent)] text-[var(--color-success-foreground)]',
  github: 'bg-[color-mix(in_srgb,var(--color-agent)_15%,transparent)] text-[var(--color-agent)]',
  hub: 'bg-[color-mix(in_srgb,var(--color-info)_15%,transparent)] text-[var(--color-info)]',
  zip: 'bg-[color-mix(in_srgb,var(--color-warning-foreground)_15%,transparent)] text-[var(--color-warning-foreground)]',
  local: 'bg-[color-mix(in_srgb,var(--text-muted)_15%,transparent)] text-[var(--text-muted)]',
  url: 'bg-[color-mix(in_srgb,var(--color-error)_15%,transparent)] text-[var(--color-error)]',
  unknown: 'bg-[color-mix(in_srgb,var(--text-muted)_10%,transparent)] text-[var(--text-muted)]',
}

export const FORM_CANCEL_BTN_CLS =
  'cursor-pointer rounded border border-[var(--border)] bg-[var(--bg-secondary)] px-3.5 py-1.5 text-[length:var(--text-base)] text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)] pointer-coarse:min-h-11'

export const FORM_SAVE_BTN_CLS =
  'cursor-pointer rounded border-0 bg-[var(--accent)] px-3.5 py-1.5 text-[length:var(--text-base)] font-medium text-[var(--accent-foreground)] hover:opacity-85 disabled:cursor-not-allowed disabled:opacity-50 pointer-coarse:min-h-11'
