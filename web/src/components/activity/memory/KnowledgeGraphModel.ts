// Pure data layer for the memory knowledge graph — kept out of
// KnowledgeGraph.tsx so the component file only exports components
// (react-refresh) and tests can exercise color/build logic directly.
import { resolveCssVar } from '../../../lib/utils'
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
