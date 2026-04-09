import { memo, useState, useEffect, useCallback, useRef, useMemo } from 'react'
import { Tree, type NodeRendererProps } from 'react-arborist'
import { ResizeHandle } from '../chat/artifacts/ResizeHandle'
import { Markdown } from '../chat/Markdown'
import '../tasks/task-execution.css'
import type { GobbyTask } from '../../hooks/useTasks'

interface TasksTabProps {
  projectId?: string | null
}

interface GobbyTaskDetail extends GobbyTask {
  description: string | null
  category: string | null
  validation_criteria: string | null
  closed_at: string | null
}

// =============================================================================
// Tree node type (mirrors TaskTree.tsx)
// =============================================================================

interface TreeNode {
  id: string
  task: GobbyTask
  children: TreeNode[]
}

// =============================================================================
// Constants
// =============================================================================

const CLOSED_STATUSES = new Set(['closed', 'review_approved'])
const ALL_STATUSES = ['open', 'in_progress', 'needs_review', 'review_approved', 'escalated', 'closed']
const DEFAULT_FILTERS = new Set(['open', 'in_progress', 'needs_review', 'review_approved', 'escalated'])
const INITIAL_TASK_LIMIT = 10

const STATUS_DOT_COLORS: Record<string, string> = {
  open: '#3b82f6',
  in_progress: '#f59e0b',
  needs_review: '#8b5cf6',
  review_approved: '#22c55e',
  closed: '#737373',
  escalated: '#ef4444',
}

const PRIORITY_LABELS: Record<number, string> = {
  0: 'Critical',
  1: 'High',
  2: 'Medium',
  3: 'Low',
  4: 'Backlog',
}

const PRIORITY_TEXT_COLORS: Record<number, string> = {
  0: 'var(--status-escalated, #ef4444)',
  1: 'var(--status-escalated, #ef4444)',
  2: 'var(--status-progress, #f59e0b)',
  3: 'var(--text-secondary, #a3a3a3)',
  4: 'var(--text-muted, #737373)',
}

function getBaseUrl(): string {
  return import.meta.env.VITE_API_BASE_URL || ''
}

// =============================================================================
// Build tree from flat task list (same logic as TaskTree.tsx)
// =============================================================================

function buildTree(tasks: GobbyTask[]): TreeNode[] {
  const nodeMap = new Map<string, TreeNode>()
  const roots: TreeNode[] = []

  for (const task of tasks) {
    nodeMap.set(task.id, { id: task.id, task, children: [] })
  }

  for (const task of tasks) {
    const node = nodeMap.get(task.id)!
    if (task.parent_task_id && nodeMap.has(task.parent_task_id)) {
      nodeMap.get(task.parent_task_id)!.children.push(node)
    } else {
      roots.push(node)
    }
  }

  return roots
}

function searchMatch(node: { data: TreeNode }, term: string): boolean {
  const task = node.data.task
  const lower = term.toLowerCase()
  return task.title.toLowerCase().includes(lower) || task.ref.toLowerCase().includes(lower)
}

// =============================================================================
// Lightweight node renderer for panel tree
// =============================================================================

function PanelTaskNode({ node, style }: NodeRendererProps<TreeNode>) {
  const task = node.data.task
  const dotColor = STATUS_DOT_COLORS[task.status] ?? '#737373'
  const textColor = PRIORITY_TEXT_COLORS[task.priority ?? 3] ?? 'var(--text-secondary)'
  const ref = task.seq_num != null ? `#${task.seq_num}` : null

  return (
    <div
      style={style}
      className={`paneltask-row${node.isSelected ? ' paneltask-row--expanded' : ''}${CLOSED_STATUSES.has(task.status) ? ' paneltask-row--closed' : ''}`}
      onClick={() => node.activate()}
    >
      {node.isInternal ? (
        <button
          className="paneltask-tree-toggle"
          onClick={e => { e.stopPropagation(); node.toggle() }}
        >
          {node.isOpen ? '▾' : '▸'}
        </button>
      ) : (
        <span className="paneltask-tree-toggle paneltask-tree-toggle--leaf" />
      )}
      <span
        className="paneltask-status-dot"
        style={{ backgroundColor: dotColor }}
      />
      {ref && <span className="paneltask-ref">{ref}</span>}
      <span className="paneltask-row-title" style={{ color: textColor }}>
        {task.title}
      </span>
    </div>
  )
}

// =============================================================================
// Filter dropdown
// =============================================================================

