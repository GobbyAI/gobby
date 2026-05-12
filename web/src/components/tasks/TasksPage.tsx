import { useState, useMemo, useCallback } from 'react'
import { useTasks } from '../../hooks/useTasks'
import { useStagesRegistry } from '../../hooks/useStagesRegistry'
import type { GobbyTask } from '../../hooks/useTasks'
import { StatusDot, PriorityBadge, TypeBadge, TaskStateBadges } from './TaskBadges'
import { TaskDetail } from './TaskDetail'
import { TaskCreateForm } from './TaskCreateForm'
import type { TaskCreateDefaults } from './TaskCreateForm'
import { LifecycleBoard } from './LifecycleBoard'
import { TaskTree } from './TaskTree'
import { PriorityBoard } from './PriorityBoard'
import { AuditLog } from './AuditLog'
import { GanttChart } from './GanttChart'
import { DigestView } from './DigestView'
import { DependencyGraph } from './DependencyGraph'
import { TaskSelectionToolbar } from './TaskSelectionToolbar'
import { cn } from '../../lib/utils'
import {
  getCanonicalTaskState,
  getTaskDisplayState,
  TASK_STATE_LABELS,
  TASK_STATE_ORDER,
  type TaskDisplayState,
} from '../../lib/taskState'

// =============================================================================
// Tailwind class constants
// =============================================================================

const PAGE_CLS = 'flex flex-1 flex-col overflow-hidden px-6 py-4 max-md:max-w-screen max-md:overflow-x-hidden max-md:px-3'
const TOOLBAR_CLS = 'flex flex-wrap items-center justify-between gap-4 border-b border-[var(--border)] pb-3 mb-2 max-md:flex-col max-md:items-start max-md:gap-2'
const TOOLBAR_LEFT_CLS = 'flex flex-wrap items-center gap-2 max-md:w-full'
const TOOLBAR_RIGHT_CLS = 'flex items-center gap-2 max-md:w-full max-md:flex-wrap'
const TITLE_CLS = 'mr-1 text-[length:calc(var(--font-size-base)*1.1)] font-semibold'
const GROUP_TABS_CLS = 'flex items-center gap-px ml-2 rounded-md bg-[var(--bg-secondary)] p-0.5'
const GROUP_TAB_CLS = 'cursor-pointer whitespace-nowrap rounded border-0 bg-transparent px-2.5 py-[3px] text-[length:calc(var(--font-size-base)*0.7)] font-medium text-[var(--text-muted)] transition-colors duration-150 hover:text-[var(--text-primary)] pointer-coarse:min-h-11'
const GROUP_TAB_ACTIVE_CLS = 'bg-[var(--bg-primary)] text-[var(--text-primary)] shadow-[var(--shadow-sm)]'

const VIEW_TOGGLE_CLS = 'flex overflow-hidden rounded-md border border-[var(--border)]'
const VIEW_BTN_CLS = 'flex h-8 w-8 cursor-pointer items-center justify-center border-0 border-r border-[var(--border)] bg-transparent text-[var(--text-muted)] transition-colors duration-150 last:border-r-0 hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-secondary)] pointer-coarse:h-11 pointer-coarse:w-11'
const VIEW_BTN_ACTIVE_CLS = 'bg-[var(--bg-tertiary)] text-[var(--accent)]'

const SEARCH_CLS = 'w-[180px] rounded-md border border-[var(--border)] bg-[var(--bg-tertiary)] px-2.5 py-1.5 font-[inherit] text-[length:calc(var(--font-size-base)*0.8)] text-[var(--text-primary)] placeholder:text-[var(--text-muted)] focus:border-[var(--accent)] focus:outline-none max-md:flex-1 max-md:min-w-0 pointer-coarse:min-h-11'
const REFRESH_BTN_CLS = 'flex h-8 w-8 cursor-pointer items-center justify-center rounded-md border border-[var(--border)] bg-transparent text-[length:calc(var(--font-size-base)*1.1)] text-[var(--text-secondary)] transition-colors duration-150 hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)] pointer-coarse:h-11 pointer-coarse:w-11'
const NEW_BTN_CLS = 'flex cursor-pointer items-center gap-1.5 whitespace-nowrap rounded-md border-0 bg-[var(--accent)] px-3 py-1.5 font-[inherit] text-[length:calc(var(--font-size-base)*0.8)] font-medium text-[var(--accent-foreground)] transition-colors duration-150 hover:bg-[var(--accent-hover)] pointer-coarse:min-h-11'

