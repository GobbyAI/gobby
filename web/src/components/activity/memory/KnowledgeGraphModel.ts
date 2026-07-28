// Pure data layer for the memory knowledge graph — kept out of
// KnowledgeGraph.tsx so the component file only exports components
// (react-refresh) and tests can exercise color/build logic directly.
import { escapeHtml, resolveCssVar } from '../../../lib/utils'
import type { KnowledgeGraphData, KnowledgeEntity, KnowledgeRelationship } from '../../../hooks/useMemory'

export function numericId(id: unknown): number {
  if (typeof id === 'number') return id
  const s = String(id)
  let h = 5381
  for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) | 0
  return Math.abs(h)
}

export interface GraphNode {
  id: string
  name: string
  type: string
  entity: KnowledgeEntity
  color: string
  val: number // node size
}

export interface GraphLink {
  source: string
  target: string
  type: string
  color: string
}

// The canonical extraction vocabulary (prompts/memory/extract_entities.md) is
// exactly these seven types. Each gets a dedicated --kg-entity-* token from
// tokens.css (deutan-safe: lightness-first separation, hues on the preserved
// blue↔yellow axis, no red/green pairs). Off-vocabulary types — legacy graph
// data or extraction drift — fall back to muted gray.
export const CANONICAL_ENTITY_TYPES = [
  'person',
  'organization',
  'tool',
  'project',
  'concept',
  'location',
  'version',
] as const

const ENTITY_TYPE_COLOR_VARS: Record<string, string> = {
  person: '--kg-entity-person',
  organization: '--kg-entity-organization',
  tool: '--kg-entity-tool',
  project: '--kg-entity-project',
  concept: '--kg-entity-concept',
  location: '--kg-entity-location',
  version: '--kg-entity-version',
}

export function entityColorVar(type: string): string {
  return ENTITY_TYPE_COLOR_VARS[type.toLowerCase()] ?? '--text-muted'
}

export function getEntityColor(type: string): string {
  return resolveCssVar(entityColorVar(type))
}

export function getEntityColorCss(type: string): string {
  return `var(${entityColorVar(type)})`
}

const EDGE_COLOR_VARS = [
  '--color-info',
  '--color-success-foreground',
  '--color-warning-foreground',
  '--color-error',
  '--color-review',
  '--accent',
] as const

function hashString(str: string): number {
  let hash = 0
  for (let i = 0; i < str.length; i++) {
    hash = str.charCodeAt(i) + ((hash << 5) - hash)
  }
  return Math.abs(hash)
}

// Resolved (not `var()` literal) because the value feeds three.js materials,
// which parse concrete colors only — a CSS var literal renders black.
export function edgeColor(relType: string): string {
  const colorVar = EDGE_COLOR_VARS[hashString(relType) % EDGE_COLOR_VARS.length]
  return resolveCssVar(colorVar)
}

export function mergeGraphData(
  existing: KnowledgeGraphData,
  incoming: KnowledgeGraphData
): KnowledgeGraphData {
  const entityMap = new Map(existing.entities.map(e => [e.entity_key, e]))
  for (const e of incoming.entities) {
    if (!entityMap.has(e.entity_key)) entityMap.set(e.entity_key, e)
  }

  const edgeKey = (r: KnowledgeRelationship) => `${r.source_key}|${r.type}|${r.target_key}`
  const edgeSet = new Set(existing.relationships.map(edgeKey))
  const merged = [...existing.relationships]
  for (const r of incoming.relationships) {
    if (!edgeSet.has(edgeKey(r))) {
      edgeSet.add(edgeKey(r))
      merged.push(r)
    }
  }

  return { entities: [...entityMap.values()], relationships: merged }
}

export interface NodeConnection {
  name: string
  relation: string
  outgoing: boolean
}

// force-graph mutates link source/target from id strings to node objects once
// the simulation ingests them, so resolve either shape.
function linkEndpointId(endpoint: unknown): string {
  if (typeof endpoint === 'object' && endpoint !== null && 'id' in endpoint) {
    return String((endpoint as { id: unknown }).id)
  }
  return String(endpoint)
}

