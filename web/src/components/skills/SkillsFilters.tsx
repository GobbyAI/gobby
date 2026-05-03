import type { SkillStats } from '../../hooks/useSkills'
import { cn } from '../../lib/utils'

const FILTERS_CLS = 'flex flex-wrap items-center justify-between gap-2 py-2'
const CHIPS_CLS = 'flex flex-wrap gap-1.5'
const SELECTS_CLS = 'flex items-center gap-1.5'

const CHIP_CLS =
  'inline-flex cursor-pointer items-center gap-1 rounded-full border border-[var(--border)] bg-[var(--bg-secondary)] px-2.5 py-1 text-[length:var(--text-sm)] text-[var(--text-secondary)] transition-[background-color,color,border-color] duration-150 hover:border-[var(--text-muted)] pointer-coarse:min-h-11'
const CHIP_ACTIVE_CLS =
  'border-[var(--accent)] bg-[color-mix(in_srgb,var(--accent)_10%,transparent)] text-[var(--accent)]'
const CHIP_COUNT_CLS =
  'text-[length:var(--text-2xs)] text-[var(--text-muted)] [font-variant-numeric:tabular-nums]'

const SELECT_CLS =
  'rounded-md border border-[var(--border)] bg-[var(--bg-secondary)] px-2 py-1 font-[inherit] text-[length:var(--text-sm)] text-[var(--text-primary)] pointer-coarse:min-h-11'
const CLEAR_BTN_CLS =
  'cursor-pointer border-0 bg-transparent px-2 py-1 text-[length:var(--text-sm)] text-[var(--accent)] hover:underline pointer-coarse:min-h-11'

interface SkillsFiltersProps {
  stats: SkillStats | null
  category: string | null
  sourceType: string | null
  onCategoryChange: (cat: string | null) => void
  onSourceTypeChange: (st: string | null) => void
  onClear: () => void
}

export function SkillsFilters({ stats, category, sourceType, onCategoryChange, onSourceTypeChange, onClear }: SkillsFiltersProps) {
  const categories = stats?.by_category ? Object.keys(stats.by_category).sort() : []
  const sourceTypes = stats?.by_source_type ? Object.keys(stats.by_source_type).sort() : []
  const hasFilters = category !== null || sourceType !== null

  return (
    <div className={FILTERS_CLS}>
      <div className={CHIPS_CLS}>
        {categories.map(cat => (
          <button
            key={cat}
            className={cn(CHIP_CLS, category === cat && CHIP_ACTIVE_CLS)}
            onClick={() => onCategoryChange(category === cat ? null : cat)}
          >
            {cat}
            {stats?.by_category[cat] != null && (
              <span className={CHIP_COUNT_CLS}>{stats.by_category[cat]}</span>
            )}
          </button>
        ))}
      </div>

      <div className={SELECTS_CLS}>
        <select
          className={SELECT_CLS}
          value={sourceType || ''}
          onChange={e => onSourceTypeChange(e.target.value || null)}
        >
          <option value="">All Sources</option>
          {sourceTypes.map(st => (
            <option key={st} value={st}>{st}</option>
          ))}
        </select>

        {hasFilters && (
          <button className={CLEAR_BTN_CLS} onClick={onClear}>
            Clear filters
          </button>
        )}
      </div>
    </div>
  )
}