const FILTER_BAR_CLS = 'flex flex-wrap items-center justify-between gap-3 border-b border-[var(--border)] py-2 mb-2 max-md:flex-col max-md:items-start'
const FILTER_CHIPS_CLS = 'flex flex-wrap items-center gap-1.5 max-md:flex-nowrap max-md:max-w-full max-md:overflow-x-auto max-md:pb-1'
const FILTER_DROPDOWNS_CLS = 'flex flex-wrap items-center gap-1.5 max-md:w-full'
const STAT_CHIP_CLS = 'inline-flex cursor-pointer items-center gap-1.5 rounded-full border border-[var(--border)] bg-transparent px-2.5 py-0.5 font-[inherit] text-[length:calc(var(--font-size-base)*0.75)] text-[var(--text-secondary)] transition-colors duration-150 hover:bg-[var(--bg-tertiary)] pointer-coarse:min-h-11'
const STAT_CHIP_ACTIVE_CLS = 'bg-[var(--bg-tertiary)] border-[var(--accent)] text-[var(--text-primary)]'
const FILTER_SELECT_CLS = 'cursor-pointer rounded-md border border-[var(--border)] bg-[var(--bg-tertiary)] px-2 py-1 font-[inherit] text-[length:calc(var(--font-size-base)*0.75)] text-[var(--text-primary)] focus:border-[var(--accent)] focus:outline-none max-md:flex-1 max-md:min-w-0 pointer-coarse:min-h-11'
const FILTER_CLEAR_CLS = 'cursor-pointer rounded-md border border-[var(--border)] bg-transparent px-2 py-1 font-[inherit] text-[length:calc(var(--font-size-base)*0.7)] text-[var(--text-muted)] transition-colors duration-150 hover:border-[var(--text-muted)] hover:text-[var(--text-primary)] pointer-coarse:min-h-11'

const STATE_CONTENT_LOADING_CLS = 'flex flex-1 items-center justify-center text-[length:calc(var(--font-size-base)*0.9)] text-[var(--text-muted)]'
const TABLE_CONTAINER_CLS = 'flex-1 overflow-y-auto max-md:overflow-x-auto max-sm:overflow-x-visible'
const TABLE_CLS = 'w-full border-collapse text-[length:calc(var(--font-size-base)*0.85)] max-sm:[&_thead]:hidden max-sm:[&_tr]:relative max-sm:[&_tr]:mb-2 max-sm:[&_tr]:block max-sm:[&_tr]:rounded-md max-sm:[&_tr]:border max-sm:[&_tr]:border-[var(--border)] max-sm:[&_tr]:bg-[var(--bg-secondary)] max-sm:[&_tr]:px-2.5 max-sm:[&_tr]:py-2 max-sm:[&_tr]:pl-7 max-sm:[&_td]:block max-sm:[&_td]:w-full max-sm:[&_td]:border-b-0 max-sm:[&_td]:px-0 max-sm:[&_td]:py-1 max-sm:[&_td]:whitespace-normal'
const TH_CLS = 'sticky top-0 z-[1] border-b border-[var(--border)] bg-[var(--bg-primary)] px-2.5 py-2 text-left text-[length:calc(var(--font-size-base)*0.7)] font-medium uppercase tracking-[0.05em] text-[var(--text-muted)]'
const TH_SORTABLE_CLS = 'cursor-pointer select-none whitespace-nowrap hover:text-[var(--text-primary)]'
const ROW_CLS = 'cursor-pointer transition-colors duration-100 hover:bg-[var(--bg-tertiary)]'
const CELL_CLS = 'whitespace-nowrap border-b border-[var(--border)] px-2.5 py-1.5'
const CELL_TITLE_CLS = 'whitespace-normal break-words max-sm:text-[length:calc(var(--font-size-base)*0.85)]'
const CELL_LABEL_BEFORE_CLS = 'max-sm:before:mb-0.5 max-sm:before:block max-sm:before:text-[length:calc(var(--font-size-base)*0.65)] max-sm:before:uppercase max-sm:before:tracking-[0.05em] max-sm:before:text-[var(--text-muted)] max-sm:before:content-[attr(data-label)]'
const CELL_TYPE_HIDE_CLS = 'max-md:hidden'
const CELL_PRIORITY_HIDE_CLS = 'max-md:hidden'
const CELL_STATE_HIDE_CLS = 'max-md:hidden'
const CELL_STATUS_CLS = 'max-sm:absolute max-sm:left-2 max-sm:top-2 max-sm:w-auto max-sm:p-0'
const CELL_REF_CLS = 'text-[length:calc(var(--font-size-base)*0.75)] text-[var(--text-muted)]'
const CELL_STATE_TEXT_CLS = 'text-[length:calc(var(--font-size-base)*0.8)] capitalize text-[var(--text-secondary)]'

