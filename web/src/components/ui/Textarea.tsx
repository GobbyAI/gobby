import { forwardRef, type TextareaHTMLAttributes } from 'react'
import { cn } from '../../lib/utils'
import { controlSurfaceCls, controlWrapperCls } from './controlStyles'

export interface TextareaProps
  extends TextareaHTMLAttributes<HTMLTextAreaElement> {
  error?: boolean
  /** Layout overrides for the wrapper label (flex-1, width caps, …). */
  wrapperClassName?: string
}

// No height styling beyond the caller's: auto-grow implementations drive
// height through the forwarded ref, and React must never fight that.
export const Textarea = forwardRef<HTMLTextAreaElement, TextareaProps>(
  ({ className, error, wrapperClassName, ...props }, ref) => {
    return (
      <label className={cn(controlWrapperCls, wrapperClassName)}>
        <textarea
          className={cn(
            'py-2',
            controlSurfaceCls,
            error ? 'border-destructive' : 'border-border',
            className
          )}
          aria-invalid={!!error}
          ref={ref}
          {...props}
        />
      </label>
    )
  }
)
Textarea.displayName = 'Textarea'
