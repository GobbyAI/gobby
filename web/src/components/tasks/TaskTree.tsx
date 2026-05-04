import { useMemo, useRef, useState, useCallback, useEffect, useLayoutEffect } from 'react'
import { Tree, TreeApi, NodeRendererProps } from 'react-arborist'
import type { GobbyTask } from '../../hooks/useTasks'
import { StatusDot, PriorityBadge, TypeBadge } from './TaskBadges'
import { TaskStatusStrip } from './TaskStatusStrip'
import { getTaskDisplayState } from '../../lib/taskState'

interface TreeNode {
  id: string
  task: GobbyTask
  children: TreeNode[]
}

const CONTAINER_CLS = 'flex-1 overflow-hidden'
const TOOLBAR_CLS = 'mb-1 flex items-center gap-2 border-b border-[var(--border)] px-2 py-[0.4rem]'
const TOOLBAR_BTN_CLS =
  'cursor-pointer rounded border border-[var(--border)] bg-[var(--bg-tertiary)] px-2 py-[0.2rem] font-[inherit] text-[length:calc(var(--font-size-base)*0.7)] text-[var(--text-secondary)] transition-colors duration-150 hover:bg-[var(--border)] hover:text-[var(--text-primary)]'
const TOOLBAR_CHECK_CLS =
  'ml-auto flex cursor-pointer items-center gap-[0.3rem] text-[length:calc(var(--font-size-base)*0.75)] text-[var(--text-secondary)] [&_input]:cursor-pointer'
const SEARCH_CLS =
  'w-40 rounded border border-[var(--border)] bg-[var(--bg-tertiary)] px-2 py-1 font-[inherit] text-[length:calc(var(--font-size-base)*0.75)] text-[var(--text-primary)] outline-none placeholder:text-[var(--text-muted)] focus:border-[var(--accent)]'

const NODE_CLS =
  'flex h-full cursor-pointer items-center gap-[0.4rem] px-2 text-[length:var(--font-size-base)] transition-colors duration-100 hover:bg-[var(--bg-tertiary)]'
const NODE_SELECTED_CLS = 'bg-[color-mix(in_srgb,var(--color-info)_8%,transparent)]'
const TOGGLE_CLS =
  'inline-flex h-4 w-4 shrink-0 cursor-pointer items-center justify-center border-none bg-transparent p-0 text-[length:calc(var(--font-size-base)*0.75)] text-[var(--text-muted)]'
const TOGGLE_LEAF_CLS = 'cursor-default'
const REF_CLS = 'shrink-0 font-[inherit] text-[length:inherit] text-[var(--text-muted)]'
const TITLE_CLS =
  'flex-1 overflow-hidden text-ellipsis whitespace-nowrap text-[length:inherit] text-[var(--text-primary)]'
const HIGHLIGHT_CLS =
  'rounded-sm bg-[color-mix(in_srgb,var(--color-warning-foreground)_30%,transparent)] px-px text-[inherit]'

const CTX_BACKDROP_CLS = 'fixed inset-0 z-[999]'
const CTX_MENU_CLS =
  'z-[1000] min-w-[180px] rounded-md border border-[var(--border)] bg-[var(--bg-secondary)] p-1 shadow-[var(--shadow-md)]'
const CTX_ITEM_CLS =
  'block w-full cursor-pointer rounded border-none bg-transparent px-2.5 py-1.5 text-left font-[var(--font-sans)] text-[length:calc(var(--font-size-base)*0.72)] text-[var(--text-primary)] transition-colors duration-100 hover:bg-[var(--bg-tertiary)]'
const DEFAULT_CTX_MENU_SIZE = { width: 220, height: 44 }
const CTX_MENU_MARGIN = 8

function clampContextMenuPosition(
  x: number,
  y: number,
  size: { width: number; height: number } = DEFAULT_CTX_MENU_SIZE
) {
  if (typeof window === 'undefined') return { x, y }
  return {
    x: Math.max(CTX_MENU_MARGIN, Math.min(x, window.innerWidth - size.width - CTX_MENU_MARGIN)),
    y: Math.max(CTX_MENU_MARGIN, Math.min(y, window.innerHeight - size.height - CTX_MENU_MARGIN)),
  }
}

