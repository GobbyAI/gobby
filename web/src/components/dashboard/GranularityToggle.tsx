import { cn } from '../../lib/utils'
import type { TimeSeriesGranularity } from '../../types/tokens'

const OPTIONS: TimeSeriesGranularity[] = ['30m', '1h', '1d']

interface Props {
  value: TimeSeriesGranularity
  onChange: (value: TimeSeriesGranularity) => void
}

export function GranularityToggle({ value, onChange }: Props) {
  return (
    <div
      role="group"
      aria-label="Token chart granularity"
      className="inline-flex rounded-md border border-border bg-background/60 p-0.5"
    >
      {OPTIONS.map((option) => (
        <button
          key={option}
          type="button"
          aria-pressed={option === value}
          className={cn(
            'rounded px-2 py-1 text-[11px] font-medium transition-colors',
            option === value
              ? 'bg-foreground text-background'
              : 'text-muted-foreground hover:text-foreground',
          )}
          onClick={() => onChange(option)}
        >
          {option}
        </button>
      ))}
    </div>
  )
}
