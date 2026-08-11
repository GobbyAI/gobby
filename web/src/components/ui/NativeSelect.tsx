import { forwardRef, type SelectHTMLAttributes } from 'react'
import { cn } from '../../lib/utils'
import { controlSurfaceCls, controlWrapperCls } from './controlStyles'

export interface NativeSelectProps
  extends SelectHTMLAttributes<HTMLSelectElement> {
  error?: boolean
  /** Layout overrides for the wrapper label (flex-1, width caps, …). */
  wrapperClassName?: string
}

/**
 * Native <select> on the shared control contract. Form contexts compose this
 * (via SelectField); toolbar/picker contexts use the Radix Select instead —
 * that split is the app-wide rule.
 */
export const NativeSelect = forwardRef<HTMLSelectElement, NativeSelectProps>(
  ({ className, error, wrapperClassName, children, ...props }, ref) => {
    return (
      <label className={cn(controlWrapperCls, wrapperClassName)}>
        <select
          className={cn(
            'h-9 py-1',
            controlSurfaceCls,
            error ? 'border-destructive' : 'border-border',
            className
          )}
          aria-invalid={!!error}
          ref={ref}
          {...props}
        >
          {children}
        </select>
      </label>
    )
  }
)
NativeSelect.displayName = 'NativeSelect'
