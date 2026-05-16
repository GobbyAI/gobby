import { useCallback, useEffect, useState } from 'react'
import { ActivityFilterFooter } from './ActivityFilterFooter'
import { FilterCheckboxRow, FilterSection } from './FilterPrimitives'
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
    setDraftStages(new Set(stages.map(stage => stage.name)))
  }, [stages])

  return (
    <>
      <div className="fixed inset-0 z-[99]" onClick={onClose} />
      <div
        className="absolute top-full right-2 z-[100] border border-border rounded-md shadow-xl flex flex-col w-[min(24rem,calc(100vw-1.5rem))]"
        style={{ background: 'var(--bg-secondary)' }}
        role="dialog"
        aria-label="Task filters"
      >
        <div className="grid grid-cols-2 divide-x divide-border">
          {/* Left column: Stage list (single column, all stages) */}
          <div className="flex flex-col gap-0.5 p-1.5 min-w-0">
            <FilterSection label="Stage">
              {stages.length === 0 ? (
                <EmptyHint>No stages available</EmptyHint>
              ) : (
                <div className="flex flex-col gap-0.5 px-2 py-1">
                  {stages.map((stage) => (
                    <FilterCheckboxRow
                      key={stage.name}
                      label={stage.display_name}
                      checked={draftStages.has(stage.name)}
                      onToggle={() => toggleDraftStage(stage.name)}
                      leading={
                        <span
                          className="w-1.5 h-1.5 rounded-full shrink-0"
                          style={{ backgroundColor: getStageStateColor(stage.state) }}
                          aria-hidden="true"
                        />
                      }
                    />
                  ))}
                </div>
              )}
            </FilterSection>
          </div>

          {/* Right column: Stage state stacked above Status */}
          <div className="flex flex-col gap-0.5 p-1.5 min-w-0">
            <FilterSection label="Stage state">
              <FilterCheckboxList
                states={STAGE_STATE_FILTERS}
                draftFilters={draftFilters}
                onToggle={toggleDraftFilter}
              />
            </FilterSection>
            <FilterSection label="Status">
              <FilterCheckboxList
                states={STATUS_FILTERS}
                draftFilters={draftFilters}
                onToggle={toggleDraftFilter}
              />
            </FilterSection>
          </div>
        </div>

        <ActivityFilterFooter onReset={handleReset} onApply={handleApply} />
      </div>
    </>
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
        <FilterCheckboxRow
          key={status}
          label={getTaskFilterLabel(status)}
          checked={draftFilters.has(status)}
          onToggle={() => onToggle(status)}
          leading={
            <span
              className="w-1.5 h-1.5 rounded-full shrink-0"
              style={{ backgroundColor: getTaskFilterColor(status) }}
              aria-hidden="true"
            />
          }
        />
      ))}
    </div>
  )
}
