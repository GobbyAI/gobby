import { useMemo, useState } from 'react'
import type { GobbySession } from '../../types/sessions'
import { SourceIcon } from '../shared/SourceIcon'
import { formatRelativeTime } from '../../utils/formatTime'
import { getSessionDisplayTitle } from '../../lib/sessionTitle'
import { cn } from '../../lib/utils'

const SECTION_CLS =
  'mb-4 rounded-lg border border-[var(--border)] bg-[var(--bg-secondary)] p-4'
const HEADING_CLS =
  'mb-3 text-[length:var(--text-base)] font-semibold uppercase tracking-[0.03em] text-[var(--text-secondary)]'
const TREE_CLS = 'text-[length:var(--text-base)]'

const NODE_CLS =
  'mb-0.5 cursor-pointer rounded border border-transparent px-2 py-1.5 transition-[background-color,border-color] duration-150 hover:border-[var(--border)] hover:bg-[var(--bg-tertiary)]'
const NODE_CURRENT_CLS = 'border-[var(--accent)] bg-[var(--bg-tertiary)]'

const NODE_HEADER_CLS = 'flex items-center gap-1.5'
const NODE_TOGGLE_CLS = 'w-3 cursor-pointer text-center text-[length:var(--text-xs)] text-[var(--text-muted)]'
const NODE_LEAF_CLS = 'w-3 text-center text-[length:var(--text-xs)] text-[var(--text-muted)]'
const NODE_TITLE_CLS =
  'max-w-[250px] overflow-hidden text-ellipsis whitespace-nowrap font-medium text-[var(--text-primary)]'
const NODE_DEPTH_CLS =
  'rounded-sm bg-[var(--bg-primary)] px-1 font-[inherit] text-[length:var(--text-xs)] text-[var(--text-muted)]'

const NODE_META_CLS = 'ml-[1.125rem] mt-0.5 flex items-center gap-2'
const NODE_STATUS_CLS = 'text-[length:var(--text-xs)] font-medium capitalize'
const NODE_STATUS_BG: Record<string, string> = {
  active: 'text-[var(--color-success-foreground)]',
  archived: 'text-[var(--text-muted)]',
  expired: 'text-[var(--color-error)]',
}
const NODE_TIME_CLS = 'text-[length:var(--text-xs)] text-[var(--text-muted)]'

interface SessionLineageProps {
  session: GobbySession
  allSessions: GobbySession[]
  onSelectSession: (sessionId: string) => void
}

interface TreeNode {
  session: GobbySession
  children: TreeNode[]
}

function findRoot(sessionId: string, lookup: Map<string, GobbySession>): GobbySession | null {
  let current = lookup.get(sessionId)
  if (!current) return null

  const visited = new Set<string>()
  while (current.parent_session_id) {
    if (visited.has(current.id)) break
    visited.add(current.id)

    const parent = lookup.get(current.parent_session_id)
    if (!parent) break
    current = parent
  }
  return current
}

function buildTree(root: GobbySession, childrenMap: Map<string, GobbySession[]>, visited = new Set<string>()): TreeNode {
  visited.add(root.id)
  const children = (childrenMap.get(root.id) || [])
    .filter((child) => !visited.has(child.id))
    .sort((a, b) => new Date(a.created_at).getTime() - new Date(b.created_at).getTime())
    .map((child) => buildTree(child, childrenMap, visited))
  return { session: root, children }
}

function countNodes(node: TreeNode): number {
  return 1 + node.children.reduce((sum, child) => sum + countNodes(child), 0)
}

function TreeNodeView({
  node,
  currentSessionId,
  onSelect,
  depth = 0,
}: {
  node: TreeNode
  currentSessionId: string
  onSelect: (id: string) => void
  depth?: number
}) {
  const [expanded, setExpanded] = useState(true)
  const isCurrent = node.session.id === currentSessionId
  const hasChildren = node.children.length > 0
  const s = node.session

  return (
    <div>
      <div
        className={cn(NODE_CLS, isCurrent && NODE_CURRENT_CLS)}
        style={{ marginLeft: depth * 20 }}
        onClick={() => onSelect(s.id)}
      >
        <div className={NODE_HEADER_CLS}>
          {hasChildren && (
            <span
              className={NODE_TOGGLE_CLS}
              onClick={(e) => { e.stopPropagation(); setExpanded(!expanded) }}
            >
              {expanded ? '▼' : '▶'}
            </span>
          )}
          {!hasChildren && <span className={NODE_LEAF_CLS}>•</span>}
          <SourceIcon source={s.source} size={12} />
          <span className={NODE_TITLE_CLS}>
            {getSessionDisplayTitle(s)}
          </span>
          {s.agent_depth > 0 && (
            <span className={NODE_DEPTH_CLS}>L{s.agent_depth}</span>
          )}
        </div>
        <div className={NODE_META_CLS}>
          <span className={cn(NODE_STATUS_CLS, NODE_STATUS_BG[s.status] ?? '')}>
            {s.status}
          </span>
          <span className={NODE_TIME_CLS}>{formatRelativeTime(s.created_at)}</span>
        </div>
      </div>
      {expanded && hasChildren && (
        <div>
          {node.children.map((child) => (
            <TreeNodeView
              key={child.session.id}
              node={child}
              currentSessionId={currentSessionId}
              onSelect={onSelect}
              depth={depth + 1}
            />
          ))}
        </div>
      )}
    </div>
  )
}

export function SessionLineage({ session, allSessions, onSelectSession }: SessionLineageProps) {
  const tree = useMemo(() => {
    if (allSessions.length === 0) return null

    const lookup = new Map(allSessions.map((s) => [s.id, s]))
    const root = findRoot(session.id, lookup)
    if (!root) return null

    const childrenMap = new Map<string, GobbySession[]>()
    for (const s of allSessions) {
      if (s.parent_session_id) {
        const siblings = childrenMap.get(s.parent_session_id) || []
        siblings.push(s)
        childrenMap.set(s.parent_session_id, siblings)
      }
    }

    const treeRoot = buildTree(root, childrenMap)
    return countNodes(treeRoot) > 1 ? treeRoot : null
  }, [session.id, allSessions])

  if (!tree) return null

  return (
    <div className={SECTION_CLS}>
      <h3 className={HEADING_CLS}>Session Lineage</h3>
      <div className={TREE_CLS}>
        <TreeNodeView
          node={tree}
          currentSessionId={session.id}
          onSelect={onSelectSession}
        />
      </div>
    </div>
  )
}
