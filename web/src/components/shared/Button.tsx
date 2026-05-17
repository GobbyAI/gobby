import { forwardRef, type ButtonHTMLAttributes } from 'react'
import { Slot } from '@radix-ui/react-slot'
import { type VariantProps } from 'class-variance-authority'
import { cn } from '../../lib/utils'
import { buttonVariants } from './buttonVariants'

export interface ButtonProps
  extends ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  /**
   * Render through Radix Slot so caller-supplied elements receive button
   * styling and state props. Loading disables the control, sets aria-busy,
   * and injects a leading spinner before children.
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
        {loading && (
          <span
            aria-hidden="true"
            className="mr-2 size-3 animate-spin rounded-full border border-current border-t-transparent motion-reduce:animate-none"
          />
        )}
        {children}
      </Comp>
    )
  },
)
Button.displayName = 'Button'
