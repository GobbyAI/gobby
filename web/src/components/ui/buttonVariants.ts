import { cva } from 'class-variance-authority'

// Canonical button appearance — exact parity with the retiring .btn ladder
// (styles/buttons.css) so migrated call sites are visually lossless. Every
// variant sets its own border-color: two border-color utilities in one class
// list would be resolved by stylesheet order, not class order.
export const buttonVariants = cva(
  'inline-flex items-center justify-center whitespace-nowrap select-none rounded-md border font-medium leading-none transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-background disabled:pointer-events-none disabled:opacity-50',
  {
    variants: {
      variant: {
        // Bordered, transparent — the default for ordinary action buttons.
        secondary:
          'border-border bg-transparent text-muted-foreground hover:bg-muted hover:text-foreground hover:border-[var(--text-muted)]',
        // Solid brand accent. Reserve for the single dominant CTA on a surface
        // (chat Send, Approve (YOLO)). Everything else uses `accent`.
        primary: 'border-transparent bg-accent text-accent-foreground hover:bg-accent-hover',
        // Tinted accent — the canonical action style (mirrors .btn-accent, the
        // New Chat / Hide Panel / Hide Chat reference buttons). Default choice
        // for meaningful actions that are not the page's primary CTA.
        accent:
          'border-[color-mix(in_srgb,var(--accent)_35%,transparent)] bg-[var(--accent-tint)] text-accent hover:bg-[var(--accent-soft)] hover:border-[color-mix(in_srgb,var(--accent)_55%,transparent)]',
        // Quiet destructive: transparent until hover reveals the magenta error
        // surface, so weight is clear before commit.
        destructive: 'border-transparent bg-transparent text-error hover:bg-error-soft',
        outline: 'border-border bg-transparent text-foreground hover:bg-muted',
        // Borderless, transparent — tertiary actions in dense rows.
        ghost: 'border-transparent bg-transparent text-muted-foreground hover:bg-muted hover:text-foreground',
      },
      size: {
        sm: 'min-h-7 gap-1.5 px-2.5 text-xs',
        md: 'min-h-8 gap-1.5 px-3.5 text-sm',
        lg: 'min-h-10 gap-2 px-4.5 text-base',
        icon: 'min-h-8 w-8 gap-1.5 p-0 text-sm',
      },
      // Coarse pointers promote every button to the 44px touch floor. `dense`
      // strips the promotion for desktop-only chrome (activity-panel toolbars,
      // app-header cluster) where rows must stay compact.
      dense: {
        false: 'pointer-coarse:min-h-11 pointer-coarse:min-w-11',
        true: '',
      },
    },
    defaultVariants: {
      variant: 'secondary',
      size: 'md',
      dense: false,
    },
  },
)
