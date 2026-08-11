import { useId, type HTMLAttributes, type ReactNode } from 'react'
import { cn } from '../../lib/utils'

export interface FormFieldRenderProps {
  /** id for the control; pre-wired to the rendered label. */
  id: string
  /** id of the label element, for composite fields that label sub-groups. */
  labelId: string
  /** Space-separated hint/error ids, or undefined when neither is present. */
  describedBy: string | undefined
  /** True when an error is present — feed the control's error/aria-invalid. */
  invalid: boolean
}

export interface FormFieldProps
  extends Omit<HTMLAttributes<HTMLDivElement>, 'children'> {
  label: ReactNode
  hint?: ReactNode
  error?: ReactNode
  /**
   * Composite fields (multiple controls) render a role="group" shell wired
   * via aria-labelledby with a plain-text label instead of a label/htmlFor
   * pair, since no single control can own the association.
   */
  group?: boolean
  children: (field: FormFieldRenderProps) => ReactNode
}

/**
 * Labeled form row: label + control slot + optional hint/error, the shared
 * shell for every field implementation. The render prop hands the control its
 * id, aria-describedby ids, and invalid state so association always survives
 * composition.
 */
export function FormField({
  label,
  hint,
  error,
  group = false,
  className,
  children,
  ...props
}: FormFieldProps) {
  const id = useId()
  const labelId = `${id}-label`
  const hintId = `${id}-hint`
  const errorId = `${id}-error`
  const describedBy =
    [hint ? hintId : undefined, error ? errorId : undefined]
      .filter(Boolean)
      .join(' ') || undefined
  const labelCls = 'text-sm font-medium text-muted-foreground'

  return (
    <div
      className={cn('flex flex-col gap-1.5', className)}
      {...(group ? { role: 'group', 'aria-labelledby': labelId } : {})}
      {...props}
    >
      {group ? (
        <span id={labelId} className={labelCls}>
          {label}
        </span>
      ) : (
        <label id={labelId} htmlFor={id} className={labelCls}>
          {label}
        </label>
      )}
      {children({ id, labelId, describedBy, invalid: Boolean(error) })}
      {hint ? (
        <p id={hintId} className="text-xs text-muted-foreground">
          {hint}
        </p>
      ) : null}
      {error ? (
        <p id={errorId} className="text-xs text-destructive">
          {error}
        </p>
      ) : null}
    </div>
  )
}
