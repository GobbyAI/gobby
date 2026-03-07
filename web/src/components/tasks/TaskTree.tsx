import { useMemo, useRef, useState, useCallback, useEffect } from 'react'
import { Tree, TreeApi, NodeRendererProps } from 'react-arborist'
import type { GobbyTask } from '../../hooks/useTasks'
import { StatusDot, PriorityBadge, TypeBadge } from './TaskBadges'
import { TaskStatusStrip } from './TaskStatusStrip'

// =============================================================================
// Tree data type
// =============================================================================

interface TreeNode {
  id: string
  task: GobbyTask
  children: TreeNode[]
}

// =============================================================================
// Closed statuses to filter
// =============================================================================

const CLOSED_STATUSES = new Set(['closed', 'review_approved'])

// =============================================================================
// Build tree from flat task list
// =============================================================================

function buildTree(tasks: GobbyTask[], hideClosed: boolean): TreeNode[] {
  const filtered = hideClosed ? tasks.filter(t => !CLOSED_STATUSES.has(t.status)) : tasks
  const nodeMap = new Map<string, TreeNode>()
  const roots: TreeNode[] = []

  for (const task of filtered) {
    nodeMap.set(task.id, { id: task.id, task, children: [] })
  }

  for (const task of filtered) {
    const node = nodeMap.get(task.id)!
    if (task.parent_task_id && nodeMap.has(task.parent_task_id)) {
      nodeMap.get(task.parent_task_id)!.children.push(node)
    } else {
      roots.push(node)
    }
  }

  return roots
}

// =============================================================================
// Highlight matching text
// =============================================================================

function HighlightText({ text, search }: { text: string; search: string }) {
  if (!search) return <>{text}</>
  const idx = text.toLowerCase().indexOf(search.toLowerCase())
  if (idx === -1) return <>{text}</>
  return (
    <>
      {text.slice(0, idx)}
      <mark className="tree-node-highlight">{text.slice(idx, idx + search.length)}</mark>
      {text.slice(idx + search.length)}
    </>
  )
}

// =============================================================================
// Custom node renderer
// =============================================================================

function makeTaskNode(searchTerm: string, onSubtreeKanban?: (taskId: string) => void) {
  return function TaskNode({ node, style, dragHandle }: NodeRendererProps<TreeNode>) {
    const task = node.data.task
    const [ctxMenu, setCtxMenu] = useState<{ x: number; y: number } | null>(null)

    const handleContextMenu = useCallback((e: React.MouseEvent) => {
      if (!onSubtreeKanban || !node.isInternal) return
      e.preventDefault()
      e.stopPropagation()
      setCtxMenu({ x: e.clientX, y: e.clientY })
    }, [node.isInternal])

    return (
      <div
        ref={dragHandle}
        style={style}
        className={`tree-node ${node.isSelected ? 'tree-node--selected' : ''}`}
        onClick={() => node.activate()}
        onContextMenu={handleContextMenu}
      >
        {node.isInternal ? (
          <button
            className="tree-node-toggle"
            onClick={e => { e.stopPropagation(); node.toggle() }}
          >
            {node.isOpen ? '▾' : '▸'}
          </button>
        ) : (
          <span className="tree-node-toggle tree-node-toggle--leaf" />
        )}
        <StatusDot status={task.status} />
        <span className="tree-node-ref">{task.ref}</span>
        <span className="tree-node-title">
          <HighlightText text={task.title} search={searchTerm} />
        </span>
        <TypeBadge type={task.task_type} />
        <PriorityBadge priority={task.priority} />
        <TaskStatusStrip task={task} compact />

        {ctxMenu && (
          <>
            <div className="tree-ctx-backdrop" onClick={() => setCtxMenu(null)} />
            <div
              className="tree-ctx-menu"
              style={{ position: 'fixed', left: ctxMenu.x, top: ctxMenu.y }}
            >
              <button
                className="tree-ctx-item"
                onClick={e => {
                  e.stopPropagation()
                  setCtxMenu(null)
                  onSubtreeKanban!(task.id)
                }}
              >
                {'\u25A6'} View subtree in Kanban
              </button>
            </div>
          </>
        )}
      </div>
    )
  }
}

