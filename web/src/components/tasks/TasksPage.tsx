import { useState, useMemo, useCallback, useEffect } from 'react'
import './tasks-page.css'
import './task-views.css'
import { useTasks } from '../../hooks/useTasks'
import type { GobbyTask, GobbyTaskDetail } from '../../hooks/useTasks'
import { StatusDot, PriorityBadge, TypeBadge } from './TaskBadges'
import { TaskDetail } from './TaskDetail'
import { TaskCreateForm } from './TaskCreateForm'
import type { TaskCreateDefaults } from './TaskCreateForm'
import { KanbanBoard } from './KanbanBoard'
import { TaskTree } from './TaskTree'
import { PriorityBoard } from './PriorityBoard'
import { TaskOverview } from './TaskOverview'
import { AuditLog } from './AuditLog'
import { GanttChart } from './GanttChart'
import { DigestView } from './DigestView'
import { DependencyGraph } from './DependencyGraph'
import { TaskSelectionToolbar } from './TaskSelectionToolbar'

// =============================================================================
// Constants
// =============================================================================

type ViewMode = 'list' | 'tree' | 'kanban' | 'priority' | 'audit' | 'gantt' | 'digest' | 'graph'
type GroupBy = 'all' | 'agent'
type SortColumn = 'ref' | 'title' | 'type' | 'priority' | 'status'
type SortDirection = 'asc' | 'desc'

const STATUS_OPTIONS = [
  'open', 'in_progress', 'needs_review', 'review_approved', 'closed', 'escalated',
]

// Statuses grouped under the 'closed' filter
const CLOSED_GROUP = ['closed']

const TYPE_OPTIONS = ['task', 'bug', 'feature', 'epic', 'chore']

const PRIORITY_OPTIONS = [
  { value: 0, label: 'Critical' },
  { value: 1, label: 'High' },
  { value: 2, label: 'Medium' },
  { value: 3, label: 'Low' },
  { value: 4, label: 'Backlog' },
]

const RECENTLY_DONE_CUTOFF_MS = 24 * 60 * 60 * 1000

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
    case 'status':
      cmp = a.status.localeCompare(b.status)
      break
  }
  return dir === 'asc' ? cmp : -cmp
}

function groupTasksByAgent(tasks: GobbyTask[]): Map<string, GobbyTask[]> {
  const groups = new Map<string, GobbyTask[]>()
  for (const t of tasks) {
    const key = t.agent_name || t.assignee || 'Unassigned'
    const arr = groups.get(key) || []
    arr.push(t)
    groups.set(key, arr)
  }
  return groups
}

function SortArrow({ column, sortColumn, sortDirection }: { column: SortColumn; sortColumn: SortColumn; sortDirection: SortDirection }) {
  if (column !== sortColumn) return <span className="sort-arrow muted">{'\u2195'}</span>
  return <span className="sort-arrow active">{sortDirection === 'asc' ? '\u2191' : '\u2193'}</span>
}

// =============================================================================
// TaskRow
// =============================================================================

