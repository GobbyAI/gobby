import { useState, useEffect, useRef, useMemo, useCallback } from 'react'
import ForceGraph3D from 'react-force-graph-3d'
import SpriteText from 'three-spritetext'
import { SphereGeometry, MeshLambertMaterial, Mesh } from 'three'
import { IS_MOBILE, IS_IOS } from '../../utils/platform'
import { resolveCssVar, cn } from '../../lib/utils'
import type { KnowledgeGraphData, KnowledgeEntity, KnowledgeRelationship } from '../../hooks/useMemory'

const CONTAINER_CLS = 'relative flex min-h-0 flex-1 overflow-hidden rounded-lg border border-[var(--border)] bg-[var(--bg-primary)]'
const EMPTY_CLS = 'flex h-full min-h-[300px] flex-col items-center justify-center gap-2 text-[var(--text-muted)]'
const OVERLAY_STACK_CLS = 'absolute left-2 top-2 z-10 flex flex-col items-start gap-1.5'
const INFO_CLS = 'rounded-md border border-[var(--border)] bg-[var(--bg-secondary)] px-2 py-1 text-[length:var(--text-xs)] text-[var(--text-muted)]'
const LEGEND_CLS = 'absolute bottom-2 left-2 z-10 flex items-center gap-2.5 rounded-md border border-[var(--border)] bg-[var(--bg-secondary)] px-2.5 py-1.5 text-[length:var(--text-xs)] text-[var(--text-muted)]'
const CONTROLS_CLS = 'absolute right-2 top-2 z-10 flex items-center gap-1 rounded-md border border-[var(--border)] bg-[var(--bg-secondary)] p-[3px]'
const CTRL_BTN_CLS = 'flex h-7 w-7 cursor-pointer items-center justify-center rounded border-0 bg-transparent text-[length:var(--text-base)] text-[var(--text-secondary)] transition-colors duration-100 hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)] pointer-coarse:h-11 pointer-coarse:w-11'
const CTRL_BTN_ACTIVE_CLS = 'bg-[var(--accent)] text-[var(--bg-primary)] hover:bg-[var(--accent)] hover:text-[var(--bg-primary)]'
const PHYSICS_CLS = 'absolute right-2 top-[42px] z-10 flex min-w-[200px] flex-col gap-1.5 rounded-md border border-[var(--border)] bg-[var(--bg-secondary)] px-2.5 py-2'
const PHYSICS_ROW_CLS = 'flex cursor-default items-center gap-1.5'
const PHYSICS_LABEL_CLS = 'min-w-[56px] text-[length:var(--text-xs)] text-[var(--text-secondary)]'
const PHYSICS_VALUE_CLS = 'min-w-[36px] text-right font-[inherit] text-[length:var(--text-2xs)] tabular-nums text-[var(--text-muted)]'
const PHYSICS_SLIDER_CLS = 'h-1 flex-1 cursor-pointer accent-[var(--accent)]'
const PHYSICS_RESET_CLS = 'mt-0.5 cursor-pointer self-end rounded border border-[var(--border)] bg-[var(--bg-tertiary)] px-2 py-0.5 text-[length:var(--text-xs)] text-[var(--text-secondary)] hover:bg-[var(--bg-primary)] hover:text-[var(--text-primary)] pointer-coarse:min-h-11 pointer-coarse:px-3'
const SEARCH_INPUT_CLS = 'rounded border border-[var(--border)] bg-[var(--bg-primary)] px-2 py-1 text-[length:var(--text-sm)] text-[var(--text-primary)] outline-none w-[150px] font-mono pointer-coarse:min-h-11 pointer-coarse:px-2.5 pointer-coarse:py-2 pointer-coarse:text-[length:var(--text-md)]'

interface KnowledgeGraphProps {
  fetchKnowledgeGraph: (limit?: number) => Promise<KnowledgeGraphData | null>
  fetchEntityNeighbors: (entityKey: string) => Promise<KnowledgeGraphData | null>
  limit?: number
  onError?: () => void
}

