import type { StageRegistryEntry } from '../../lib/taskNormalization'
import {
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
  onToggle: (status: TaskFilterKey) => void
  onToggleStage: (stage: string) => void
  onClose: () => void
}

export function TasksTabFilters({
  filters,
  stages,
  selectedStages,
  onToggle,
  onToggleStage,
  onClose,
}: FilterDropdownProps) {
  const filterGroups: Array<{ label: string; states: TaskFilterKey[] }> = [
    { label: 'Stage state', states: STAGE_STATE_FILTERS },
    { label: 'Status', states: STATUS_FILTERS },
  ]

  return (
    <>
      <div className="fixed inset-0 z-[99]" onClick={onClose} />
      <div
        className="absolute top-full right-2 z-[100] border border-border rounded-md shadow-xl p-1.5 flex flex-col gap-0.5 min-w-[10rem] max-w-[min(20rem,calc(100vw-2rem))]"
        style={{ background: 'var(--bg-secondary)' }}
      >
        {stages.length > 0 && (
          <div className="flex flex-col gap-0.5 py-0.5">
            <div className="px-2 py-1 text-[length:var(--text-sm)] font-medium uppercase tracking-wide text-muted-foreground/80">
              Stage
            </div>
            {stages.map(stage => {
              const selected = selectedStages.has(stage.name)
              return (
                <label
                  key={stage.name}
                  className={`flex items-center gap-1.5 px-2 py-1 rounded text-[length:var(--text-md)] text-left cursor-pointer hover:bg-muted/50 ${
                    selected ? 'text-foreground bg-muted/50' : 'text-muted-foreground'
                  }`}
                >
                  <input
                    type="checkbox"
                    className="w-3 h-3"
                    checked={selected}
                    onChange={() => onToggleStage(stage.name)}
                  />
                  <span
                    className="w-1.5 h-1.5 rounded-full shrink-0"
                    style={{ backgroundColor: getStageStateColor(stage.state) }}
                    aria-hidden="true"
                  />
                  <span>{stage.display_name}</span>
                </label>
              )
            })}
          </div>
        )}

        {filterGroups.map(group => (
          <div key={group.label} className="flex flex-col gap-0.5 py-0.5">
            <div className="px-2 py-1 text-[length:var(--text-sm)] font-medium uppercase tracking-wide text-muted-foreground/80">
              {group.label}
            </div>
            {group.states.map(status => (
              <label
                key={status}
                className="flex items-center gap-1.5 px-2 py-1 rounded text-[length:var(--text-md)] text-muted-foreground cursor-pointer hover:bg-muted/50"
              >
                <input
                  type="checkbox"
                  className="w-3 h-3"
                  checked={filters.has(status)}
                  onChange={() => onToggle(status)}
                />
                <span
                  className="w-1.5 h-1.5 rounded-full shrink-0"
                  style={{ backgroundColor: getTaskFilterColor(status) }}
                />
                <span>{getTaskFilterLabel(status)}</span>
              </label>
            ))}
          </div>
        ))}
      </div>
    </>
  )
}
