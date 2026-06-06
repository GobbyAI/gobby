import { cva } from 'class-variance-authority'

export const buttonVariants = cva(
  'inline-flex items-center justify-center rounded-md text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-background disabled:pointer-events-none disabled:opacity-50',
  {
    variants: {
      variant: {
        default: 'bg-foreground text-background hover:bg-foreground/90',
        // Solid brand accent. Reserve for the single dominant CTA on a surface
        // (chat Send, Approve (YOLO)). Everything else uses `accent`.
        primary: 'bg-accent text-accent-foreground hover:bg-accent-hover',
        // Tinted accent — the canonical action style (mirrors .btn-accent, the
        // New Chat / Hide Panel / Hide Chat reference buttons). Default choice
        // for meaningful actions that are not the page's primary CTA.
        accent:
          'border border-[color-mix(in_srgb,var(--accent)_35%,transparent)] bg-[var(--accent-tint)] text-accent hover:bg-[var(--accent-soft)] hover:border-[color-mix(in_srgb,var(--accent)_55%,transparent)]',
        // Quiet destructive (mirrors .btn-destructive): transparent until hover
        // reveals the magenta error surface, so weight is clear before commit.
        destructive:
          'border border-transparent bg-transparent text-error hover:bg-error-soft',
        outline: 'border border-border bg-transparent hover:bg-muted text-foreground',
        ghost: 'hover:bg-muted text-foreground',
      },
      size: {
        sm: 'h-8 px-3 text-xs pointer-coarse:min-h-11 pointer-coarse:min-w-11',
        md: 'h-9 px-4 pointer-coarse:min-h-11 pointer-coarse:min-w-11',
        lg: 'h-10 px-6 text-base pointer-coarse:min-h-11 pointer-coarse:min-w-11',
        icon: 'h-9 w-9 pointer-coarse:min-h-11 pointer-coarse:min-w-11',
      },
    },
    defaultVariants: {
      variant: 'default',
      size: 'md',
    },
  },
)