function FilterDropdown({
  filters,
  onToggle,
  onClose,
}: {
  filters: Set<string>
  onToggle: (status: string) => void
  onClose: () => void
}) {
  return (
    <>
      <div className="paneltask-filter-backdrop" onClick={onClose} />
      <div className="paneltask-filter-dropdown">
        {ALL_STATUSES.map((status) => (
          <label key={status} className="paneltask-filter-chip">
            <input
              type="checkbox"
              checked={filters.has(status)}
              onChange={() => onToggle(status)}
            />
            <span
              className="paneltask-status-dot"
              style={{ backgroundColor: STATUS_DOT_COLORS[status] ?? '#737373' }}
            />
            <span>{status.replace(/_/g, ' ')}</span>
          </label>
        ))}
      </div>
    </>
  )
}

// =============================================================================
// TasksTab
// =============================================================================

export const TasksTab = memo(function TasksTab({ projectId }: TasksTabProps) {
  const [tasks, setTasks] = useState<GobbyTask[]>([])
  const [loading, setLoading] = useState(true)
  const [search, setSearch] = useState('')
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null)
  const [statusFilters, setStatusFilters] = useState<Set<string>>(() => new Set(DEFAULT_FILTERS))
  const [visibleCount, setVisibleCount] = useState(INITIAL_TASK_LIMIT)
  const [showFilterDropdown, setShowFilterDropdown] = useState(false)
  const [topHeight, setTopHeight] = useState(50)
  const [taskDetail, setTaskDetail] = useState<GobbyTaskDetail | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const containerRef = useRef<HTMLDivElement | null>(null)
  const [treeHeight, setTreeHeight] = useState(300)

  // Fetch all tasks (filter client-side)
  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    const baseUrl = getBaseUrl()
    const params = new URLSearchParams()
    if (projectId) params.set('project_id', projectId)
    params.set('limit', '500')
    fetch(`${baseUrl}/api/tasks?${params}`, { signal: controller.signal })
      .then((res) => (res.ok ? res.json() : { tasks: [] }))
      .then((data) => setTasks(data.tasks ?? []))
      .catch((err) => { if (err.name !== 'AbortError') setTasks([]) })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [projectId])

  // Fetch task detail when selected
  useEffect(() => {
    if (!selectedTaskId) { setTaskDetail(null); return }
    const controller = new AbortController()
    setDetailLoading(true)
    const baseUrl = getBaseUrl()
    fetch(`${baseUrl}/api/tasks/${selectedTaskId}`, { signal: controller.signal })
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => setTaskDetail(data?.id ? data : (data?.task ?? null)))
      .catch((err) => { if (err.name !== 'AbortError') setTaskDetail(null) })
      .finally(() => { if (!controller.signal.aborted) setDetailLoading(false) })
    return () => controller.abort()
  }, [selectedTaskId])

  // ResizeObserver for tree height
  useEffect(() => {
    const container = containerRef.current
    if (!container) return
    const observer = new ResizeObserver(([entry]) => {
      const available = entry.contentRect.height - 40
      if (available > 100) setTreeHeight(Math.round(available))
    })
    observer.observe(container)
    return () => observer.disconnect()
  }, [])

  const toggleFilter = useCallback((status: string) => {
    setStatusFilters((prev) => {
      const next = new Set(prev)
      if (next.has(status)) next.delete(status)
      else next.add(status)
      return next
    })
  }, [])

  useEffect(() => {
    setVisibleCount(INITIAL_TASK_LIMIT)
  }, [projectId, search, statusFilters])

  // Client-side filtering
  const now = Date.now()
  const DAY_MS = 24 * 60 * 60 * 1000
  const filtered = useMemo(() => {
    return tasks
      .filter((t) => {
        if (!statusFilters.has(t.status)) {
          // Show closed tasks if closed within 24h and closed filter is on
          if (CLOSED_STATUSES.has(t.status) && statusFilters.has('closed')) {
            const closedAt = (t as GobbyTaskDetail).closed_at
            if (closedAt && now - new Date(closedAt).getTime() < DAY_MS) return true
          }
          return false
        }
        return true
      })
      .sort((a, b) => {
        const pa = a.priority ?? 3
        const pb = b.priority ?? 3
        if (pa !== pb) return pa - pb
        return (b.created_at ?? '').localeCompare(a.created_at ?? '')
      })
  }, [tasks, statusFilters, now])

  const treeData = useMemo(() => {
    const taskMap = new Map(tasks.map((task) => [task.id, task]))
    const visibleIds = new Set<string>()

    for (const task of filtered.slice(0, visibleCount)) {
      let current: GobbyTask | undefined = task
      while (current) {
        if (visibleIds.has(current.id)) break
        visibleIds.add(current.id)
        current = current.parent_task_id ? taskMap.get(current.parent_task_id) : undefined
      }
    }

    const visibleTasks = filtered.filter((task) => visibleIds.has(task.id))
    return buildTree(visibleTasks)
  }, [filtered, tasks, visibleCount])

  const hasMore = filtered.length > visibleCount

  if (loading) {
    return <div className="activity-tab-empty"><p>Loading tasks...</p></div>
  }

  return (
    <div className="flex flex-col h-full">
      {/* Toolbar */}
      <div className="paneltask-toolbar" style={{ position: 'relative' }}>
        <input
          type="text"
          className="paneltask-search"
          placeholder="Search..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <button
          type="button"
          className="paneltask-filter-btn"
          onClick={() => setShowFilterDropdown((v) => !v)}
          title="Filter by status"
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3" />
          </svg>
        </button>
        {showFilterDropdown && (
          <FilterDropdown
            filters={statusFilters}
            onToggle={toggleFilter}
            onClose={() => setShowFilterDropdown(false)}
          />
        )}
      </div>

      {/* Tree pane */}
      <div
        ref={containerRef}
        className={`overflow-y-auto ${selectedTaskId ? 'border-b border-border' : 'flex-1'}`}
        style={selectedTaskId ? { height: `${topHeight}%` } : undefined}
      >
        {filtered.length === 0 ? (
          <div className="activity-tab-empty">
            <p>No tasks match filters</p>
          </div>
        ) : (
          <>
            <Tree<TreeNode>
              data={treeData}
              openByDefault={true}
              width="100%"
              height={selectedTaskId ? undefined : treeHeight}
              rowHeight={30}
              indent={16}
              searchTerm={search}
              searchMatch={searchMatch}
              onActivate={(node) => setSelectedTaskId(node.data.task.id)}
              disableDrag
              disableDrop
            >
              {PanelTaskNode}
            </Tree>
            {hasMore && (
              <button
                className="w-full py-2 text-xs text-muted-foreground hover:text-foreground hover:bg-muted/30 transition-colors"
                onClick={() => setVisibleCount((prev) => prev + INITIAL_TASK_LIMIT)}
              >
                Load more
              </button>
            )}
          </>
        )}
      </div>

      {/* Resize handle */}
      {selectedTaskId && (
        <ResizeHandle direction="vertical" onResize={setTopHeight} panelHeight={topHeight} minHeight={15} maxHeight={80} />
      )}

      {/* Detail pane */}
      {selectedTaskId && (
        <div className="flex-1 flex flex-col min-h-0 overflow-y-auto">
          <div className="paneltask-detail-header">
            <span className="paneltask-detail-header-title">
              {taskDetail ? taskDetail.title : 'Loading...'}
            </span>
            <button
              type="button"
              className="paneltask-detail-close"
              onClick={() => setSelectedTaskId(null)}
              title="Close detail"
            >
              ✕
            </button>
          </div>
          {detailLoading ? (
            <p className="text-xs text-muted-foreground px-3 py-2">Loading...</p>
          ) : taskDetail ? (
            <TaskDetail task={taskDetail} />
          ) : (
            <p className="text-xs text-muted-foreground px-3 py-2">Task not found</p>
          )}
        </div>
      )}
    </div>
  )
})

