import { cn } from '../../lib/utils'

interface SwitchProps {
  checked: boolean
  onChange: (checked: boolean) => void
  disabled?: boolean
  'aria-label': string
}

export function Switch({
  checked,
  onChange,
  disabled = false,
  'aria-label': ariaLabel,
}: SwitchProps) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={ariaLabel}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={cn(
        'inline-flex h-7 w-12 shrink-0 items-center rounded-full border transition-colors duration-150',
        checked
          ? 'border-transparent bg-accent'
          : 'border-border bg-muted',
        disabled
          ? 'cursor-not-allowed opacity-50'
          : 'cursor-pointer',
      )}
    >
      <span
        className={cn(
          'mx-0.5 block h-5 w-5 rounded-full bg-background shadow-sm transition-transform duration-150',
          checked ? 'translate-x-5' : 'translate-x-0',
        )}
      />
    </button>
  )
}