const GROUP_SECTION_CLS = 'mb-4'
const GROUP_HEADER_CLS = 'mb-1 rounded-md bg-[var(--bg-tertiary)] px-3 py-2 text-[length:calc(var(--font-size-base)*0.85)] font-semibold text-[var(--text-primary)]'
const GROUP_COUNT_CLS = 'font-normal text-[var(--text-muted)]'

const SORT_ARROW_MUTED_CLS = 'ml-0.5 text-[length:calc(var(--font-size-base)*0.7)] text-[var(--text-muted)] opacity-40'
const SORT_ARROW_ACTIVE_CLS = 'ml-0.5 text-[length:calc(var(--font-size-base)*0.7)] text-[var(--accent)]'

const LOAD_MORE_WRAP_CLS = 'flex justify-center py-3'
const LOAD_MORE_BTN_CLS = 'inline-flex cursor-pointer items-center gap-2 rounded-md border border-[var(--border)] bg-[var(--bg-secondary)] px-3.5 py-1.5 font-[inherit] text-[length:calc(var(--font-size-base)*0.8)] text-[var(--text-secondary)] transition-colors duration-150 hover:border-[var(--accent)] hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)] disabled:cursor-progress disabled:opacity-60 pointer-coarse:min-h-11'

const SUBTREE_BANNER_CLS = 'mb-2 flex items-center gap-2 rounded-md border border-[var(--border)] bg-[var(--bg-secondary)] px-3 py-2 text-[length:calc(var(--font-size-base)*0.85)] text-[var(--text-secondary)]'
const SUBTREE_LABEL_CLS = 'flex-1'
const SUBTREE_COUNT_CLS = 'text-[length:calc(var(--font-size-base)*0.7)] text-[var(--text-muted)]'
const SUBTREE_CLEAR_CLS = 'cursor-pointer rounded border border-[var(--border)] bg-transparent px-2 py-0.5 text-[length:calc(var(--font-size-base)*0.75)] text-[var(--text-muted)] hover:text-[var(--text-primary)] pointer-coarse:min-h-11'

const SELECT_CHECKBOX_CLS = 'cursor-pointer'
const STATE_BADGES_WRAP_CLS = 'flex flex-wrap items-center gap-1'

// =============================================================================
// Constants
// =============================================================================

type ViewMode = 'list' | 'tree' | 'kanban' | 'priority' | 'audit' | 'gantt' | 'digest' | 'graph'
type GroupBy = 'all' | 'agent'
type SortColumn = 'ref' | 'title' | 'type' | 'priority' | 'state'
type SortDirection = 'asc' | 'desc'

const STATE_FILTER_OPTIONS: TaskDisplayState[] = [
  'ready',
  'in_progress',
  'needs_review',
  'blocked',
  'review_approved',
  'closed',
]

const FILTER_DOT_STATUS: Record<TaskDisplayState, string> = {
  ready: 'ready',
  in_progress: 'in_progress',
  needs_review: 'needs_review',
  blocked: 'blocked',
  review_approved: 'review_approved',
  closed: 'closed',
}

const TYPE_OPTIONS = ['task', 'bug', 'feature', 'epic', 'chore']

const PRIORITY_OPTIONS = [
  { value: 0, label: 'Critical' },
  { value: 1, label: 'High' },
  { value: 2, label: 'Medium' },
  { value: 3, label: 'Low' },
  { value: 4, label: 'Backlog' },
]

// =============================================================================
// View toggle icons
// =============================================================================

function ListIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <line x1="8" y1="6" x2="21" y2="6" /><line x1="8" y1="12" x2="21" y2="12" /><line x1="8" y1="18" x2="21" y2="18" />
      <line x1="3" y1="6" x2="3.01" y2="6" /><line x1="3" y1="12" x2="3.01" y2="12" /><line x1="3" y1="18" x2="3.01" y2="18" />
    </svg>
  )
}

function TreeIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M6 3v12" /><path d="M18 9a3 3 0 1 0 0-6 3 3 0 0 0 0 6z" />
      <path d="M6 21a3 3 0 1 0 0-6 3 3 0 0 0 0 6z" /><path d="M15 6a9 9 0 0 0-9 9" />
    </svg>
  )
}

function KanbanIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="3" y="3" width="5" height="18" rx="1" /><rect x="10" y="3" width="5" height="12" rx="1" /><rect x="17" y="3" width="5" height="15" rx="1" />
    </svg>
  )
}

function PriorityIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z" />
      <line x1="4" y1="22" x2="4" y2="15" />
    </svg>
  )
}

function AuditIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
      <polyline points="14 2 14 8 20 8" />
      <line x1="16" y1="13" x2="8" y2="13" /><line x1="16" y1="17" x2="8" y2="17" /><polyline points="10 9 9 9 8 9" />
    </svg>
  )
}

function GanttIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="3" y="4" width="12" height="4" rx="1" />
      <rect x="7" y="10" width="14" height="4" rx="1" />
      <rect x="5" y="16" width="10" height="4" rx="1" />
    </svg>
  )
}

function DigestIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="3" y="3" width="18" height="18" rx="2" />
      <line x1="7" y1="8" x2="17" y2="8" />
      <line x1="7" y1="12" x2="13" y2="12" />
      <line x1="7" y1="16" x2="15" y2="16" />
    </svg>
  )
}

function GraphIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="6" cy="6" r="3" /><circle cx="18" cy="6" r="3" /><circle cx="12" cy="18" r="3" />
      <line x1="9" y1="6" x2="15" y2="6" /><line x1="7.5" y1="8.5" x2="10.5" y2="15.5" /><line x1="16.5" y1="8.5" x2="13.5" y2="15.5" />
    </svg>
  )
}

function PlusIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
      <line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" />
    </svg>
  )
}

// =============================================================================
// Sorting helpers
// =============================================================================

function compareTasks(a: GobbyTask, b: GobbyTask, col: SortColumn, dir: SortDirection): number {
  let cmp = 0
  switch (col) {
    case 'ref':
      cmp = (a.seq_num ?? 0) - (b.seq_num ?? 0)
      break
    case 'title':
      cmp = a.title.localeCompare(b.title)
      break
    case 'type':
      cmp = a.task_type.localeCompare(b.task_type)
      break
    case 'priority':
      cmp = a.priority - b.priority
      break
    case 'state':
      cmp = TASK_STATE_ORDER.indexOf(getTaskDisplayState(a)) - TASK_STATE_ORDER.indexOf(getTaskDisplayState(b))
      break
  }
  return dir === 'asc' ? cmp : -cmp
}

function groupKeyPart(value: string): string {
  // Length-prefix values so agent:, session:, and agent-session: composite keys
  // cannot collide when agent names or short session IDs contain separators.
  return `${value.length}:${value}`
}

function groupKeyAndLabel(t: GobbyTask): { key: string; label: string; sortPriority: number } {
  const ownerId = getCanonicalTaskState(t).owner_session_id ?? null
  const shortId = ownerId ? ownerId.slice(0, 8) : null
  if (t.agent_name && ownerId && shortId) {
    return {
      key: `agent-session:${groupKeyPart(t.agent_name)}:${groupKeyPart(ownerId)}`,
      label: `${t.agent_name} #${shortId}`,
      sortPriority: 0,
    }
  }
  if (t.agent_name) {
    return { key: `agent:${groupKeyPart(t.agent_name)}`, label: t.agent_name, sortPriority: 0 }
  }
  if (ownerId && shortId) {
    return { key: `session:${groupKeyPart(ownerId)}`, label: `#${shortId}`, sortPriority: 1 }
  }
  return { key: 'unassigned:all', label: 'Unassigned', sortPriority: 2 }
}

function groupTasksByAgent(tasks: GobbyTask[]): Array<{ key: string; label: string; tasks: GobbyTask[] }> {
  const groups = new Map<string, { key: string; label: string; sortPriority: number; tasks: GobbyTask[] }>()
  for (const t of tasks) {
    const { key, label, sortPriority } = groupKeyAndLabel(t)
    const existing = groups.get(key)
    if (existing) {
      existing.tasks.push(t)
    } else {
      groups.set(key, { key, label, sortPriority, tasks: [t] })
    }
  }
  return Array.from(groups.values())
    .sort((a, b) => a.sortPriority - b.sortPriority || a.label.localeCompare(b.label))
    .map(({ key, label, tasks: groupTasks }) => ({ key, label, tasks: groupTasks }))
}

function SortArrow({ column, sortColumn, sortDirection }: { column: SortColumn; sortColumn: SortColumn; sortDirection: SortDirection }) {
  if (column !== sortColumn) return <span className={SORT_ARROW_MUTED_CLS}>{'↕'}</span>
  return <span className={SORT_ARROW_ACTIVE_CLS}>{sortDirection === 'asc' ? '↑' : '↓'}</span>
}

// =============================================================================
// TaskRow
// =============================================================================

