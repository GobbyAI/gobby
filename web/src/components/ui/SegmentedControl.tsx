import { useRef, type KeyboardEvent } from 'react'
import { cn } from '../../lib/utils'

export interface SegmentedControlOption<T extends string> {
  value: T
  label: string
  title?: string
  onClick?: () => void
}

interface SegmentedControlProps<T extends string> {
  value: T
  onChange: (value: T) => void
  options: readonly SegmentedControlOption<T>[]
  ariaLabel: string
  size?: 'sm' | 'md'
  disabled?: boolean
  className?: string
}

export function SegmentedControl<T extends string>({
  value,
  onChange,
  options,
  ariaLabel,
  size = 'sm',
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

  const sizeText = size === 'md' ? 'text-sm' : 'text-xs'
  const sizePad = size === 'md' ? 'px-2.5 py-1.5' : 'px-2 py-1'

  return (
    <div
      role="radiogroup"
      aria-label={ariaLabel}
      aria-disabled={disabled || undefined}
      className={cn('inline-flex rounded-md border border-border', sizeText, className)}
    >
      {options.map((option, index) => {
        const isActive = option.value === value
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
            title={option.title}
            onClick={() => {
              if (disabled) return
              onChange(option.value)
              option.onClick?.()
            }}
            onKeyDown={(event) => handleKeyDown(event, index)}
            className={cn(
              sizePad,
              'transition-colors',
              index === 0 && 'rounded-l-md',
              index === options.length - 1 && 'rounded-r-md',
              isActive
                ? 'bg-accent/15 text-accent'
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
