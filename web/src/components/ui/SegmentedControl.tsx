import { useRef, type KeyboardEvent, type ReactNode } from 'react'
import { cn } from '../../lib/utils'
import { useResolvedTheme } from '../../hooks/useResolvedTheme'

interface BaseSegmentedControlOption<T extends string> {
  value: T
  title?: string
  onClick?: () => void
}

type TextSegmentedControlOption<T extends string> = BaseSegmentedControlOption<T> & {
  label: string
  ariaLabel?: string
}

type NonTextSegmentedControlOption<T extends string> = BaseSegmentedControlOption<T> & {
  label: Exclude<ReactNode, string>
  ariaLabel: string
}

export type SegmentedControlOption<T extends string> =
  | TextSegmentedControlOption<T>
  | NonTextSegmentedControlOption<T>

interface SegmentedControlProps<T extends string> {
  value: T
  onChange: (value: T) => void
  options: readonly SegmentedControlOption<T>[]
  ariaLabel: string
  size?: 'sm' | 'md'
  controlHeight?: 'sm' | 'md'
  disabled?: boolean
  className?: string
}

export function SegmentedControl<T extends string>({
  value,
  onChange,
  options,
  ariaLabel,
  size = 'sm',
  controlHeight = 'md',
  disabled = false,
  className,
}: SegmentedControlProps<T>) {
  const buttonRefs = useRef<(HTMLButtonElement | null)[]>([])

  function selectIndex(index: number) {
    const next = options[index]
    if (!next) return
    onChange(next.value)
    next.onClick?.()
    buttonRefs.current[index]?.focus()
  }

  function handleKeyDown(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    if (disabled) return
    let nextIndex = -1
    if (event.key === 'ArrowRight') nextIndex = (index + 1) % options.length
    else if (event.key === 'ArrowLeft') nextIndex = (index - 1 + options.length) % options.length
    else if (event.key === 'Home') nextIndex = 0
    else if (event.key === 'End') nextIndex = options.length - 1
    if (nextIndex < 0) return
    event.preventDefault()
    selectIndex(nextIndex)
  }

  const sizeText = size === 'md' ? 'text-base' : 'text-xs'
  const sizePad = size === 'md' ? 'segmented-control__option--md' : 'segmented-control__option--sm'
  const heightVar = controlHeight === 'sm' ? 'var(--control-row-height-sm)' : 'var(--control-row-height)'

  // Light: recessed bg-secondary track + a brighter neutral --surface-selected
  // well for the active segment (selection by lightness + weight, hue stays
  // the fourth signal — .impeccable.md). Dark is byte-frozen to the shipped
  // accent treatment (IMG_3754); CSS can't reach Tailwind classes so this
  // branches on the same useResolvedTheme() hook the chrome uses.
  const isLight = useResolvedTheme() === 'light'
  const trackBg = isLight ? 'bg-[var(--bg-secondary)]' : 'bg-[var(--bg-primary)]'
  const activeOption = isLight
    ? 'bg-[var(--surface-selected)] text-[var(--text-primary)] font-semibold'
    : 'bg-accent/15 text-accent font-semibold'

  return (
    <div
      role="radiogroup"
      aria-label={ariaLabel}
      aria-disabled={disabled || undefined}
      style={{ height: heightVar }}
      className={cn(
        'inline-flex items-stretch rounded-md border border-border',
        trackBg,
        sizeText,
        className,
      )}
    >
      {options.map((option, index) => {
        const isActive = option.value === value
        const explicitAriaLabel = option.ariaLabel?.trim()
        const optionAriaLabel =
          explicitAriaLabel || (typeof option.label === 'string' ? option.label : undefined)
        return (
          <button
            key={option.value}
            ref={(node) => {
              buttonRefs.current[index] = node
            }}
            type="button"
            role="radio"
            aria-checked={isActive}
            tabIndex={isActive ? 0 : -1}
            disabled={disabled}
            aria-label={optionAriaLabel}
            title={option.title}
            onClick={() => {
              if (disabled) return
              onChange(option.value)
              option.onClick?.()
            }}
            onKeyDown={(event) => handleKeyDown(event, index)}
            className={cn(
              'segmented-control__option',
              'inline-flex items-center justify-center',
              sizePad,
              'transition-colors motion-reduce:transition-none',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-background',
              index === 0 && 'rounded-l-md',
              index === options.length - 1 && 'rounded-r-md',
              isActive
                ? activeOption
                : 'text-muted-foreground hover:bg-muted hover:text-foreground',
              disabled && 'cursor-not-allowed opacity-50',
            )}
          >
            {option.label}
          </button>
        )
      })}
    </div>
  )
}
