import { forwardRef, type ButtonHTMLAttributes } from 'react'
import { Slot } from '@radix-ui/react-slot'
import { type VariantProps } from 'class-variance-authority'
import { cn } from '../../lib/utils'
import { buttonVariants } from './buttonVariants'

const BUTTON_SPINNER_CLASS_NAME =
  'mr-2 size-3 animate-spin rounded-full border border-current border-t-transparent motion-reduce:animate-none'

export interface ButtonProps
  extends ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  /**
   * Render through Radix Slot so caller-supplied elements receive button
   * styling and state props. Loading disables the slotted control and sets
   * aria-busy, but does not inject a spinner because Slot requires one child.
   */
  asChild?: boolean
  loading?: boolean
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, loading = false, disabled, children, ...props }, ref) => {
    const Comp = asChild ? Slot : 'button'
    return (
      <Comp
        className={cn(buttonVariants({ variant, size, className }))}
        ref={ref}
        disabled={disabled || loading}
        aria-busy={loading || undefined}
        {...props}
      >
        {asChild ? (
          // Radix Slot enforces a single element child, so the spinner is
          // never injected as a sibling here. State still propagates via the
          // disabled / aria-busy props above.
          children
        ) : (
          <>
            {loading && (
              <span
                aria-hidden="true"
                className={BUTTON_SPINNER_CLASS_NAME}
              />
            )}
            {children}
          </>
        )}
      </Comp>
    )
  },
)
Button.displayName = 'Button'