// =============================================================================
// Search match function: match on title or ref
// =============================================================================

function searchMatch(node: { data: TreeNode }, term: string): boolean {
  const task = node.data.task
  const lower = term.toLowerCase()
  return task.title.toLowerCase().includes(lower) || task.ref.toLowerCase().includes(lower)
}

// =============================================================================
// TaskTree
// =============================================================================

interface TaskTreeProps {
  tasks: GobbyTask[]
  onSelectTask: (id: string) => void
  onReparent?: (taskId: string, newParentId: string | null) => void
  onSubtreeKanban?: (taskId: string) => void
}

/** Check if making childId a child of parentId would create a cycle. */
function wouldCreateCycle(childId: string, parentId: string, tasks: GobbyTask[]): boolean {
  const taskMap = new Map(tasks.map(t => [t.id, t]))
  let current = parentId
  while (current) {
    if (current === childId) return true
    const task = taskMap.get(current)
    if (!task?.parent_task_id) break
    current = task.parent_task_id
  }
  return false
}

export function TaskTree({ tasks, onSelectTask, onReparent, onSubtreeKanban }: TaskTreeProps) {
  const treeRef = useRef<TreeApi<TreeNode> | null>(null)
  const containerRef = useRef<HTMLDivElement | null>(null)
  const [treeHeight, setTreeHeight] = useState(560)
  const [hideClosed, setHideClosed] = useState(false)
  const [searchTerm, setSearchTerm] = useState('')
  const treeData = useMemo(() => buildTree(tasks, hideClosed), [tasks, hideClosed])
  const NodeRenderer = useMemo(() => makeTaskNode(searchTerm, onSubtreeKanban), [searchTerm, onSubtreeKanban])

  useEffect(() => {
    const container = containerRef.current
    if (!container) return
    const observer = new ResizeObserver(([entry]) => {
      // Subtract toolbar height (approx 40px) from container
      const available = entry.contentRect.height - 40
      if (available > 100) setTreeHeight(Math.round(available))
    })
    observer.observe(container)
    return () => observer.disconnect()
  }, [])

  const handleMove = useCallback(
    ({ dragIds, parentId }: { dragIds: string[]; parentId: string | null; index: number }) => {
      if (!onReparent) return
      for (const dragId of dragIds) {
        // Prevent cycles
        if (parentId && wouldCreateCycle(dragId, parentId, tasks)) continue
        // Don't re-parent to self
        if (parentId === dragId) continue
        onReparent(dragId, parentId)
      }
    },
    [onReparent, tasks]
  )

  return (
    <div className="task-tree-container" ref={containerRef}>
      <div className="task-tree-toolbar">
        <input
          type="text"
          className="task-tree-search"
          placeholder="Filter tree..."
          value={searchTerm}
          onChange={e => setSearchTerm(e.target.value)}
        />
        <button
          className="task-tree-toolbar-btn"
          onClick={() => treeRef.current?.openAll()}
          title="Expand all"
        >
          Expand all
        </button>
        <button
          className="task-tree-toolbar-btn"
          onClick={() => treeRef.current?.closeAll()}
          title="Collapse all"
        >
          Collapse all
        </button>
        <label className="task-tree-toolbar-check">
          <input
            type="checkbox"
            checked={hideClosed}
            onChange={e => setHideClosed(e.target.checked)}
          />
          Hide closed
        </label>
      </div>
      <Tree<TreeNode>
        ref={treeRef}
        data={treeData}
        width="100%"
        height={treeHeight}
        indent={24}
        rowHeight={34}
        openByDefault={false}
        searchTerm={searchTerm}
        searchMatch={searchMatch}
        onActivate={node => onSelectTask(node.data.id)}
        onMove={onReparent ? handleMove : undefined}
        disableDrag={!onReparent}
        disableDrop={!onReparent}
      >
        {NodeRenderer}
      </Tree>
    </div>
  )
}
