import { useMemo } from 'react'
import type { GobbyTask } from '../../hooks/useTasks'
import { StatusDot, PriorityBadge, TypeBadge } from './TaskBadges'
import { TaskStatusStrip } from './TaskStatusStrip'
import { getCanonicalTaskState, getTaskDisplayState, getTaskStateSummary } from '../../lib/taskState'
import type { StageAdvanceAction } from '../../lib/stageActions'

interface PriorityColumnDef {
  key: 'now' | 'next' | 'later'
  label: string
  color: string
  description: string
}

const COLUMNS: PriorityColumnDef[] = [
  { key: 'now',   label: 'Now',   color: 'var(--color-error)', description: 'Active + Critical/High' },
  { key: 'next',  label: 'Next',  color: 'var(--color-warning-foreground)', description: 'Medium priority, ready' },
  { key: 'later', label: 'Later', color: 'var(--text-muted)', description: 'Low + Backlog' },
]

const WRAPPER_CLS = 'flex flex-1 flex-col overflow-hidden'
const BOARD_CLS = 'flex flex-1 gap-3 overflow-x-auto p-2'
const COLUMN_CLS =
  'flex min-w-60 flex-1 flex-col overflow-hidden rounded-lg border border-[var(--border)] bg-[var(--bg-secondary)]'
const COLUMN_HEADER_CLS = 'flex items-center gap-[0.4rem] px-3 pb-[0.2rem] pt-[0.6rem]'
const COLUMN_DOT_CLS = 'h-2 w-2 shrink-0 rounded-full'
const COLUMN_LABEL_CLS = 'text-[length:calc(var(--font-size-base)*0.9)] font-bold text-[var(--text-primary)]'
const COLUMN_COUNT_CLS =
  'ml-auto min-w-[1.2rem] rounded-full bg-[var(--bg-tertiary)] px-[0.35rem] py-[0.05rem] text-center text-[length:calc(var(--font-size-base)*0.7)] text-[var(--text-muted)]'
const COLUMN_DESC_CLS =
  'border-b border-[var(--border)] px-3 pb-2 text-[length:calc(var(--font-size-base)*0.7)] text-[var(--text-muted)]'
const COLUMN_BODY_CLS = 'flex flex-1 flex-col gap-[0.4rem] overflow-y-auto p-2'
const COLUMN_EMPTY_CLS =
  'px-2 py-6 text-center text-[length:calc(var(--font-size-base)*0.75)] text-[var(--text-muted)]'

const CARD_CLS =
  'group flex w-full cursor-pointer flex-col gap-1 rounded-md border border-[var(--border)] bg-[var(--bg-primary)] px-[0.6rem] py-[0.55rem] text-left font-[inherit] text-[var(--text-primary)] transition-colors duration-150 hover:border-[var(--accent)] hover:bg-[var(--bg-tertiary)]'
const CARD_HEADER_CLS = 'flex items-center gap-[0.3rem]'
const CARD_REF_CLS = 'mr-auto font-[inherit] text-[length:calc(var(--font-size-base)*0.7)] text-[var(--text-muted)]'
const CARD_TITLE_CLS =
  'overflow-hidden text-[length:calc(var(--font-size-base)*0.8)] leading-[1.35] text-[var(--text-primary)] [-webkit-box-orient:vertical] [-webkit-line-clamp:2] [display:-webkit-box]'
const CARD_FOOTER_CLS = 'mt-[0.1rem] flex items-center gap-[0.3rem]'
const CARD_STATUS_CLS = 'ml-auto text-[length:calc(var(--font-size-base)*0.65)] text-[var(--text-muted)]'
const CARD_ACTION_CLS =
  'inline-flex h-[1.2rem] w-[1.2rem] cursor-pointer items-center justify-center rounded-[0.2rem] bg-[var(--bg-tertiary)] text-[length:calc(var(--font-size-base)*0.6)] text-[var(--text-muted)] opacity-0 transition-[opacity,background,color] duration-100 group-hover:opacity-100 hover:bg-[var(--accent)] hover:text-[var(--accent-foreground)]'

const DONE_SUMMARY_CLS =
  'p-2 text-center text-[length:calc(var(--font-size-base)*0.75)] text-[var(--text-muted)]'

function classifyTask(task: GobbyTask): 'now' | 'next' | 'later' | null {
  const displayState = getTaskDisplayState(task)

  if (displayState === 'closed') return null

  if (displayState === 'in_progress' || displayState === 'blocked') return 'now'
  if (task.priority <= 1) return 'now'

  if (displayState === 'needs_review' || displayState === 'review_approved') return 'next'
  if (task.priority === 2) return 'next'

  return 'later'
}

