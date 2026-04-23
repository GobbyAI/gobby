import { formatTokens } from '../../utils/formatTime'

interface BreakdownEntry {
  family: string
  totalTokens: number
  inputTokens: number
  outputTokens: number
  models: Array<{ model: string; totalTokens: number }>
}

interface Props {
  breakdown: BreakdownEntry[]
}

export function SessionModelBreakdown({ breakdown }: Props) {
  if (breakdown.length === 0) {
    return null
  }

  return (
    <div className="rounded-md border border-border bg-background/40 p-2">
      <div className="mb-2 text-[11px] font-medium uppercase tracking-[0.04em] text-muted-foreground">
        Session Models
      </div>
      <div className="flex flex-col gap-1.5">
        {breakdown.map((entry) => (
          <div key={entry.family} className="rounded bg-background/70 px-2 py-1.5">
            <div className="flex items-center gap-3 text-xs">
              <span className="min-w-0 flex-1 truncate text-muted-foreground">{entry.family}</span>
              <span className="tabular-nums font-medium text-foreground">
                {formatTokens(entry.totalTokens)}
              </span>
            </div>
            <div className="mt-1 text-[11px] text-muted-foreground">
              {formatTokens(entry.inputTokens)} in · {formatTokens(entry.outputTokens)} out
            </div>
            {entry.models.length > 0 && (
              <div className="mt-2 flex flex-col gap-1 border-t border-border/60 pt-2">
                {entry.models.map((model) => (
                  <div key={model.model} className="flex items-center gap-3 text-[11px]">
                    <span className="min-w-0 flex-1 truncate text-muted-foreground">
                      {model.model}
                    </span>
                    <span className="tabular-nums text-foreground">
                      {formatTokens(model.totalTokens)}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}
