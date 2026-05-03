import type { MemoryFilters as MemoryFiltersType, MemoryStats } from '../../hooks/useMemory'
import { cn } from '../../lib/utils'

interface MemoryFiltersProps {
  filters: MemoryFiltersType
  stats: MemoryStats | null
  recentCount: number
  onFiltersChange: (filters: MemoryFiltersType) => void
  viewMode?: string
  knowledgeGraphLimit?: number
  onKnowledgeGraphLimitChange?: (limit: number) => void
  limitMin?: number
  limitMax?: number
  limitStep?: number
}

const MEMORY_TYPES = [
  { key: 'fact', label: 'Fact', color: 'var(--accent)' },
  { key: 'preference', label: 'Preference', color: 'var(--color-agent)' },
  { key: 'pattern', label: 'Pattern', color: 'var(--color-review)' },
  { key: 'context', label: 'Context', color: 'var(--color-warning-foreground)' },
] as const

const CHIP_BASE_CLS =
  'inline-flex cursor-pointer items-center gap-1.5 whitespace-nowrap rounded-full border border-[var(--border)] bg-[var(--bg-secondary)] px-2.5 py-1 text-[length:var(--text-md)] text-[var(--text-secondary)] transition-all duration-150 hover:border-[var(--text-muted)] pointer-coarse:min-h-11 pointer-coarse:py-2'
const CHIP_ACTIVE_CLS =
  'border-[var(--accent)] bg-[color-mix(in_srgb,var(--accent)_8%,transparent)] text-[var(--text-primary)]'

export function MemoryFilters({
  filters, stats, recentCount, onFiltersChange,
  viewMode, knowledgeGraphLimit, onKnowledgeGraphLimitChange,
  limitMin = 50, limitMax = 5000, limitStep = 50,
}: MemoryFiltersProps) {
  return (
    <div className="flex flex-wrap items-center gap-2 pb-2.5">
      <div className="flex flex-wrap items-center gap-1.5">
        {MEMORY_TYPES.map(t => {
          const count = stats?.by_type?.[t.key] ?? 0
          const isActive = filters.memoryType === t.key
          return (
            <button
              key={t.key}
              type="button"
              className={cn(CHIP_BASE_CLS, isActive && CHIP_ACTIVE_CLS)}
              onClick={() =>
                onFiltersChange({
                  ...filters,
                  memoryType: isActive ? null : t.key,
                  recentOnly: false,
                })
              }
            >
              <span
                className="h-2 w-2 shrink-0 rounded-full"
                style={{ backgroundColor: t.color }}
              />
              {t.label}
              <span className="text-[length:var(--text-xs)] tabular-nums text-[var(--text-muted)]">{count}</span>
            </button>
          )
        })}
        <button
          type="button"
          className={cn(CHIP_BASE_CLS, filters.recentOnly && CHIP_ACTIVE_CLS)}
          onClick={() =>
            onFiltersChange({
              ...filters,
              recentOnly: !filters.recentOnly,
              memoryType: null,
            })
          }
        >
          <span
            className="h-2 w-2 shrink-0 rounded-full"
            style={{ backgroundColor: 'var(--color-success-foreground)' }}
          />
          24H
          <span className="text-[length:var(--text-xs)] tabular-nums text-[var(--text-muted)]">{recentCount}</span>
        </button>
        {viewMode === 'knowledge' && onKnowledgeGraphLimitChange && (
          <label
            className="ml-auto flex items-center gap-1 whitespace-nowrap text-[length:var(--text-sm)] text-[var(--text-secondary)]"
            htmlFor="knowledge-graph-limit"
            title="Max nodes to display"
          >
            Limit
            <input
              id="knowledge-graph-limit"
              type="number"
              min={limitMin}
              max={limitMax}
              step={limitStep}
              value={knowledgeGraphLimit}
              onChange={e => {
                const v = Math.max(limitMin, Math.min(limitMax, Number(e.target.value) || limitMin))
                onKnowledgeGraphLimitChange(v)
              }}
              className="w-16 rounded border border-[var(--border)] bg-[var(--bg-secondary)] px-1.5 py-1 text-right text-[length:var(--text-sm)] text-[var(--text-primary)] focus:border-[var(--accent)] focus:outline-none pointer-coarse:min-h-11"
            />
          </label>
        )}
      </div>

      {(filters.memoryType !== null || filters.recentOnly) && (
        <button
          type="button"
          className="cursor-pointer rounded-full border border-[var(--border)] bg-transparent px-2.5 py-1 text-[length:var(--text-sm)] text-[var(--text-muted)] transition-all duration-150 hover:border-[var(--color-error)] hover:text-[var(--color-error)] pointer-coarse:min-h-11 pointer-coarse:py-2"
          onClick={() =>
            onFiltersChange({ ...filters, memoryType: null, recentOnly: false })
          }
        >
          Clear filters
        </button>
      )}
    </div>
  )
}