function numericId(id: unknown): number {
  if (typeof id === 'number') return id
  const s = String(id)
  let h = 5381
  for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) | 0
  return Math.abs(h)
}

interface GraphNode {
  id: string
  name: string
  type: string
  entity: KnowledgeEntity
  color: string
  val: number // node size
}

interface GraphLink {
  source: string
  target: string
  type: string
  color: string
}

// Categorical entity types collapse onto the deutan-safe semantic palette.
// Multiple types intentionally share a token; the legend disambiguates by name.
const ENTITY_TYPE_COLOR_VARS: Record<string, string> = {
  function: '--color-info',
  file: '--color-success-foreground',
  class: '--text-muted',
  concept: '--color-warning-foreground',
  hook: '--color-error',
  module: '--color-review',
  config: '--color-warning-foreground',
  test: '--text-muted',
  route: '--color-info',
  component: '--accent',
}

function entityColorVar(type: string): string {
  return ENTITY_TYPE_COLOR_VARS[type.toLowerCase()] ?? '--text-muted'
}

function getEntityColor(type: string): string {
  return resolveCssVar(entityColorVar(type))
}

function getEntityColorCss(type: string): string {
  return `var(${entityColorVar(type)})`
}

function hashToHue(str: string): number {
  let hash = 0
  for (let i = 0; i < str.length; i++) {
    hash = str.charCodeAt(i) + ((hash << 5) - hash)
  }
  return Math.abs(hash) % 360
}

function edgeColor(relType: string): string {
  // Open-ended relationship types — hashed hue keeps adjacent edges distinguishable
  // while staying mid-saturation/mid-lightness to harmonize with the design palette.
  const hue = hashToHue(relType)
  return `hsl(${hue}, 45%, 50%)`
}

