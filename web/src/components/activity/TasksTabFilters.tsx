import { useCallback, useEffect, useState } from 'react'
import type { StageRegistryEntry } from '../../lib/taskNormalization'
import {
  DEFAULT_FILTERS,
  getStageStateColor,
  getTaskFilterColor,
  getTaskFilterLabel,
  STAGE_STATE_FILTERS,
  STATUS_FILTERS,
  type TaskFilterKey,
} from './TasksTabModel'

interface FilterDropdownProps {
  filters: Set<TaskFilterKey>
  stages: StageRegistryEntry[]
  selectedStages: ReadonlySet<string>
  onApply: (filters: Set<TaskFilterKey>, stages: Set<string>) => void
  onClose: () => void
}

export function TasksTabFilters({
  filters,
  stages,
  selectedStages,
  onApply,
  onClose,
}: FilterDropdownProps) {
  // Draft state mirrors the SessionsFilterDropdown pattern: changes don't
  // touch the parent until Apply, so the user can stage edits and back out
  // via Escape or overlay click without polluting the visible task list.
  const [draftFilters, setDraftFilters] = useState<Set<TaskFilterKey>>(() => new Set(filters))
  const [draftStages, setDraftStages] = useState<Set<string>>(() => new Set(selectedStages))

  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [onClose])

  const toggleDraftFilter = useCallback((key: TaskFilterKey) => {
    setDraftFilters((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }, [])

  const toggleDraftStage = useCallback((name: string) => {
    setDraftStages((prev) => {
      const next = new Set(prev)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })
  }, [])

  const handleApply = useCallback(() => {
    onApply(draftFilters, draftStages)
    onClose()
  }, [onApply, onClose, draftFilters, draftStages])

  const handleReset = useCallback(() => {
    setDraftFilters(new Set(DEFAULT_FILTERS))
    setDraftStages(new Set())
  }, [])

  return (
    <>
      <div className="fixed inset-0 z-[99]" onClick={onClose} />
      <div
        className="absolute top-full right-2 z-[100] border border-border rounded-md shadow-xl flex flex-col w-[min(30rem,calc(100vw-1.5rem))]"
        style={{ background: 'var(--bg-secondary)' }}
        role="dialog"
        aria-label="Task filters"
      >
        <div className="grid grid-cols-2 divide-x divide-border">
          {/* Left column: Stage list (single column, all stages) */}
          <div className="flex flex-col gap-0.5 p-1.5 min-w-0">
            <Section label="Stage">
              {stages.length === 0 ? (
                <EmptyHint>No stages available</EmptyHint>
              ) : (
                <div className="flex flex-col gap-0.5 px-2 py-1">
                  {stages.map((stage) => {
                    const selected = draftStages.has(stage.name)
                    return (
                      <label
                        key={stage.name}
                        className={`flex min-w-0 items-center gap-1.5 px-2 py-1 rounded text-[length:var(--text-md)] text-left cursor-pointer hover:bg-muted/50 ${
                          selected ? 'text-foreground bg-muted/50' : 'text-muted-foreground'
                        }`}
                      >
                        <input
                          type="checkbox"
                          className="w-3 h-3"
                          checked={selected}
                          onChange={() => toggleDraftStage(stage.name)}
                        />
                        <span
                          className="w-1.5 h-1.5 rounded-full shrink-0"
                          style={{ backgroundColor: getStageStateColor(stage.state) }}
                          aria-hidden="true"
                        />
                        <span className="truncate">{stage.display_name}</span>
                      </label>
                    )
                  })}
                </div>
              )}
            </Section>
          </div>

          {/* Right column: Stage state stacked above Status */}
          <div className="flex flex-col gap-0.5 p-1.5 min-w-0">
            <Section label="Stage state">
              <FilterCheckboxList
                states={STAGE_STATE_FILTERS}
                draftFilters={draftFilters}
                onToggle={toggleDraftFilter}
              />
            </Section>
            <Section label="Status">
              <FilterCheckboxList
                states={STATUS_FILTERS}
                draftFilters={draftFilters}
                onToggle={toggleDraftFilter}
              />
            </Section>
          </div>
        </div>

        <div
          className="flex items-center justify-between border-t border-border px-2 py-1.5"
          style={{ background: 'var(--bg-secondary)' }}
        >
          <button type="button" className="btn btn-ghost btn-sm" onClick={handleReset}>
            Reset
          </button>
          <button type="button" className="btn btn-accent btn-sm" onClick={handleApply}>
            Apply
          </button>
        </div>
      </div>
    </>
  )
}

function Section({
  label,
  children,
}: {
  label: string
  children: React.ReactNode
}) {
  return (
    <div className="flex flex-col gap-0.5 py-0.5">
      <div className="px-2 py-1 text-[length:var(--text-sm)] font-medium uppercase tracking-wide text-muted-foreground/80">
        {label}
      </div>
      {children}
    </div>
  )
}

function EmptyHint({ children }: { children: React.ReactNode }) {
  return (
    <div className="px-2 py-1 text-[length:var(--text-md)] text-muted-foreground">{children}</div>
  )
}

function FilterCheckboxList({
  states,
  draftFilters,
  onToggle,
}: {
  states: TaskFilterKey[]
  draftFilters: Set<TaskFilterKey>
  onToggle: (key: TaskFilterKey) => void
}) {
  return (
    <div className="flex flex-col gap-0.5 px-2 py-1">
      {states.map((status) => (
        <label
          key={status}
          className="flex min-w-0 items-center gap-1.5 px-2 py-1 rounded text-[length:var(--text-md)] text-muted-foreground cursor-pointer hover:bg-muted/50"
        >
          <input
            type="checkbox"
            className="w-3 h-3"
            checked={draftFilters.has(status)}
            onChange={() => onToggle(status)}
          />
          <span
            className="w-1.5 h-1.5 rounded-full shrink-0"
            style={{ backgroundColor: getTaskFilterColor(status) }}
            aria-hidden="true"
          />
          <span className="truncate">{getTaskFilterLabel(status)}</span>
        </label>
      ))}
    </div>
  )
}