function groupByPriority(tasks: GobbyTask[]): Map<string, GobbyTask[]> {
  const grouped = new Map<string, GobbyTask[]>()
  for (const col of COLUMNS) grouped.set(col.key, [])

  for (const task of tasks) {
    const col = classifyTask(task)
    if (col) grouped.get(col)!.push(task)
  }

  for (const [, list] of grouped) {
    list.sort((a, b) => {
      if (a.priority !== b.priority) return a.priority - b.priority
      return new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime()
    })
  }

  return grouped
}

function PriorityCard({
  task,
  onSelect,
  onAdvanceStage,
}: {
  task: GobbyTask
  onSelect: (id: string) => void
  onAdvanceStage?: (
    taskId: string,
    stageName: string,
    action: StageAdvanceAction,
  ) => void | Promise<void>
}) {
  const current = getCanonicalTaskState(task).current_stage
  const canStart = Boolean(onAdvanceStage && current && getTaskDisplayState(task) === 'ready')

  return (
    <button
      className={CARD_CLS}
      onClick={() => onSelect(task.id)}
    >
      <div className={CARD_HEADER_CLS}>
        <StatusDot task={task} />
        <span className={CARD_REF_CLS}>{task.ref}</span>
        <PriorityBadge priority={task.priority} />
      </div>
      <div className={CARD_TITLE_CLS}>{task.title}</div>
      <div className={CARD_FOOTER_CLS}>
        <TypeBadge type={task.task_type} />
        <span className={CARD_STATUS_CLS}>{getTaskStateSummary(task)}</span>
        {canStart && current && (
          <button
            type="button"
            className={CARD_ACTION_CLS}
            title="Start work"
            onClick={e => { e.stopPropagation(); void onAdvanceStage?.(task.id, current.name, 'start') }}
          >
            ▶
          </button>
        )}
      </div>
      <TaskStatusStrip task={task} compact />
    </button>
  )
}

function PriorityColumn({
  col,
  tasks,
  onSelectTask,
  onAdvanceStage,
}: {
  col: PriorityColumnDef
  tasks: GobbyTask[]
  onSelectTask: (id: string) => void
  onAdvanceStage?: (
    taskId: string,
    stageName: string,
    action: StageAdvanceAction,
  ) => void | Promise<void>
}) {
  return (
    <div className={COLUMN_CLS}>
      <div className={COLUMN_HEADER_CLS}>
        <span className={COLUMN_DOT_CLS} style={{ background: col.color }} />
        <span className={COLUMN_LABEL_CLS}>{col.label}</span>
        <span className={COLUMN_COUNT_CLS}>{tasks.length}</span>
      </div>
      <div className={COLUMN_DESC_CLS}>{col.description}</div>
      <div className={COLUMN_BODY_CLS}>
        {tasks.length === 0 ? (
          <div className={COLUMN_EMPTY_CLS}>No tasks</div>
        ) : (
          tasks.map(task => (
            <PriorityCard
              key={task.id}
              task={task}
              onSelect={onSelectTask}
              onAdvanceStage={onAdvanceStage}
            />
          ))
        )}
      </div>
    </div>
  )
}

interface PriorityBoardProps {
  tasks: GobbyTask[]
  onSelectTask: (id: string) => void
  onAdvanceStage?: (
    taskId: string,
    stageName: string,
    action: StageAdvanceAction,
  ) => void | Promise<void>
}

export function PriorityBoard({ tasks, onSelectTask, onAdvanceStage }: PriorityBoardProps) {
  const grouped = useMemo(() => groupByPriority(tasks), [tasks])
  const doneCount = useMemo(
    () => tasks.filter(t => getTaskDisplayState(t) === 'closed').length,
    [tasks]
  )

  return (
    <div className={WRAPPER_CLS}>
      <div className={BOARD_CLS}>
        {COLUMNS.map(col => (
          <PriorityColumn
            key={col.key}
            col={col}
            tasks={grouped.get(col.key) || []}
            onSelectTask={onSelectTask}
            onAdvanceStage={onAdvanceStage}
          />
        ))}
      </div>
      {doneCount > 0 && (
        <div className={DONE_SUMMARY_CLS}>
          {doneCount} completed task{doneCount !== 1 ? 's' : ''} hidden
        </div>
      )}
    </div>
  )
}