function mergeGraphData(
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

function buildForceData(data: KnowledgeGraphData): { nodes: GraphNode[]; links: GraphLink[] } {
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

const DEFAULT_CHARGE = -120
const DEFAULT_LINK_DIST = 60
const DEFAULT_CENTER = 0.05

export function KnowledgeGraph({ fetchKnowledgeGraph, fetchEntityNeighbors, limit, onError }: KnowledgeGraphProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const fgRef = useRef<any>(null)  
  const [graphData, setGraphData] = useState<KnowledgeGraphData | null>(null)
  const [loading, setLoading] = useState(true)
  const [searchQuery, setSearchQuery] = useState('')
  const [expandingNode, setExpandingNode] = useState<string | null>(null)
  const [dimensions, setDimensions] = useState({ width: 800, height: 600 })
  const [animateIdle, setAnimateIdle] = useState(() => {
    try { return localStorage.getItem('gobby-kg-animate') === 'true' } catch { return false }
  })
  const [showPhysics, setShowPhysics] = useState(false)
  const [charge, setCharge] = useState(() => {
    try { const v = localStorage.getItem('gobby-kg-charge'); const n = Number(v); return v && Number.isFinite(n) ? n : DEFAULT_CHARGE } catch { return DEFAULT_CHARGE }
  })
  const [linkDist, setLinkDist] = useState(() => {
    try { const v = localStorage.getItem('gobby-kg-link-dist'); const n = Number(v); return v && Number.isFinite(n) ? n : DEFAULT_LINK_DIST } catch { return DEFAULT_LINK_DIST }
  })
  const [centerStrength, setCenterStrength] = useState(() => {
    try { const v = localStorage.getItem('gobby-kg-center'); const n = Number(v); return v && Number.isFinite(n) ? n : DEFAULT_CENTER } catch { return DEFAULT_CENTER }
  })

  // Catch async WebGL/Three.js errors that escape React error boundaries
  const onErrorRef = useRef(onError)
  useEffect(() => {
    onErrorRef.current = onError
  }, [onError])
  useEffect(() => {
    const handleError = (e: ErrorEvent) => {
      const msg = (e.message || '').toLowerCase()
      if (msg.includes('webgl') || msg.includes('three') || msg.includes('context lost') || msg.includes('texture') || msg.includes('gl_')) {
        console.error('[KnowledgeGraph] WebGL/Three.js error caught:', e.message)
        e.preventDefault()
        onErrorRef.current?.()
      }
    }
    const handleRejection = (e: PromiseRejectionEvent) => {
      const msg = String(e.reason || '').toLowerCase()
      if (msg.includes('webgl') || msg.includes('three') || msg.includes('context lost')) {
        console.error('[KnowledgeGraph] Unhandled WebGL rejection:', e.reason)
        e.preventDefault()
        onErrorRef.current?.()
      }
    }
    window.addEventListener('error', handleError)
    window.addEventListener('unhandledrejection', handleRejection)
    return () => {
      window.removeEventListener('error', handleError)
      window.removeEventListener('unhandledrejection', handleRejection)
    }
  }, [])

  // Handle WebGL context lost on the canvas element
  useEffect(() => {
    const container = containerRef.current
    if (!container) return
    const canvas = container.querySelector('canvas')
    if (!canvas) return
    const handleContextLost = (e: Event) => {
      e.preventDefault()
      console.error('[KnowledgeGraph] WebGL context lost')
      onErrorRef.current?.()
    }
    canvas.addEventListener('webglcontextlost', handleContextLost)
    return () => canvas.removeEventListener('webglcontextlost', handleContextLost)
  }, [loading]) // re-run after loading completes since canvas only exists after render

  // Track container size
  useEffect(() => {
    const container = containerRef.current
    if (!container) return

    const observer = new ResizeObserver(entries => {
      for (const entry of entries) {
        setDimensions({
          width: entry.contentRect.width,
          height: entry.contentRect.height,
        })
      }
    })
    observer.observe(container)
    return () => observer.disconnect()
  }, [])

  // Persist animation toggle
  const toggleAnimate = useCallback(() => {
    setAnimateIdle(prev => {
      const next = !prev
      try { localStorage.setItem('gobby-kg-animate', String(next)) } catch { /* noop */ }
      return next
    })
  }, [])

  // Manual auto-rotation (TrackballControls lacks autoRotate)
  useEffect(() => {
    if (!animateIdle) return
    let raf: number
    const rotate = () => {
      const fg = fgRef.current
      if (fg) {
        const pos = fg.cameraPosition()
        const dist = Math.sqrt(pos.x * pos.x + pos.z * pos.z)
        const angle = Math.atan2(pos.z, pos.x) + 0.002
        fg.cameraPosition({ x: dist * Math.cos(angle), y: pos.y, z: dist * Math.sin(angle) })
      }
      raf = requestAnimationFrame(rotate)
    }
    raf = requestAnimationFrame(rotate)
    return () => cancelAnimationFrame(raf)
  }, [animateIdle])

  // Initial data fetch (refetches when limit changes)
  useEffect(() => {
    let cancelled = false
    setLoading(true)
    fetchKnowledgeGraph(limit).then(data => {
      if (!cancelled && data) setGraphData(data)
      if (!cancelled) setLoading(false)
    })
    return () => { cancelled = true }
  }, [fetchKnowledgeGraph, limit])

  // Build force graph data
  const forceData = useMemo(() => {
    if (!graphData) return { nodes: [], links: [] }
    return buildForceData(graphData)
  }, [graphData])

  // Apply force parameters whenever data or physics values change
  useEffect(() => {
    const fg = fgRef.current
    if (!fg) return
    fg.d3Force('charge')?.strength(charge)
    fg.d3Force('link')?.distance(linkDist)
    fg.d3Force('center')?.strength(centerStrength)

    // Cap pixel ratio on mobile — iPhone 16 PM is 3x; capping at 2x cuts framebuffer 9x → 4x
    if (IS_MOBILE) {
      try {
        fg.renderer().setPixelRatio(Math.min(window.devicePixelRatio, 2))
      } catch { /* renderer may not be ready */ }
    }
  }, [forceData, charge, linkDist, centerStrength])

  // Reheat simulation only when physics sliders change (not on data load)
  const physicsInitialized = useRef(false)
  useEffect(() => {
    if (!physicsInitialized.current) {
      physicsInitialized.current = true
      return
    }
    const fg = fgRef.current
    if (!fg) return
    try { fg.d3ReheatSimulation() } catch { /* simulation may not be ready */ }
  }, [charge, linkDist, centerStrength])

  // Search
  const searchLower = searchQuery.toLowerCase()
  const isSearchActive = searchQuery.length > 0

  // Node click: expand neighbors
  const handleNodeClick = useCallback((node: any) => {  
    if (expandingNode) return
    const entityKey = node.id as string
    const displayName = (node.name as string) || entityKey
    setExpandingNode(displayName)
    fetchEntityNeighbors(entityKey).then(data => {
      if (data) {
        setGraphData(prev => prev ? mergeGraphData(prev, data) : data)
      }
      setExpandingNode(null)
    }).catch(() => setExpandingNode(null))
  }, [fetchEntityNeighbors, expandingNode])

  // Shared sphere geometry (reused across all iOS nodes to reduce GPU allocations)
  const sphereGeo = useMemo(() => IS_IOS ? new SphereGeometry(3, 12, 8) : null, [])

  // Custom node rendering — iOS: simple colored spheres (zero per-node textures);
  // other mobile: lightweight SpriteText; desktop: full SpriteText with backgrounds
  const nodeThreeObject = useCallback((node: any) => {  
    try {
      const label = node.name as string
      const color = node.color as string
      const dimmed = isSearchActive && !label.toLowerCase().includes(searchLower)

      // iOS: simple sphere mesh — no canvas textures at all
      if (IS_IOS && sphereGeo) {
        const mat = new MeshLambertMaterial({
          color: dimmed ? resolveCssVar('--text-muted') : color,
          transparent: dimmed,
          opacity: dimmed ? 0.4 : 1,
        })
        return new Mesh(sphereGeo, mat)
      }

      const sprite = new SpriteText(label)
      sprite.color = dimmed ? resolveCssVar('--text-muted') : color
      sprite.fontFace = 'SF Mono, Menlo, monospace'

      if (IS_MOBILE) {
        // Other mobile: smaller text, no background/border (smaller canvas textures)
        sprite.textHeight = 2
      } else {
        // Desktop: full styling
        sprite.textHeight = 3
        sprite.backgroundColor = dimmed
          ? resolveCssVar('--bg-primary', 0.3)
          : resolveCssVar('--bg-primary', 0.75)
        sprite.borderColor = dimmed ? 'transparent' : color
        sprite.borderWidth = 0.3
        sprite.borderRadius = 3
        sprite.padding = [2, 4] as any
      }
      return sprite
    } catch (e) {
      console.error('[KnowledgeGraph] SpriteText creation failed:', e)
      const fallback = new SpriteText('?')
      fallback.color = resolveCssVar('--text-muted')
      fallback.textHeight = 3
      return fallback
    }
  }, [isSearchActive, searchLower, sphereGeo])

  // Link styling
  const linkColor = useCallback((link: any) => {  
    if (isSearchActive) {
      const sourceLabel = typeof link.source === 'object'
        ? (link.source.name ?? link.source.id)
        : link.source
      const targetLabel = typeof link.target === 'object'
        ? (link.target.name ?? link.target.id)
        : link.target
      const srcMatch = String(sourceLabel).toLowerCase().includes(searchLower)
      const tgtMatch = String(targetLabel).toLowerCase().includes(searchLower)
      if (!srcMatch && !tgtMatch) return resolveCssVar('--text-muted', 0.15)
    }
    return link.color || resolveCssVar('--text-muted', 0.4)
  }, [isSearchActive, searchLower])

  const linkLabel = useCallback((link: any) => link.type as string, [])  

  // Node breathing effect — use ref to avoid prop changes that trigger graph rebuilds
  const animateRef = useRef(animateIdle)
  useEffect(() => {
    animateRef.current = animateIdle
  }, [animateIdle])
  const nodePositionUpdate = useCallback((obj: any) => {  
    if (!animateRef.current) {
      // Restore original scale when animation stops
      if (obj.__origScale) {
        obj.scale.copy(obj.__origScale)
        delete obj.__origScale
      }
      return
    }
    // Capture SpriteText's dimensional scale on first animated frame
    if (!obj.__origScale) {
      obj.__origScale = obj.scale.clone()
    }
    const t = performance.now() * 0.001
    const offset = numericId(obj.id) % 100 * 0.1
    const factor = 1 + Math.sin(t * 1.5 + offset) * 0.06
    obj.scale.set(
      obj.__origScale.x * factor,
      obj.__origScale.y * factor,
      obj.__origScale.z * factor
    )
  }, [])

  // Legend types
  const legendTypes = useMemo(() => {
    if (!graphData) return []
    return [...new Set(graphData.entities.map(e => e.entity_type.toLowerCase()))]
  }, [graphData])

  // Loading state
  if (loading) {
    return (
      <div className={CONTAINER_CLS} ref={containerRef}>
        <div className={EMPTY_CLS}>Loading knowledge graph...</div>
      </div>
    )
  }

  // Empty state
  if (!graphData || graphData.entities.length === 0) {
    return (
      <div className={CONTAINER_CLS} ref={containerRef}>
        <div className={EMPTY_CLS}>
          <div>No entities found</div>
          <div className="mt-1 text-[length:var(--text-sm)]">
            Connect a Neo4j instance to explore knowledge graph entities and relationships.
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className={CONTAINER_CLS} ref={containerRef}>
      <ForceGraph3D
        ref={fgRef}
        width={dimensions.width}
        height={dimensions.height}
        graphData={forceData}
        nodeId="id"
        nodeLabel={(node: any) => {  
          const e = node.entity as KnowledgeEntity
          const props = Object.entries(e.properties || {}).slice(0, 4)
            .map(([k, v]) => `${k}: ${String(v).slice(0, 30)}`)
            .join('\n')
          return `<div style="text-align:center;font-family:monospace;font-size:11px;line-height:1.4">
            <b>${e.name}</b><br/>
            <span style="color:${getEntityColorCss(e.entity_type)};text-transform:uppercase;font-size:9px">${e.entity_type}</span>
            ${props ? '<br/><span style="color:var(--text-muted);font-size:9px">' + props.replace(/\n/g, '<br/>') + '</span>' : ''}
          </div>`
        }}
        nodeThreeObject={nodeThreeObject}
        nodeThreeObjectExtend={false}
        onNodeClick={handleNodeClick}
        linkSource="source"
        linkTarget="target"
        linkLabel={linkLabel}
        linkColor={linkColor}
        linkWidth={0.5}
        linkOpacity={0.6}
        linkDirectionalArrowLength={IS_MOBILE ? 0 : 3}
        linkDirectionalArrowRelPos={1}
        linkDirectionalParticles={IS_MOBILE ? 0 : 2}
        linkDirectionalParticleSpeed={0.004}
        linkDirectionalParticleWidth={0.8}
        linkDirectionalParticleColor={linkColor}
        nodePositionUpdate={IS_MOBILE ? undefined : nodePositionUpdate}
        backgroundColor="rgba(0,0,0,0)"
        showNavInfo={false}
        enableNodeDrag={true}
        {...(IS_MOBILE ? { rendererConfig: { antialias: false, powerPreference: 'low-power' as const } } : {})}
      />

      <div className={OVERLAY_STACK_CLS}>
        <div className={INFO_CLS}>
          {forceData.nodes.length} entities &middot; {forceData.links.length} relationships
        </div>
        {expandingNode && (
          <div className={cn(INFO_CLS, 'text-[var(--accent)]')}>
            Expanding {expandingNode}...
          </div>
        )}
        <input
          type="text"
          placeholder="Filter entities..."
          value={searchQuery}
          onChange={e => setSearchQuery(e.target.value)}
          className={SEARCH_INPUT_CLS}
        />
      </div>

      {/* Legend overlay (bottom-left) */}
      {legendTypes.length > 0 && (
        <div className={LEGEND_CLS}>
          {legendTypes.map(type => (
            <div key={type} className="flex items-center gap-1">
              <span
                className="h-2 w-2 shrink-0 rounded-full"
                style={{ backgroundColor: getEntityColorCss(type) }}
              />
              {type}
            </div>
          ))}
        </div>
      )}

      {/* Controls (top-right) */}
      <div className={CONTROLS_CLS}>
        <button
          type="button"
          className={CTRL_BTN_CLS}
          onClick={() => fgRef.current?.zoomToFit(400)}
          title="Zoom to fit"
          aria-label="Zoom to fit"
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7" />
          </svg>
        </button>
        <button
          type="button"
          className={cn(CTRL_BTN_CLS, animateIdle && CTRL_BTN_ACTIVE_CLS)}
          onClick={toggleAnimate}
          title={animateIdle ? 'Pause idle animation' : 'Animate when idle'}
        >
          {animateIdle ? (
            <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
              <rect x="6" y="4" width="4" height="16" rx="1" />
              <rect x="14" y="4" width="4" height="16" rx="1" />
            </svg>
          ) : (
            <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
              <path d="M8 5v14l11-7z" />
            </svg>
          )}
        </button>
        <button
          type="button"
          className={cn(CTRL_BTN_CLS, showPhysics && CTRL_BTN_ACTIVE_CLS)}
          onClick={() => setShowPhysics(p => !p)}
          title="Physics controls"
          aria-label="Physics controls"
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="12" cy="12" r="3" />
            <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
          </svg>
        </button>
      </div>

      {/* Physics controls panel */}
      {showPhysics && (
        <div className={PHYSICS_CLS}>
          <label className={PHYSICS_ROW_CLS}>
            <span className={PHYSICS_LABEL_CLS}>Repulsion</span>
            <input
              type="range"
              min={-500}
              max={-20}
              step={10}
              value={charge}
              className={PHYSICS_SLIDER_CLS}
              onChange={e => {
                const v = Number(e.target.value)
                setCharge(v)
                try { localStorage.setItem('gobby-kg-charge', String(v)) } catch { /* noop */ }
              }}
            />
            <span className={PHYSICS_VALUE_CLS}>{charge}</span>
          </label>
          <label className={PHYSICS_ROW_CLS}>
            <span className={PHYSICS_LABEL_CLS}>Link dist</span>
            <input
              type="range"
              min={10}
              max={200}
              step={5}
              value={linkDist}
              className={PHYSICS_SLIDER_CLS}
              onChange={e => {
                const v = Number(e.target.value)
                setLinkDist(v)
                try { localStorage.setItem('gobby-kg-link-dist', String(v)) } catch { /* noop */ }
              }}
            />
            <span className={PHYSICS_VALUE_CLS}>{linkDist}</span>
          </label>
          <label className={PHYSICS_ROW_CLS}>
            <span className={PHYSICS_LABEL_CLS}>Gravity</span>
            <input
              type="range"
              min={0.005}
              max={0.15}
              step={0.005}
              value={centerStrength}
              className={PHYSICS_SLIDER_CLS}
              onChange={e => {
                const v = Number(e.target.value)
                setCenterStrength(v)
                try { localStorage.setItem('gobby-kg-center', String(v)) } catch { /* noop */ }
              }}
            />
            <span className={PHYSICS_VALUE_CLS}>{centerStrength.toFixed(3)}</span>
          </label>
          <button
            type="button"
            className={PHYSICS_RESET_CLS}
            onClick={() => {
              setCharge(DEFAULT_CHARGE)
              setLinkDist(DEFAULT_LINK_DIST)
              setCenterStrength(DEFAULT_CENTER)
              try {
                localStorage.removeItem('gobby-kg-charge')
                localStorage.removeItem('gobby-kg-link-dist')
                localStorage.removeItem('gobby-kg-center')
              } catch { /* noop */ }
            }}
          >
            Reset
          </button>
        </div>
      )}
    </div>
  )
}