/** `DEPENDS_ON` → `depends on` — relation types read as sentences, not enums. */
export function humanizeRelation(type: string): string {
  return type.replace(/_/g, ' ').toLowerCase()
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const HEX_ID_RE = /^[0-9a-f]{16,}$/i

/** True for machine identifiers (UUIDs, long hex) that mean nothing to humans. */
export function isOpaqueIdentifier(name: string): boolean {
  const trimmed = name.trim()
  return UUID_RE.test(trimmed) || HEX_ID_RE.test(trimmed)
}

/** Index every node's named connections from the loaded links. */
export function buildNeighborIndex(
  nodes: Array<{ id: string; name: string }>,
  links: Array<{ source: unknown; target: unknown; type: string }>
): Map<string, NodeConnection[]> {
  const nameById = new Map(nodes.map(n => [n.id, n.name]))
  const index = new Map<string, NodeConnection[]>()
  const push = (id: string, connection: NodeConnection) => {
    const list = index.get(id)
    if (list) list.push(connection)
    else index.set(id, [connection])
  }
  for (const link of links) {
    const sourceId = linkEndpointId(link.source)
    const targetId = linkEndpointId(link.target)
    const sourceName = nameById.get(sourceId)
    const targetName = nameById.get(targetId)
    if (sourceName === undefined || targetName === undefined) continue
    const relation = humanizeRelation(link.type)
    push(sourceId, { name: targetName, relation, outgoing: true })
    push(targetId, { name: sourceName, relation, outgoing: false })
  }
  return index
}

const CARD_MAX_CONNECTIONS = 4

/**
 * Hover card for a graph node: display name (never a bare UUID), typed color
 * label, the backing memory snippet, and connections as readable sentences.
 * Modeled on Zep/Graphiti (entity summary + facts-as-sentences) and Mem0's
 * graph view (memory text as the payload behind each entity).
 */
export function buildNodeCardHtml(entity: KnowledgeEntity, connections: NodeConnection[]): string {
  const opaqueName = isOpaqueIdentifier(entity.name)
  const title = opaqueName ? `Unlabeled ${entity.entity_type.toLowerCase()}` : entity.name
  const shortId = opaqueName ? entity.name.trim().slice(0, 8) : null

  const header =
    '<div style="display:flex;align-items:baseline;gap:8px;justify-content:space-between">' +
    `<span style="font-weight:600;font-size:var(--text-md);color:var(--text-primary)">${escapeHtml(title)}</span>` +
    `<span style="color:${getEntityColorCss(entity.entity_type)};text-transform:uppercase;font-size:var(--text-2xs);letter-spacing:0.05em">${escapeHtml(entity.entity_type)}</span>` +
    '</div>'

  const idRow = shortId
    ? `<div style="margin-top:2px;font-family:var(--font-mono);font-size:var(--text-2xs);color:var(--text-muted)">${escapeHtml(shortId)}…</div>`
    : ''

  const preview = entity.memory_preview
    ? `<div style="margin-top:6px;color:var(--text-secondary)">“${escapeHtml(entity.memory_preview)}”</div>`
    : ''

  const shown = connections.slice(0, CARD_MAX_CONNECTIONS)
  const overflow = connections.length - shown.length
  const connectionRows = shown
    .map(
      c =>
        '<div style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' +
        `<span style="color:var(--text-muted)">${c.outgoing ? '→' : '←'} ${escapeHtml(c.relation)} </span>` +
        `<span style="color:var(--text-primary)">${escapeHtml(c.name)}</span>` +
        '</div>'
    )
    .join('')
  const overflowRow =
    overflow > 0
      ? `<div style="color:var(--text-muted)">+${overflow} more connection${overflow === 1 ? '' : 's'}</div>`
      : ''
  const connectionsBlock = connectionRows
    ? `<div style="margin-top:8px;display:flex;flex-direction:column;gap:2px">${connectionRows}${overflowRow}</div>`
    : ''

  const memoryCount = entity.memory_count
  const footer =
    typeof memoryCount === 'number'
      ? `<div style="margin-top:8px;font-size:var(--text-2xs);color:var(--text-muted)">${memoryCount} ${memoryCount === 1 ? 'memory' : 'memories'} · ${connections.length} connection${connections.length === 1 ? '' : 's'}</div>`
      : ''

  return (
    '<div style="min-width:200px;max-width:280px;text-align:left;font-family:var(--font-sans);font-size:var(--text-sm);line-height:1.5;background:var(--bg-primary);border:1px solid var(--border);border-radius:8px;padding:10px 12px">' +
    header +
    idRow +
    preview +
    connectionsBlock +
    footer +
    '</div>'
  )
}

export function buildForceData(data: KnowledgeGraphData): { nodes: GraphNode[]; links: GraphLink[] } {
  const entityKeys = new Set(data.entities.map(e => e.entity_key))

  const nodes: GraphNode[] = data.entities.map(e => ({
    id: e.entity_key,
    name: e.name,
    type: e.entity_type,
    entity: e,
    color: getEntityColor(e.entity_type),
    val: 2,
  }))

  const links: GraphLink[] = data.relationships
    .filter(r => entityKeys.has(r.source_key) && entityKeys.has(r.target_key))
    .map(r => ({
      source: r.source_key,
      target: r.target_key,
      type: r.type,
      color: edgeColor(r.type),
    }))

  return { nodes, links }
}