function TaskRow({ task, onSelect, isSelected, onToggleSelect }: {
  task: GobbyTask
  onSelect: (id: string) => void
  isSelected?: boolean
  onToggleSelect?: (id: string) => void
}) {
  return (
    <tr className={ROW_CLS} onClick={() => onSelect(task.id)}>
      <td className={cn(CELL_CLS, CELL_STATUS_CLS)} data-label="">
        {onToggleSelect ? (
          <input
            type="checkbox"
            className={SELECT_CHECKBOX_CLS}
            checked={isSelected || false}
            onChange={e => { e.stopPropagation(); onToggleSelect(task.id) }}
            onClick={e => e.stopPropagation()}
          />
        ) : (
          <StatusDot task={task} />
        )}
      </td>
      <td className={cn(CELL_CLS, CELL_LABEL_BEFORE_CLS)} data-label="Ref">
        <span className={CELL_REF_CLS}>{task.ref}</span>
      </td>
      <td className={cn(CELL_CLS, CELL_TITLE_CLS, CELL_LABEL_BEFORE_CLS)} data-label="Title">{task.title}</td>
      <td className={cn(CELL_CLS, CELL_TYPE_HIDE_CLS, CELL_LABEL_BEFORE_CLS)} data-label="Type">
        <TypeBadge type={task.task_type} />
      </td>
      <td className={cn(CELL_CLS, CELL_PRIORITY_HIDE_CLS, CELL_LABEL_BEFORE_CLS)} data-label="Priority">
        <PriorityBadge priority={task.priority} />
      </td>
      <td className={cn(CELL_CLS, CELL_STATE_HIDE_CLS, CELL_STATE_TEXT_CLS, CELL_LABEL_BEFORE_CLS)} data-label="State">
        <div className={STATE_BADGES_WRAP_CLS}>
          <TaskStateBadges task={task} />
        </div>
      </td>
    </tr>
  )
}

// =============================================================================
// TasksPage
// =============================================================================

interface TasksPageProps {
  projectFilter?: string
}

