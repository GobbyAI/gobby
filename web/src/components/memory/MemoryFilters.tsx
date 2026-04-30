import type { MemoryFilters as MemoryFiltersType, MemoryStats } from '../../hooks/useMemory'

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

export function MemoryFilters({
  filters, stats, recentCount, onFiltersChange,
  viewMode, knowledgeGraphLimit, onKnowledgeGraphLimitChange,
  limitMin = 50, limitMax = 5000, limitStep = 50,
}: MemoryFiltersProps) {
  return (
    <div className="memory-filter-bar">
      <div className="memory-filter-chips">
        {MEMORY_TYPES.map(t => {
          const count = stats?.by_type?.[t.key] ?? 0
          const isActive = filters.memoryType === t.key
          return (
            <button
              key={t.key}
              className={`memory-type-chip ${isActive ? 'active' : ''}`}
              onClick={() =>
                onFiltersChange({
                  ...filters,
                  memoryType: isActive ? null : t.key,
                  recentOnly: false,
                })
              }
            >
              <span className="memory-type-dot" style={{ backgroundColor: t.color }} />
              {t.label}
              <span className="memory-type-chip-count">{count}</span>
            </button>
          )
        })}
        <button
          className={`memory-type-chip ${filters.recentOnly ? 'active' : ''}`}
          onClick={() =>
            onFiltersChange({
              ...filters,
              recentOnly: !filters.recentOnly,
              memoryType: null,
            })
          }
        >
          <span className="memory-type-dot" style={{ backgroundColor: 'var(--color-success-foreground)' }} />
          24H
          <span className="memory-type-chip-count">{recentCount}</span>
        </button>
        {viewMode === 'knowledge' && onKnowledgeGraphLimitChange && (
          <label className="memory-limit-control" htmlFor="knowledge-graph-limit" title="Max nodes to display" style={{ marginLeft: 'auto' }}>
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
            />
          </label>
        )}
      </div>

      {(filters.memoryType !== null || filters.recentOnly) && (
        <button
          className="memory-filter-clear"
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
