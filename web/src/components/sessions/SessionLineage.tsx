import { useMemo, useState } from 'react'
import type { GobbySession } from '../../types/sessions'
import { SourceIcon } from '../shared/SourceIcon'
import { formatRelativeTime } from '../../utils/formatTime'
import { getSessionDisplayTitle } from '../../lib/sessionTitle'

interface SessionLineageProps {
  /** The currently selected session. */
  session: GobbySession
  /** All available sessions to search for relatives. */
  allSessions: GobbySession[]
  /** Callback when a session node is clicked. */
  onSelectSession: (sessionId: string) => void
}

interface TreeNode {
  session: GobbySession
  children: TreeNode[]
}

/** Walk up to find the root ancestor of a session. */
function findRoot(sessionId: string, lookup: Map<string, GobbySession>): GobbySession | null {
  let current = lookup.get(sessionId)
  if (!current) return null

  // Guard against cycles with a visited set
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

/** Build a tree from a root session. */
function buildTree(root: GobbySession, childrenMap: Map<string, GobbySession[]>, visited = new Set<string>()): TreeNode {
  visited.add(root.id)
  const children = (childrenMap.get(root.id) || [])
    .filter((child) => !visited.has(child.id))
    .sort((a, b) => new Date(a.created_at).getTime() - new Date(b.created_at).getTime())
    .map((child) => buildTree(child, childrenMap, visited))
  return { session: root, children }
}

/** Count total nodes in a tree. */
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
    <div className="lineage-node-wrapper">
      <div
        className={`lineage-node${isCurrent ? ' lineage-node-current' : ''}`}
        style={{ marginLeft: depth * 20 }}
        onClick={() => onSelect(s.id)}
      >
        <div className="lineage-node-header">
          {hasChildren && (
            <span
              className="lineage-node-toggle"
              onClick={(e) => { e.stopPropagation(); setExpanded(!expanded) }}
            >
              {expanded ? '\u25bc' : '\u25b6'}
            </span>
          )}
          {!hasChildren && <span className="lineage-node-leaf">{'\u2022'}</span>}
          <SourceIcon source={s.source} size={12} />
          <span className="lineage-node-title">
            {getSessionDisplayTitle(s)}
          </span>
          {s.agent_depth > 0 && (
            <span className="lineage-node-depth">L{s.agent_depth}</span>
          )}
        </div>
        <div className="lineage-node-meta">
          <span className={`lineage-node-status lineage-node-status-${s.status}`}>
            {s.status}
          </span>
          <span className="lineage-node-time">{formatRelativeTime(s.created_at)}</span>
        </div>
      </div>
      {expanded && hasChildren && (
        <div className="lineage-children">
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

    // Build children map
    const childrenMap = new Map<string, GobbySession[]>()
    for (const s of allSessions) {
      if (s.parent_session_id) {
        const siblings = childrenMap.get(s.parent_session_id) || []
        siblings.push(s)
        childrenMap.set(s.parent_session_id, siblings)
      }
    }

    const treeRoot = buildTree(root, childrenMap)
    // Only show lineage if there's more than one node (i.e., actual relationships)
    return countNodes(treeRoot) > 1 ? treeRoot : null
  }, [session.id, allSessions])

  if (!tree) return null

  return (
    <div className="session-lineage">
      <h3>Session Lineage</h3>
      <div className="lineage-tree">
        <TreeNodeView
          node={tree}
          currentSessionId={session.id}
          onSelect={onSelectSession}
        />
      </div>
    </div>
  )
}
