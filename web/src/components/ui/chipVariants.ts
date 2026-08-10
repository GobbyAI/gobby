import { cva, type VariantProps } from 'class-variance-authority'

// Canonical status-chip appearance — byte parity with the retiring `.chip`
// geometry (height 1.25rem, padding-inline 0.375rem, pill radius,
// --text-2xs, weight 600). One geometry: every status-chip family measured
// identical, so there is deliberately no size axis.
//
// The tone ladder is the .impeccable.md state palette. Success rides the
// accent tone: it shares the brand hue by design and the ladder keeps the
// tones pairwise lightness-separated in dark, which success-foreground's
// lightness would collapse.
export const chipVariants = cva(
  'inline-flex h-5 items-center justify-center whitespace-nowrap rounded-full px-1.5 text-[length:var(--text-2xs)] font-semibold leading-none tracking-[0]',
  {
    variants: {
      tone: {
        neutral:
          'bg-[color-mix(in_srgb,var(--text-muted)_15%,transparent)] text-[color:var(--text-muted)]',
        accent:
          'bg-[color-mix(in_srgb,var(--accent)_15%,transparent)] text-[color:var(--accent)]',
        info: 'bg-[color-mix(in_srgb,var(--color-info)_15%,transparent)] text-[color:var(--color-info)]',
        warning:
          'bg-[color-mix(in_srgb,var(--color-warning-foreground)_15%,transparent)] text-[color:var(--color-warning-foreground)]',
        error:
          'bg-[color-mix(in_srgb,var(--color-error)_15%,transparent)] text-[color:var(--color-error)]',
      },
      uppercase: {
        true: 'uppercase',
        false: '',
      },
    },
    defaultVariants: {
      tone: 'neutral',
      uppercase: false,
    },
  },
)

export type ChipTone = NonNullable<VariantProps<typeof chipVariants>['tone']>

// Session identity chips (WEB/TMUX/ACP/auto/SB/LOCAL) — mono bordered pills
// composed onto tone="accent". Dark keeps the accent "on" fill (byte-frozen
// to IMG_3754); light reads identity metadata as neutral because accent is
// reserved for applied filters/constraints there.
export const chipIdentityClasses =
  'border border-border font-mono [[data-theme=light]_&]:bg-muted [[data-theme=light]_&]:text-muted-foreground'
