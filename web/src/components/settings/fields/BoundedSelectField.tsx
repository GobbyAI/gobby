import { SelectField } from '../../activity/fields'
import type { FieldOption } from '../../activity/fields'

export interface BoundedSelectFieldProps {
  label: string
  value: string
  onChange: (value: string) => void
  options: FieldOption[]
  ariaLabel: string
  disabled?: boolean
  placeholder?: string
}

/**
 * A Select constrained to a known set of values — the primitive for the audit's
 * "missing validation" rows where the backend currently persists free text.
 * If the stored value is outside the allowed set it is surfaced as a flagged,
 * selectable option so the user can see and correct it rather than have it
 * silently disappear.
 */
export function BoundedSelectField({
  value,
  options,
  ...rest
}: BoundedSelectFieldProps) {
  const known = value === '' || options.some((option) => option.value === value)
  const effectiveOptions: FieldOption[] = known
    ? options
    : [{ value, label: `${value} (unsupported)` }, ...options]

  return <SelectField value={value} options={effectiveOptions} {...rest} />
}