// =============================================================================
// Task detail panel (extracted from former accordion)
// =============================================================================

function TaskDetail({ task }: { task: GobbyTaskDetail }) {
  const priorityLabel = PRIORITY_LABELS[task.priority ?? 4] ?? 'Backlog'

  return (
    <div className="paneltask-accordion-content">
      <div className="paneltask-accordion-meta">
        <span className="paneltask-accordion-status">{task.status.replace(/_/g, ' ')}</span>
        <span className="paneltask-detail-sep">{'\u00B7'}</span>
        <span>{priorityLabel}</span>
        {task.task_type !== 'task' && (
          <>
            <span className="paneltask-detail-sep">{'\u00B7'}</span>
            <span>{task.task_type}</span>
          </>
        )}
        {task.assignee && (
          <>
            <span className="paneltask-detail-sep">{'\u00B7'}</span>
            <span>{task.assignee}</span>
          </>
        )}
      </div>

      {task.description && (
        <div className="paneltask-accordion-section">
          <div className="message-content text-xs">
            <Markdown content={task.description} id={`task-desc-${task.id}`} />
          </div>
        </div>
      )}

      {task.validation_criteria && (
        <div className="paneltask-accordion-section">
          <div className="paneltask-detail-label">Validation</div>
          <div className="message-content text-xs">
            <Markdown content={task.validation_criteria} id={`task-vc-${task.id}`} />
          </div>
        </div>
      )}

      <div className="paneltask-accordion-dates">
        <span>Created {new Date(task.created_at).toLocaleDateString()}</span>
        {task.closed_at && <span> {'\u00B7'} Closed {new Date(task.closed_at).toLocaleDateString()}</span>}
      </div>
    </div>
  )
}