export function TasksPage({ projectFilter }: TasksPageProps = {}) {
  const {
    allTasks,
    tasks,
    stats,
    hasMore,
    isLoadingMore,
    loadMore,
    isLoading,
    filters,
    setFilters,
    refreshTasks,
    getTask,
    createTask,
    updateTask,
    claimTask,
    releaseTaskClaim,
    escalateTask,
    deEscalateTask,
    advanceStage,
    failStage,
    closeTask,
    reopenTask,
    getDependencies,
    getSubtasks,
  } = useTasks(projectFilter)
  const { registry } = useStagesRegistry()
  const [viewMode, setViewMode] = useState<ViewMode>('list')
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null)
  const [showCreateForm, setShowCreateForm] = useState(false)
  const [cloneDefaults, setCloneDefaults] = useState<TaskCreateDefaults | null>(null)
  const [groupBy, setGroupBy] = useState<GroupBy>('all')
  const [sortColumn, setSortColumn] = useState<SortColumn>('ref')
  const [sortDirection, setSortDirection] = useState<SortDirection>('desc')
  const [subtreeRootId, setSubtreeRootId] = useState<string | null>(null)
  const [selectedTaskIds, setSelectedTaskIds] = useState<Set<string>>(new Set())

  const toggleTaskSelection = useCallback((taskId: string) => {
    setSelectedTaskIds(prev => {
      const next = new Set(prev)
      if (next.has(taskId)) next.delete(taskId)
      else next.add(taskId)
      return next
    })
  }, [])

  const handleSort = useCallback((col: SortColumn) => {
    setSortColumn(prev => {
      if (prev === col) {
        setSortDirection(d => d === 'asc' ? 'desc' : 'asc')
        return col
      }
      setSortDirection('asc')
      return col
    })
  }, [])

  const scopedTasks = useMemo(() => {
    const sorted = [...tasks].sort((a, b) => compareTasks(a, b, sortColumn, sortDirection))
    return sorted
  }, [tasks, sortColumn, sortDirection])

  const displayTasks = scopedTasks
  const groupedByAgent = useMemo(() => groupTasksByAgent(displayTasks), [displayTasks])

  const selectedTaskObjects = useMemo(() => {
    return displayTasks.filter(t => selectedTaskIds.has(t.id)).map(t => ({
      id: t.id,
      title: t.title,
      category: null as string | null,
    }))
  }, [displayTasks, selectedTaskIds])

  const kanbanTasks = useMemo(() => {
    if (!subtreeRootId) return displayTasks
    const descendantIds = new Set<string>()
    const collect = (parentId: string) => {
      for (const t of allTasks) {
        if (t.parent_task_id === parentId && !descendantIds.has(t.id)) {
          descendantIds.add(t.id)
          collect(t.id)
        }
      }
    }
    collect(subtreeRootId)
    const parentIds = new Set(allTasks.map(t => t.parent_task_id).filter(Boolean))
    return displayTasks.filter(t => descendantIds.has(t.id) && !parentIds.has(t.id))
  }, [allTasks, subtreeRootId, displayTasks])

  const subtreeRoot = subtreeRootId ? allTasks.find(t => t.id === subtreeRootId) : null

  const handleSubtreeKanban = useCallback((taskId: string) => {
    setSubtreeRootId(taskId)
    setViewMode('kanban')
  }, [])

  const hasActiveFilters = filters.status !== null || filters.priority !== null
    || filters.taskType !== null || filters.assignee !== null

  const createDefaults = useMemo((): TaskCreateDefaults => {
    if (cloneDefaults) return cloneDefaults

    const defaults: TaskCreateDefaults = {}
    if (filters.taskType) defaults.taskType = filters.taskType
    if (filters.priority !== null) defaults.priority = filters.priority
    if (selectedTaskId) {
      const selected = allTasks.find(t => t.id === selectedTaskId)
      if (selected && (selected.task_type === 'epic' || selected.task_type === 'task')) {
        defaults.parentTaskId = selectedTaskId
      }
    }
    return defaults
  }, [filters.taskType, filters.priority, selectedTaskId, allTasks, cloneDefaults])

  return (
    <main className={PAGE_CLS}>
      <div className={TOOLBAR_CLS}>
        <div className={TOOLBAR_LEFT_CLS}>
          <h1 className={TITLE_CLS}>Tasks</h1>
          <div className={GROUP_TABS_CLS}>
            <button className={cn(GROUP_TAB_CLS, groupBy === 'all' && GROUP_TAB_ACTIVE_CLS)} onClick={() => setGroupBy('all')}>All Tasks</button>
            <button className={cn(GROUP_TAB_CLS, groupBy === 'agent' && GROUP_TAB_ACTIVE_CLS)} onClick={() => setGroupBy('agent')}>By Agent</button>
          </div>
        </div>
        <div className={TOOLBAR_RIGHT_CLS}>
          <div className={VIEW_TOGGLE_CLS}>
            {([['list', ListIcon], ['tree', TreeIcon], ['kanban', KanbanIcon], ['priority', PriorityIcon], ['audit', AuditIcon], ['gantt', GanttIcon], ['digest', DigestIcon], ['graph', GraphIcon]] as const).map(
              ([mode, Icon]) => (
                <button
                  key={mode}
                  className={cn(VIEW_BTN_CLS, viewMode === mode && VIEW_BTN_ACTIVE_CLS)}
                  onClick={() => setViewMode(mode as ViewMode)}
                  title={`${mode.charAt(0).toUpperCase() + mode.slice(1)} view`}
                >
                  <Icon />
                </button>
              )
            )}
          </div>
          <input
            type="text"
            className={SEARCH_CLS}
            placeholder="Search"
            value={filters.search}
            onChange={e => setFilters(f => ({ ...f, search: e.target.value }))}
          />
          <button className={REFRESH_BTN_CLS} onClick={refreshTasks} title="Refresh">
            ↻
          </button>
          <button className={NEW_BTN_CLS} title="New Task" onClick={() => setShowCreateForm(true)}>
            <PlusIcon />
            <span>New Task</span>
          </button>
        </div>
      </div>

      <div className={FILTER_BAR_CLS}>
        <div className={FILTER_CHIPS_CLS}>
          {STATE_FILTER_OPTIONS.filter(bucket => (stats[bucket] || 0) > 0).map(bucket => (
              <button
                key={bucket}
                className={cn(STAT_CHIP_CLS, filters.status === bucket && STAT_CHIP_ACTIVE_CLS)}
                onClick={() =>
                  setFilters(f => ({ ...f, status: f.status === bucket ? null : bucket }))
                }
              >
                <StatusDot status={FILTER_DOT_STATUS[bucket]} />
                {TASK_STATE_LABELS[bucket]} ({stats[bucket] || 0})
              </button>
          ))}
        </div>
        <div className={FILTER_DROPDOWNS_CLS}>
          <select
            className={FILTER_SELECT_CLS}
            value={filters.priority ?? ''}
            onChange={e =>
              setFilters(f => ({
                ...f,
                priority: e.target.value === '' ? null : Number(e.target.value),
              }))
            }
          >
            <option value="">All Priorities</option>
            {PRIORITY_OPTIONS.map(p => (
              <option key={p.value} value={p.value}>{p.label}</option>
            ))}
          </select>
          <select
            className={FILTER_SELECT_CLS}
            value={filters.taskType ?? ''}
            onChange={e =>
              setFilters(f => ({ ...f, taskType: e.target.value || null }))
            }
          >
            <option value="">All Types</option>
            {TYPE_OPTIONS.map(t => (
              <option key={t} value={t}>{t}</option>
            ))}
          </select>
          <select
            className={FILTER_SELECT_CLS}
            value={filters.status ?? ''}
            onChange={e =>
              setFilters(f => ({ ...f, status: e.target.value || null }))
            }
          >
            <option value="">All States</option>
            {STATE_FILTER_OPTIONS.map(bucket => (
              <option key={bucket} value={bucket}>{TASK_STATE_LABELS[bucket]}</option>
            ))}
          </select>
          {hasActiveFilters && (
            <button
              className={FILTER_CLEAR_CLS}
              onClick={() =>
                setFilters(f => ({
                  ...f,
                  status: null,
                  priority: null,
                  taskType: null,
                  assignee: null,
                }))
              }
            >
              Clear filters
            </button>
          )}
        </div>
      </div>

      {isLoading ? (
        <div className={STATE_CONTENT_LOADING_CLS}>Loading tasks...</div>
      ) : displayTasks.length === 0 ? (
        <div className={STATE_CONTENT_LOADING_CLS}>No tasks found</div>
      ) : viewMode === 'digest' ? (
        <DigestView
          tasks={displayTasks}
          onSelectTask={setSelectedTaskId}
        />
      ) : viewMode === 'graph' ? (
        <DependencyGraph
          tasks={displayTasks}
          onSelectTask={setSelectedTaskId}
        />
      ) : viewMode === 'gantt' ? (
        <GanttChart
          tasks={displayTasks}
          onSelectTask={setSelectedTaskId}
          onReschedule={(taskId, offsetDays) => {
            const task = displayTasks.find(t => t.id === taskId)
            const currentOrder = task?.sequence_order ?? 0
            updateTask(taskId, { sequence_order: currentOrder + offsetDays * 1000 })
          }}
        />
      ) : viewMode === 'audit' ? (
        <AuditLog
          tasks={displayTasks}
          onSelectTask={setSelectedTaskId}
        />
      ) : viewMode === 'priority' ? (
        <PriorityBoard
          tasks={displayTasks}
          onSelectTask={setSelectedTaskId}
          onAdvanceStage={advanceStage}
        />
      ) : viewMode === 'kanban' ? (
        <>
          {subtreeRoot && (
            <div className={SUBTREE_BANNER_CLS}>
              <span className={SUBTREE_LABEL_CLS}>
                {'▦'} Subtree of <strong>{subtreeRoot.ref}</strong> {subtreeRoot.title}
              </span>
              <span className={SUBTREE_COUNT_CLS}>{kanbanTasks.length} leaf task{kanbanTasks.length !== 1 ? 's' : ''}</span>
              <button className={SUBTREE_CLEAR_CLS} onClick={() => setSubtreeRootId(null)}>
                {'✕'} Show all
              </button>
            </div>
          )}
          <LifecycleBoard
            tasks={subtreeRootId ? kanbanTasks : displayTasks}
            registry={registry}
            onSelectTask={setSelectedTaskId}
            onAdvanceStage={advanceStage}
            onFailStage={failStage}
          />
        </>
      ) : viewMode === 'tree' ? (
        <TaskTree
          tasks={displayTasks}
          onSelectTask={setSelectedTaskId}
          onReparent={(taskId, newParentId) => updateTask(taskId, { parent_task_id: newParentId || '' })}
          onSubtreeKanban={handleSubtreeKanban}
        />
      ) : (
        <div className={TABLE_CONTAINER_CLS}>
          {groupBy === 'agent' ? (
            <>
              {groupedByAgent.map(({ key, label, tasks: agentTasks }) => (
                <div key={key} className={GROUP_SECTION_CLS}>
                  <div className={GROUP_HEADER_CLS}>{label} <span className={GROUP_COUNT_CLS}>({agentTasks.length})</span></div>
                  <table className={TABLE_CLS}>
                    <thead>
                      <tr>
                        <th className={TH_CLS} style={{ width: 28 }} aria-label="Select"></th>
                        <th className={cn(TH_CLS, TH_SORTABLE_CLS)} style={{ width: 64 }} onClick={() => handleSort('ref')}>Ref <SortArrow column="ref" sortColumn={sortColumn} sortDirection={sortDirection} /></th>
                        <th className={cn(TH_CLS, TH_SORTABLE_CLS)} onClick={() => handleSort('title')}>Title <SortArrow column="title" sortColumn={sortColumn} sortDirection={sortDirection} /></th>
                        <th className={cn(TH_CLS, TH_SORTABLE_CLS, CELL_TYPE_HIDE_CLS)} style={{ width: 80 }} onClick={() => handleSort('type')}>Type <SortArrow column="type" sortColumn={sortColumn} sortDirection={sortDirection} /></th>
                        <th className={cn(TH_CLS, TH_SORTABLE_CLS, CELL_PRIORITY_HIDE_CLS)} style={{ width: 80 }} onClick={() => handleSort('priority')}>Priority <SortArrow column="priority" sortColumn={sortColumn} sortDirection={sortDirection} /></th>
                        <th className={cn(TH_CLS, TH_SORTABLE_CLS, CELL_STATE_HIDE_CLS)} style={{ width: 140 }} onClick={() => handleSort('state')}>State <SortArrow column="state" sortColumn={sortColumn} sortDirection={sortDirection} /></th>
                      </tr>
                    </thead>
                    <tbody>
                      {agentTasks.map(task => (
                        <TaskRow key={task.id} task={task} onSelect={setSelectedTaskId} isSelected={selectedTaskIds.has(task.id)} onToggleSelect={toggleTaskSelection} />
                      ))}
                    </tbody>
                  </table>
                </div>
              ))}
            </>
          ) : (
            <table className={TABLE_CLS}>
              <thead>
                <tr>
                  <th className={TH_CLS} style={{ width: 28 }} aria-label="Select"></th>
                  <th className={cn(TH_CLS, TH_SORTABLE_CLS)} style={{ width: 64 }} onClick={() => handleSort('ref')}>Ref <SortArrow column="ref" sortColumn={sortColumn} sortDirection={sortDirection} /></th>
                  <th className={cn(TH_CLS, TH_SORTABLE_CLS)} onClick={() => handleSort('title')}>Title <SortArrow column="title" sortColumn={sortColumn} sortDirection={sortDirection} /></th>
                  <th className={cn(TH_CLS, TH_SORTABLE_CLS, CELL_TYPE_HIDE_CLS)} style={{ width: 80 }} onClick={() => handleSort('type')}>Type <SortArrow column="type" sortColumn={sortColumn} sortDirection={sortDirection} /></th>
                  <th className={cn(TH_CLS, TH_SORTABLE_CLS, CELL_PRIORITY_HIDE_CLS)} style={{ width: 80 }} onClick={() => handleSort('priority')}>Priority <SortArrow column="priority" sortColumn={sortColumn} sortDirection={sortDirection} /></th>
                  <th className={cn(TH_CLS, TH_SORTABLE_CLS, CELL_STATE_HIDE_CLS)} style={{ width: 140 }} onClick={() => handleSort('state')}>State <SortArrow column="state" sortColumn={sortColumn} sortDirection={sortDirection} /></th>
                </tr>
              </thead>
              <tbody>
                {displayTasks.map(task => (
                  <TaskRow key={task.id} task={task} onSelect={setSelectedTaskId} isSelected={selectedTaskIds.has(task.id)} onToggleSelect={toggleTaskSelection} />
                ))}
              </tbody>
            </table>
          )}
          {hasMore && groupBy !== 'agent' && (
            <div className={LOAD_MORE_WRAP_CLS}>
              <button
                type="button"
                className={LOAD_MORE_BTN_CLS}
                onClick={() => { void loadMore() }}
                disabled={isLoadingMore}
              >
                {isLoadingMore ? 'Loading…' : 'Load more'}
              </button>
            </div>
          )}
        </div>
      )}

      <TaskSelectionToolbar
        selectedTasks={selectedTaskObjects}
        projectId={projectFilter}
        onClearSelection={() => setSelectedTaskIds(new Set())}
        onBatchSpawned={() => refreshTasks()}
      />

      <TaskDetail
        taskId={selectedTaskId}
        getTask={getTask}
        getDependencies={getDependencies}
        getSubtasks={getSubtasks}
        actions={{
          claimTask,
          releaseTaskClaim,
          advanceStage,
          escalateTask,
          deEscalateTask,
          closeTask,
          reopenTask,
        }}
        onSelectTask={setSelectedTaskId}
        onClose={() => setSelectedTaskId(null)}
      />

      <TaskCreateForm
        isOpen={showCreateForm}
        tasks={allTasks}
        defaults={createDefaults}
        onSubmit={createTask}
        onClose={() => { setShowCreateForm(false); setCloneDefaults(null) }}
      />
    </main>
  )
}
