import { useState } from 'react'

import type { ModelBreakdown } from '../../types/tokens'
import { formatTokens } from '../../utils/formatTime'

interface Props {
  items: ModelBreakdown[]
}

export function ModelBreakdownList({ items }: Props) {
  const [expandedFamily, setExpandedFamily] = useState<string | null>(null)

  if (items.length === 0) {
    return null
  }

  return (
    <div className="mt-3 flex flex-col gap-1.5 border-t border-border pt-2.5">
      {items.map((item) => {
        const isExpanded = expandedFamily === item.family
        return (
          <div key={item.family} className="rounded-md bg-background/40 px-2 py-1.5">
            <button
              type="button"
              className="flex w-full items-center gap-3 text-left text-xs"
              onClick={() =>
                setExpandedFamily((current) => (current === item.family ? null : item.family))
              }
            >
              <span className="min-w-0 flex-1 truncate text-muted-foreground">{item.family}</span>
              <span className="tabular-nums text-muted-foreground">
                {Math.round(item.percentage)}%
              </span>
              <span className="tabular-nums font-medium text-foreground">
                {formatTokens(item.totalTokens)}
              </span>
            </button>
            {isExpanded && item.models.length > 0 && (
              <div className="mt-2 flex flex-col gap-1 border-t border-border/60 pt-2">
                {item.models.map((model) => (
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
        )
      })}
    </div>
  )
}