function buildTree(tasks: GobbyTask[], hideClosed: boolean): TreeNode[] {
  const filtered = hideClosed ? tasks.filter(t => getTaskDisplayState(t) !== 'closed') : tasks
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

function HighlightText({ text, search }: { text: string; search: string }) {
  if (!search) return <>{text}</>
  const idx = text.toLowerCase().indexOf(search.toLowerCase())
  if (idx === -1) return <>{text}</>
  return (
    <>
      {text.slice(0, idx)}
      <mark className={HIGHLIGHT_CLS}>{text.slice(idx, idx + search.length)}</mark>
      {text.slice(idx + search.length)}
    </>
  )
}

interface TaskNodeProps extends NodeRendererProps<TreeNode> {
  searchTerm: string
  onSubtreeKanban?: (taskId: string) => void
}

function TaskNode({ node, style, dragHandle, searchTerm, onSubtreeKanban }: TaskNodeProps) {
  const task = node.data.task
  const [ctxMenuAnchor, setCtxMenuAnchor] = useState<{ x: number; y: number } | null>(null)
  const [ctxMenuSize, setCtxMenuSize] = useState(DEFAULT_CTX_MENU_SIZE)
  const menuRef = useRef<HTMLDivElement | null>(null)
  const ctxMenuPosition = useMemo(
    () => (
      ctxMenuAnchor
        ? clampContextMenuPosition(ctxMenuAnchor.x, ctxMenuAnchor.y, ctxMenuSize)
        : null
    ),
    [ctxMenuAnchor, ctxMenuSize]
  )

  const handleContextMenu = useCallback((e: React.MouseEvent) => {
    if (!onSubtreeKanban || !node.isInternal) return
    e.preventDefault()
    e.stopPropagation()
    setCtxMenuAnchor({ x: e.clientX, y: e.clientY })
  }, [node.isInternal, onSubtreeKanban])

  const handleSubtreeKanban = useCallback((e: React.MouseEvent) => {
    e.stopPropagation()
    setCtxMenuAnchor(null)
    onSubtreeKanban?.(task.id)
  }, [onSubtreeKanban, task.id])

  const handleNodeActivate = useCallback(() => node.activate(), [node])
  const handleToggle = useCallback((e: React.MouseEvent) => {
    e.stopPropagation()
    node.toggle()
  }, [node])
  const handleBackdropClick = useCallback(() => setCtxMenuAnchor(null), [])

  useLayoutEffect(() => {
    if (!ctxMenuAnchor) return
    const rect = menuRef.current?.getBoundingClientRect()
    if (!rect) return
    setCtxMenuSize(prev => {
      if (prev.width === rect.width && prev.height === rect.height) return prev
      return { width: rect.width, height: rect.height }
    })
  }, [ctxMenuAnchor])

  useEffect(() => {
    if (!ctxMenuAnchor) return
    requestAnimationFrame(() => {
      menuRef.current?.querySelector<HTMLElement>('[role="menuitem"]')?.focus()
    })
  }, [ctxMenuAnchor])

  const handleMenuKeyDown = useCallback((e: React.KeyboardEvent<HTMLDivElement>) => {
    if (e.key === 'Escape') {
      e.preventDefault()
      setCtxMenuAnchor(null)
      return
    }
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(e.key)) return
    const items = Array.from(
      menuRef.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]') ?? []
    )
    if (items.length === 0) return
    e.preventDefault()
    const activeIndex = items.indexOf(document.activeElement as HTMLButtonElement)
    const nextIndex =
      e.key === 'Home' ? 0 :
      e.key === 'End' ? items.length - 1 :
      e.key === 'ArrowUp' ? (activeIndex <= 0 ? items.length - 1 : activeIndex - 1) :
      (activeIndex + 1) % items.length
    items[nextIndex]?.focus()
  }, [])

  return (
    <div
      ref={dragHandle}
      style={style}
      className={node.isSelected ? `${NODE_CLS} ${NODE_SELECTED_CLS}` : NODE_CLS}
      onClick={handleNodeActivate}
      onContextMenu={handleContextMenu}
    >
      {node.isInternal ? (
        <button
          type="button"
          className={TOGGLE_CLS}
          onClick={handleToggle}
        >
          {node.isOpen ? '▾' : '▸'}
        </button>
      ) : (
        <span className={`${TOGGLE_CLS} ${TOGGLE_LEAF_CLS}`} />
      )}
      <StatusDot task={task} />
      <span className={REF_CLS}>{task.ref}</span>
      <span className={TITLE_CLS}>
        <HighlightText text={task.title} search={searchTerm} />
      </span>
      <TypeBadge type={task.task_type} />
      <PriorityBadge priority={task.priority} />
      <TaskStatusStrip task={task} compact />

      {ctxMenuPosition && (
        <>
          <div className={CTX_BACKDROP_CLS} onClick={handleBackdropClick} />
          <div
            ref={menuRef}
            className={CTX_MENU_CLS}
            role="menu"
            aria-label="Task actions"
            style={{ position: 'fixed', left: ctxMenuPosition.x, top: ctxMenuPosition.y }}
            onKeyDown={handleMenuKeyDown}
          >
            <button
              type="button"
              className={CTX_ITEM_CLS}
              role="menuitem"
              onClick={handleSubtreeKanban}
            >
              {'▦'} View subtree in Kanban
            </button>
          </div>
        </>
      )}
    </div>
  )
}

function searchMatch(node: { data: TreeNode }, term: string): boolean {
  const task = node.data.task
  const lower = term.toLowerCase()
  return task.title.toLowerCase().includes(lower) || task.ref.toLowerCase().includes(lower)
}

interface TaskTreeProps {
  tasks: GobbyTask[]
  onSelectTask: (id: string) => void
  onReparent?: (taskId: string, newParentId: string | null) => void
  onSubtreeKanban?: (taskId: string) => void
}

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
  const NodeRenderer = useCallback(
    (props: NodeRendererProps<TreeNode>) => (
      <TaskNode {...props} searchTerm={searchTerm} onSubtreeKanban={onSubtreeKanban} />
    ),
    [searchTerm, onSubtreeKanban]
  )

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

  const handleMove = useCallback(
    ({ dragIds, parentId }: { dragIds: string[]; parentId: string | null; index: number }) => {
      if (!onReparent) return
      for (const dragId of dragIds) {
        if (parentId && wouldCreateCycle(dragId, parentId, tasks)) continue
        if (parentId === dragId) continue
        onReparent(dragId, parentId)
      }
    },
    [onReparent, tasks]
  )

  return (
    <div className={CONTAINER_CLS} ref={containerRef}>
      <div className={TOOLBAR_CLS}>
        <input
          type="text"
          className={SEARCH_CLS}
          placeholder="Filter tree..."
          value={searchTerm}
          onChange={e => setSearchTerm(e.target.value)}
        />
        <button
          className={TOOLBAR_BTN_CLS}
          onClick={() => treeRef.current?.openAll()}
          title="Expand all"
        >
          Expand all
        </button>
        <button
          className={TOOLBAR_BTN_CLS}
          onClick={() => treeRef.current?.closeAll()}
          title="Collapse all"
        >
          Collapse all
        </button>
        <label className={TOOLBAR_CHECK_CLS}>
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