function TaskRow({ task, onSelect, isSelected, onToggleSelect }: {
  task: GobbyTask
  onSelect: (id: string) => void
  isSelected?: boolean
  onToggleSelect?: (id: string, e: React.MouseEvent) => void
}) {
  return (
    <tr className={`tasks-row ${isSelected ? 'tasks-row--selected' : ''}`} onClick={() => onSelect(task.id)} style={{ cursor: 'pointer' }}>
      <td className="tasks-cell tasks-cell--status">
        {onToggleSelect ? (
          <input
            type="checkbox"
            className="task-select-checkbox"
            checked={isSelected || false}
            onChange={() => {}}
            onClick={e => { e.stopPropagation(); onToggleSelect(task.id, e as any) }}
          />
        ) : (
          <StatusDot status={task.status} />
        )}
      </td>
      <td className="tasks-cell tasks-cell--ref">
        <span className="tasks-ref">{task.ref}</span>
      </td>
      <td className="tasks-cell tasks-cell--title">{task.title}</td>
      <td className="tasks-cell tasks-cell--type">
        <TypeBadge type={task.task_type} />
      </td>
      <td className="tasks-cell tasks-cell--priority">
        <PriorityBadge priority={task.priority} />
      </td>
      <td className="tasks-cell tasks-cell--status-text">{task.status.replace(/_/g, ' ')}</td>
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
  const { tasks, total, stats, isLoading, filters, setFilters, refreshTasks, getTask, createTask, updateTask, closeTask, reopenTask, getDependencies, getSubtasks } = useTasks()

  // Sync global project filter into the hook's server-side filtering
  useEffect(() => {
    setFilters(f => ({ ...f, projectId: projectFilter ?? null }))
  }, [projectFilter, setFilters])
  const [viewMode, setViewMode] = useState<ViewMode>('list')
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null)
  const [showCreateForm, setShowCreateForm] = useState(false)
  const [cloneDefaults, setCloneDefaults] = useState<TaskCreateDefaults | null>(null)
  const [groupBy, setGroupBy] = useState<GroupBy>('all')
  const [sortColumn, setSortColumn] = useState<SortColumn>('ref')
  const [sortDirection, setSortDirection] = useState<SortDirection>('desc')
  const [subtreeRootId, setSubtreeRootId] = useState<string | null>(null)
  const [selectedTaskIds, setSelectedTaskIds] = useState<Set<string>>(new Set())

  const toggleTaskSelection = useCallback((taskId: string, _event?: React.MouseEvent) => {
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

  // Sorted tasks
  const scopedTasks = useMemo(() => {
    const sorted = [...tasks].sort((a, b) => compareTasks(a, b, sortColumn, sortDirection))
    return sorted
  }, [tasks, sortColumn, sortDirection])

  // Compute stats from the filtered task array so overview cards reflect project scope
  const localStats = useMemo(() => {
    const counts: Record<string, number> = {}
    for (const t of scopedTasks) {
      counts[t.status] = (counts[t.status] || 0) + 1
    }
    return counts
  }, [scopedTasks])

  // Apply 24h cutoff when "Recently Done" filter is active so the displayed
  // task list matches the overview card count (which uses the same cutoff).
  const displayTasks = useMemo(() => {
    if (filters.status !== 'recently_done') return scopedTasks
    const cutoff = Date.now() - RECENTLY_DONE_CUTOFF_MS
    const completed = new Set(['closed'])
    return scopedTasks.filter(
      t => completed.has(t.status) && new Date(t.updated_at).getTime() > cutoff
    )
  }, [scopedTasks, filters.status])

  const selectedTaskObjects = useMemo(() => {
    return displayTasks.filter(t => selectedTaskIds.has(t.id)).map(t => ({
      id: t.id,
      title: t.title,
      category: null as string | null,
    }))
  }, [displayTasks, selectedTaskIds])

  // Subtree kanban: filter to leaf tasks under a specific parent
  const kanbanTasks = useMemo(() => {
    if (!subtreeRootId) return scopedTasks
    // Collect all descendant IDs
    const descendantIds = new Set<string>()
    const collect = (parentId: string) => {
      for (const t of tasks) {
        if (t.parent_task_id === parentId && !descendantIds.has(t.id)) {
          descendantIds.add(t.id)
          collect(t.id)
        }
      }
    }
    collect(subtreeRootId)
    // Leaf = has no children in the task set
    const parentIds = new Set(tasks.map(t => t.parent_task_id).filter(Boolean))
    return tasks.filter(t => descendantIds.has(t.id) && !parentIds.has(t.id))
  }, [tasks, subtreeRootId, scopedTasks])

  const subtreeRoot = subtreeRootId ? tasks.find(t => t.id === subtreeRootId) : null

  const handleSubtreeKanban = useCallback((taskId: string) => {
    setSubtreeRootId(taskId)
    setViewMode('kanban')
  }, [])

  const hasActiveFilters = filters.status !== null || filters.priority !== null
    || filters.taskType !== null || filters.assignee !== null

  // Context-aware defaults for task creation
  const createDefaults = useMemo((): TaskCreateDefaults => {
    // Clone defaults take priority when set
    if (cloneDefaults) return cloneDefaults

    const defaults: TaskCreateDefaults = {}
    // Pre-fill type from active filter
    if (filters.taskType) defaults.taskType = filters.taskType
    // Pre-fill priority from active filter
    if (filters.priority !== null) defaults.priority = filters.priority
    // Pre-fill parent from selected task (if it's an epic/task)
    if (selectedTaskId) {
      const selected = tasks.find(t => t.id === selectedTaskId)
      if (selected && (selected.task_type === 'epic' || selected.task_type === 'task')) {
        defaults.parentTaskId = selectedTaskId
      }
    }
    return defaults
  }, [filters.taskType, filters.priority, selectedTaskId, tasks, cloneDefaults])

  const handleClone = useCallback((task: GobbyTaskDetail) => {
    setCloneDefaults({
      title: `[Clone] ${task.title}`,
      description: task.description || undefined,
      taskType: task.task_type,
      priority: task.priority,
      validationCriteria: task.validation_criteria || undefined,
      labels: task.labels || undefined,
      parentTaskId: task.parent_task_id || undefined,
    })
    setShowCreateForm(true)
  }, [])

  return (
    <main className="tasks-page">
      {/* Toolbar */}
      <div className="tasks-toolbar">
        <div className="tasks-toolbar-left">
          <h2 className="tasks-title">Tasks</h2>
          <span className="tasks-count">{total} total</span>
          <div className="task-group-tabs">
            <button className={`task-group-tab ${groupBy === 'all' ? 'active' : ''}`} onClick={() => setGroupBy('all')}>All Tasks</button>
            <button className={`task-group-tab ${groupBy === 'agent' ? 'active' : ''}`} onClick={() => setGroupBy('agent')}>By Agent</button>
          </div>
        </div>
        <div className="tasks-toolbar-right">
          <div className="tasks-view-toggle">
            {([['list', ListIcon], ['tree', TreeIcon], ['kanban', KanbanIcon], ['priority', PriorityIcon], ['audit', AuditIcon], ['gantt', GanttIcon], ['digest', DigestIcon], ['graph', GraphIcon]] as const).map(
              ([mode, Icon]) => (
                <button
                  key={mode}
                  className={`tasks-view-btn ${viewMode === mode ? 'active' : ''}`}
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
            className="tasks-search"
            placeholder="Search tasks..."
            value={filters.search}
            onChange={e => setFilters(f => ({ ...f, search: e.target.value }))}
          />
          <button className="tasks-refresh-btn" onClick={refreshTasks} title="Refresh">
            ↻
          </button>
          <button className="tasks-new-btn" title="New Task" onClick={() => setShowCreateForm(true)}>
            <PlusIcon />
            <span>New Task</span>
          </button>
        </div>
      </div>

      {/* Overview cards */}
      <TaskOverview
        tasks={scopedTasks}
        stats={stats}
        activeFilter={filters.status}
        onFilterStatus={status => setFilters(f => ({ ...f, status }))}
      />

      {/* Filter bar */}
      <div className="tasks-filter-bar">
        <div className="tasks-filter-chips">
          {STATUS_OPTIONS.filter(status => {
              const count = status === 'closed'
                ? CLOSED_GROUP.reduce((sum, s) => sum + ((localStats as Record<string, number>)[s] || 0), 0)
                : (localStats as Record<string, number>)[status] || 0
              return count > 0
            }).map(status => {
              const count = status === 'closed'
                ? CLOSED_GROUP.reduce((sum, s) => sum + ((localStats as Record<string, number>)[s] || 0), 0)
                : (localStats as Record<string, number>)[status] || 0
              return (
              <button
                key={status}
                className={`tasks-stat-chip ${filters.status === status ? 'active' : ''}`}
                onClick={() =>
                  setFilters(f => ({ ...f, status: f.status === status ? null : status }))
                }
              >
                <StatusDot status={status} />
                {status.replace(/_/g, ' ')} ({count})
              </button>
              )
          })}
        </div>
        <div className="tasks-filter-dropdowns">
          <select
            className="tasks-filter-select"
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
            className="tasks-filter-select"
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
            className="tasks-filter-select"
            value={filters.status ?? ''}
            onChange={e =>
              setFilters(f => ({ ...f, status: e.target.value || null }))
            }
          >
            <option value="">All Statuses</option>
            {STATUS_OPTIONS.map(s => (
              <option key={s} value={s}>{s.replace(/_/g, ' ')}</option>
            ))}
          </select>
          {hasActiveFilters && (
            <button
              className="tasks-filter-clear"
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

      {/* Content */}
      {isLoading ? (
        <div className="tasks-loading">Loading tasks...</div>
      ) : displayTasks.length === 0 ? (
        <div className="tasks-empty">No tasks found</div>
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
            // Persist position change via sequence_order (offset * 1000 for granularity)
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
          onUpdateStatus={(taskId, newStatus) => updateTask(taskId, { status: newStatus })}
        />
      ) : viewMode === 'kanban' ? (
        <>
          {subtreeRoot && (
            <div className="subtree-kanban-banner">
              <span className="subtree-kanban-label">
                {'\u25A6'} Subtree of <strong>{subtreeRoot.ref}</strong> {subtreeRoot.title}
              </span>
              <span className="subtree-kanban-count">{kanbanTasks.length} leaf task{kanbanTasks.length !== 1 ? 's' : ''}</span>
              <button className="subtree-kanban-clear" onClick={() => setSubtreeRootId(null)}>
                {'\u2715'} Show all
              </button>
            </div>
          )}
          <KanbanBoard
            tasks={subtreeRootId ? kanbanTasks : displayTasks}
            onSelectTask={setSelectedTaskId}
            onUpdateStatus={(taskId, newStatus) => updateTask(taskId, { status: newStatus })}
            onReorder={(taskId, newOrder) => updateTask(taskId, { sequence_order: newOrder })}
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
        <div className="tasks-table-container">
          {groupBy === 'agent' ? (
            <>
              {Array.from(groupTasksByAgent(displayTasks)).map(([agent, agentTasks]) => (
                <div key={agent} className="task-group-section">
                  <div className="task-group-header">{agent} <span className="task-group-count">({agentTasks.length})</span></div>
                  <table className="tasks-table">
                    <thead>
                      <tr>
                        <th className="tasks-th" style={{ width: 28 }}></th>
                        <th className="tasks-th tasks-th--sortable" style={{ width: 64 }} onClick={() => handleSort('ref')}>Ref <SortArrow column="ref" sortColumn={sortColumn} sortDirection={sortDirection} /></th>
                        <th className="tasks-th tasks-th--sortable" onClick={() => handleSort('title')}>Title <SortArrow column="title" sortColumn={sortColumn} sortDirection={sortDirection} /></th>
                        <th className="tasks-th tasks-th--sortable" style={{ width: 80 }} onClick={() => handleSort('type')}>Type <SortArrow column="type" sortColumn={sortColumn} sortDirection={sortDirection} /></th>
                        <th className="tasks-th tasks-th--sortable" style={{ width: 80 }} onClick={() => handleSort('priority')}>Priority <SortArrow column="priority" sortColumn={sortColumn} sortDirection={sortDirection} /></th>
                        <th className="tasks-th tasks-th--sortable" style={{ width: 100 }} onClick={() => handleSort('status')}>Status <SortArrow column="status" sortColumn={sortColumn} sortDirection={sortDirection} /></th>
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
            <table className="tasks-table">
              <thead>
                <tr>
                  <th className="tasks-th" style={{ width: 28 }}></th>
                  <th className="tasks-th tasks-th--sortable" style={{ width: 64 }} onClick={() => handleSort('ref')}>Ref <SortArrow column="ref" sortColumn={sortColumn} sortDirection={sortDirection} /></th>
                  <th className="tasks-th tasks-th--sortable" onClick={() => handleSort('title')}>Title <SortArrow column="title" sortColumn={sortColumn} sortDirection={sortDirection} /></th>
                  <th className="tasks-th tasks-th--sortable" style={{ width: 80 }} onClick={() => handleSort('type')}>Type <SortArrow column="type" sortColumn={sortColumn} sortDirection={sortDirection} /></th>
                  <th className="tasks-th tasks-th--sortable" style={{ width: 80 }} onClick={() => handleSort('priority')}>Priority <SortArrow column="priority" sortColumn={sortColumn} sortDirection={sortDirection} /></th>
                  <th className="tasks-th tasks-th--sortable" style={{ width: 100 }} onClick={() => handleSort('status')}>Status <SortArrow column="status" sortColumn={sortColumn} sortDirection={sortDirection} /></th>
                </tr>
              </thead>
              <tbody>
                {displayTasks.map(task => (
                  <TaskRow key={task.id} task={task} onSelect={setSelectedTaskId} isSelected={selectedTaskIds.has(task.id)} onToggleSelect={toggleTaskSelection} />
                ))}
              </tbody>
            </table>
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
        actions={{ updateTask, closeTask, reopenTask }}
        onSelectTask={setSelectedTaskId}
        onClose={() => setSelectedTaskId(null)}
        onClone={handleClone}
      />

      <TaskCreateForm
        isOpen={showCreateForm}
        tasks={tasks}
        defaults={createDefaults}
        onSubmit={createTask}
        onClose={() => { setShowCreateForm(false); setCloneDefaults(null) }}
      />
    </main>
  )
}
