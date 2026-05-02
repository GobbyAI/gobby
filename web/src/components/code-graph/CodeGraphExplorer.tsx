import { useState, useEffect, useRef, useMemo, useCallback } from 'react'
import ForceGraph3D from 'react-force-graph-3d'
import SpriteText from 'three-spritetext'
import { useCodeGraph, mergeCodeGraphData } from '../../hooks/useCodeGraph'
import type { CodeGraphData, CodeGraphNode, CodeGraphSearchResult } from '../../hooks/useCodeGraph'
import { IS_MOBILE, IS_IOS } from '../../utils/platform'
import { resolveCssVar, cn } from '../../lib/utils'

const DEFAULT_CODE_GRAPH_LIMIT = IS_IOS ? 30 : IS_MOBILE ? 50 : 100
const CODE_GRAPH_LIMIT_MIN = 10
const CODE_GRAPH_LIMIT_MAX = IS_IOS ? 100 : IS_MOBILE ? 200 : 1000
const CODE_GRAPH_LIMIT_STEP = 10

const DEFAULT_CHARGE = -120
const DEFAULT_LINK_DIST = 60
const DEFAULT_CENTER = 0.05

const ROOT_CLS =
  'relative h-full w-full overflow-hidden bg-[var(--bg-primary)] [background-image:radial-gradient(circle_at_50%_50%,color-mix(in_srgb,var(--color-info)_2%,transparent),transparent_70%)]'
const EMPTY_CLS = 'flex h-full items-center justify-center text-[length:var(--text-base)] text-[var(--text-muted)]'

const CONTROLS_CLS = 'absolute right-3 top-3 z-10 flex gap-1.5'
const BTN_CLS =
  'cursor-pointer rounded border border-[var(--border)] bg-[var(--bg-secondary)] px-2.5 py-1 font-mono text-[length:var(--text-sm)] text-[var(--text-secondary)] transition-all duration-150 hover:border-[var(--accent)] hover:text-[var(--text-primary)] pointer-coarse:min-h-11 pointer-coarse:px-3'
const BTN_ACTIVE_CLS =
  'border-[var(--destructive,var(--color-error))] bg-[color-mix(in_srgb,var(--destructive,var(--color-error))_15%,transparent)] text-[var(--destructive,var(--color-error))]'

const SEARCH_WRAP_CLS = 'absolute left-3 top-3 z-10 w-[260px]'
const SEARCH_INPUT_CLS =
  'w-full rounded border border-[var(--border)] bg-[var(--bg-secondary)] px-2.5 py-1.5 font-mono text-[length:var(--text-sm)] text-[var(--text-primary)] outline-none transition-colors duration-150 placeholder:text-[var(--text-muted)] focus:border-[var(--accent)] pointer-coarse:min-h-11'
const SEARCH_RESULTS_CLS =
  'mt-1 max-h-[240px] overflow-y-auto rounded border border-[var(--border)] bg-[var(--bg-secondary)]'
const SEARCH_RESULT_CLS =
  'flex w-full cursor-pointer items-center gap-2 border-0 bg-transparent px-2.5 py-1.5 text-left font-mono text-[length:var(--text-sm)] text-[var(--text-primary)] hover:bg-[var(--bg-tertiary)] pointer-coarse:min-h-11'
const SEARCH_KIND_CLS = 'min-w-[55px] shrink-0 text-[length:var(--text-2xs)] uppercase tracking-[0.5px]'
const SEARCH_NAME_CLS = 'shrink-0 font-medium'
const SEARCH_PATH_CLS = 'overflow-hidden text-ellipsis whitespace-nowrap text-[length:var(--text-2xs)] text-[var(--text-muted)]'

const INFO_CLS =
  'absolute bottom-3 left-3 z-10 rounded bg-[color-mix(in_srgb,black_80%,transparent)] px-2 py-1 font-mono text-[length:var(--text-xs)] text-[var(--text-muted)]'

const LEGEND_CLS =
  'absolute bottom-3 right-3 z-10 flex flex-col gap-[3px] rounded border border-[var(--border)] bg-[color-mix(in_srgb,black_85%,transparent)] px-2.5 py-2'
const LEGEND_ITEM_CLS = 'flex items-center gap-1.5 font-mono text-[length:var(--text-2xs)] text-[var(--text-secondary)]'
const LEGEND_DOT_CLS = 'h-2 w-2 shrink-0 rounded-full'
const LEGEND_LINE_CLS = 'h-0.5 w-3 shrink-0 rounded-[1px]'
const LEGEND_SEPARATOR_CLS = 'my-0.5 h-px bg-[var(--border)]'

const DETAIL_CLS =
  'absolute right-3 top-[50px] z-10 w-[250px] rounded-md border border-[var(--border)] bg-[var(--bg-secondary)] p-3'
const DETAIL_HEADER_CLS = 'mb-1.5 flex items-center justify-between'
const DETAIL_TYPE_CLS = 'font-mono text-[length:var(--text-2xs)] uppercase tracking-[0.5px]'
const DETAIL_CLOSE_CLS =
  'cursor-pointer border-0 bg-transparent p-0 text-[length:var(--text-lg)] leading-none text-[var(--text-muted)] hover:text-[var(--text-primary)] pointer-coarse:h-11 pointer-coarse:w-11'
const DETAIL_NAME_CLS = 'mb-1 break-all font-mono text-[length:var(--text-md)] font-semibold text-[var(--text-primary)]'
const DETAIL_SIG_CLS =
  'mb-1 overflow-x-auto whitespace-nowrap rounded-[3px] bg-[color-mix(in_srgb,black_30%,transparent)] px-1.5 py-1 font-mono text-[length:var(--text-xs)] text-[var(--color-warning-foreground)]'
const DETAIL_PATH_CLS = 'mb-0.5 font-mono text-[length:var(--text-xs)] text-[var(--text-muted)]'
const DETAIL_META_CLS = 'font-mono text-[length:var(--text-2xs)] text-[var(--text-muted)]'

const PHYSICS_CLS =
  'absolute right-2 top-[42px] z-[15] flex min-w-[200px] flex-col gap-[5px] rounded-md border border-[var(--border)] bg-[var(--bg-secondary)] px-2.5 py-2'
const PHYSICS_ROW_CLS = 'flex cursor-default items-center gap-1.5'
const PHYSICS_LABEL_CLS = 'min-w-[56px] text-[length:var(--text-xs)] text-[var(--text-secondary)]'
const PHYSICS_VALUE_CLS = 'min-w-[32px] text-right text-[length:var(--text-2xs)] tabular-nums text-[var(--text-muted)]'
const PHYSICS_SLIDER_CLS = 'h-1 flex-1 cursor-pointer accent-[var(--accent)]'
const PHYSICS_RESET_CLS =
  'mt-0.5 cursor-pointer rounded border border-[var(--border)] bg-[var(--bg-tertiary)] px-2 py-0.5 text-[length:var(--text-xs)] text-[var(--text-muted)] transition-colors duration-150 hover:bg-[var(--bg-primary)] hover:text-[var(--text-primary)] pointer-coarse:min-h-11 pointer-coarse:px-3'

interface CodeGraphExplorerProps {
  projectId: string | null
}

// ── Node / edge colors routed through deutan-safe semantic tokens ───────────
// Multiple types intentionally collapse onto the same token; the legend
// disambiguates by name. resolveCssVar() returns canvas-normalized strings
// for three.js consumers; getNodeColorCss() returns var() form for HTML.

const NODE_COLOR_VARS: Record<string, string> = {
  file: '--color-info',
  folder: '--color-agent',
  class: '--color-warning-foreground',
  function: '--color-success-foreground',
  method: '--color-review',
  interface: '--color-error',
  module: '--color-agent',
  constant: '--color-warning-foreground',
  variable: '--text-muted',
  type: '--color-agent',
  unresolved: '--color-error',
  external: '--color-error',
}

const EDGE_COLOR_VARS: Record<string, string> = {
  CALLS: '--color-agent',
  IMPORTS: '--color-info',
  DEFINES: '--color-review',
}

// Blast-radius gradient: hottest (closest) → coolest (farthest).
const BLAST_COLOR_VARS = [
  '--color-error',
  '--color-warning-foreground',
  '--accent',
  '--color-success-foreground',
]

function nodeColorVar(type: string): string {
  return NODE_COLOR_VARS[type] ?? '--text-muted'
}

function edgeColorVar(type: string): string {
  return EDGE_COLOR_VARS[type] ?? '--text-muted'
}

function getNodeColor(node: GraphNode): string {
  if (node.blast_distance !== undefined && node.blast_distance >= 0) {
    const idx = Math.min(node.blast_distance, BLAST_COLOR_VARS.length - 1)
    return resolveCssVar(BLAST_COLOR_VARS[idx])
  }
  return resolveCssVar(nodeColorVar(node.type))
}

function getNodeColorCss(type: string | undefined): string {
  return `var(${type ? nodeColorVar(type) : '--text-muted'})`
}

// ── Force graph data types ─────────────────────────────────────

interface GraphNode {
  id: string
  name: string
  type: string
  kind?: string
  file_path?: string
  line_start?: number
  signature?: string
  symbol_count?: number
  blast_distance?: number
  color: string
  val: number
}

interface GraphLink {
  source: string
  target: string
  type: string
  color: string
}

function buildForceData(data: CodeGraphData): { nodes: GraphNode[]; links: GraphLink[] } {
  const nodeIds = new Set(data.nodes.map(n => n.id))

  const nodes: GraphNode[] = data.nodes.map(n => {
    const gn: GraphNode = {
      id: n.id,
      name: n.name,
      type: n.type,
      kind: n.kind,
      file_path: n.file_path,
      line_start: n.line_start,
      signature: n.signature,
      symbol_count: n.symbol_count,
      blast_distance: n.blast_distance,
      color: '',
      val: 2,
    }
    gn.color = getNodeColor(gn)
    return gn
  })

  const links: GraphLink[] = data.links
    .filter(l => nodeIds.has(l.source) && nodeIds.has(l.target))
    .map(l => ({
      source: l.source,
      target: l.target,
      type: l.type,
      color: resolveCssVar(edgeColorVar(l.type)),
    }))

  return { nodes, links }
}

function edgeColor(relType: string): string {
  return resolveCssVar(edgeColorVar(relType))
}

function escapeHtml(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')
}

function getStoredNumber(key: string, defaultVal: number, min?: number, max?: number): number {
  try {
    const v = localStorage.getItem(key)
    const n = Number(v)
    if (!v || !Number.isFinite(n)) return defaultVal
    if (min !== undefined && n < min) return defaultVal
    if (max !== undefined && n > max) return defaultVal
    return n
  } catch {
    return defaultVal
  }
}

// ── Component ──────────────────────────────────────────────────

export function CodeGraphExplorer({ projectId }: CodeGraphExplorerProps) {
  // react-force-graph-3d does not export a usable instance type
  const fgRef = useRef<any>(null)
  const containerRef = useRef<HTMLDivElement>(null)
  const [dimensions, setDimensions] = useState({ width: 800, height: 600 })
  const [graphData, setGraphData] = useState<CodeGraphData>({ nodes: [], links: [] })
  const [selectedNode, setSelectedNode] = useState<GraphNode | null>(null)
  const [blastMode, setBlastMode] = useState(false)
  const [blastData, setBlastData] = useState<Set<string> | null>(null)
  const [searchQuery, setSearchQuery] = useState('')
  const [searchResults, setSearchResults] = useState<CodeGraphSearchResult[]>([])
  const [isLoading, setIsLoading] = useState(false)
  const [expandedNodes, setExpandedNodes] = useState<Set<string>>(new Set())
  const [webglError, setWebglError] = useState(false)
  const [showPhysics, setShowPhysics] = useState(false)
  const [limit, setLimit] = useState(() => getStoredNumber('gobby-cg-limit', DEFAULT_CODE_GRAPH_LIMIT, CODE_GRAPH_LIMIT_MIN, CODE_GRAPH_LIMIT_MAX))
  const [charge, setCharge] = useState(() => getStoredNumber('gobby-cg-charge', DEFAULT_CHARGE))
  const [linkDist, setLinkDist] = useState(() => getStoredNumber('gobby-cg-link-dist', DEFAULT_LINK_DIST))
  const [centerStrength, setCenterStrength] = useState(() => getStoredNumber('gobby-cg-center', DEFAULT_CENTER))
  const searchDebounceRef = useRef<number | null>(null)

  const { fetchFileGraph, expandFile, expandSymbol, fetchBlastRadius, searchSymbols } = useCodeGraph()

  // Fetch config override for limit
  useEffect(() => {
    const controller = new AbortController()
    fetch('/api/config/values', { signal: controller.signal })
      .then(res => res.ok ? res.json() : null)
      .then(data => {
        if (!data) return
        const values = data.values ?? data
        const cgLimit = values?.['ui.code_graph_limit']
        if (typeof cgLimit === 'number' && cgLimit >= CODE_GRAPH_LIMIT_MIN) setLimit(cgLimit)
      })
      .catch((e) => { if (e.name !== 'AbortError') console.debug('Config fetch failed:', e) })
    return () => controller.abort()
  }, [])

  // WebGL error handling (from KnowledgeGraph pattern)
  useEffect(() => {
    const handleError = (e: ErrorEvent) => {
      const msg = (e.message || '').toLowerCase()
      if (msg.includes('webgl') || msg.includes('three') || msg.includes('context lost')) {
        e.preventDefault()
        setWebglError(true)
      }
    }
    window.addEventListener('error', handleError)
    return () => window.removeEventListener('error', handleError)
  }, [])

  // Clean up search debounce on unmount
  useEffect(() => {
    return () => {
      if (searchDebounceRef.current) clearTimeout(searchDebounceRef.current)
    }
  }, [])

  // Resize observer
  useEffect(() => {
    const container = containerRef.current
    if (!container) return
    const ro = new ResizeObserver(entries => {
      const { width, height } = entries[0].contentRect
      setDimensions({ width, height })
    })
    ro.observe(container)
    return () => ro.disconnect()
  }, [])

  // Initial load (re-fetch when limit changes)
  useEffect(() => {
    if (!projectId) return
    setIsLoading(true)
    setExpandedNodes(new Set())
    fetchFileGraph(projectId, limit).then(data => {
      if (data) setGraphData(data)
    }).catch(e => {
      console.error('CodeGraphExplorer: fetchFileGraph failed', e)
    }).finally(() => {
      setIsLoading(false)
    })
  }, [projectId, limit, fetchFileGraph])

  // Build force data
  const forceData = useMemo(() => buildForceData(graphData), [graphData])

  // Apply force parameters whenever data or physics values change, and reheat
  // the simulation so a fresh batch of nodes actually spreads instead of
  // collapsing into one super-cluster at the origin.
  useEffect(() => {
    const fg = fgRef.current
    if (!fg) return
    fg.d3Force('charge')?.strength(charge)
    fg.d3Force('link')?.distance(linkDist)
    fg.d3Force('center')?.strength(centerStrength)
    if (IS_MOBILE) {
      try { fg.renderer().setPixelRatio(Math.min(window.devicePixelRatio, 2)) } catch (e) { console.warn('CodeGraphExplorer: setPixelRatio failed', e) }
    }
    try { fg.d3ReheatSimulation() } catch { /* simulation may not be ready */ }
  }, [forceData, charge, linkDist, centerStrength])

  // Search
  const searchLower = searchQuery.toLowerCase()
  const isSearchActive = searchQuery.length > 0

  // Node click handler
  const handleNodeClick = useCallback(async (node: any) => {
    if (!projectId) return
    setSelectedNode(node as GraphNode)

    if (blastMode) {
      const opts = node.type === 'file'
        ? { filePath: node.id }
        : { symbolId: node.id }
      const data = await fetchBlastRadius(projectId, opts)
      if (data) {
        const affected = new Set(data.nodes.map((n: any) => n.id))
        setBlastData(affected)
        setGraphData(prev => mergeCodeGraphData(prev, data))
      }
      return
    }

    if (expandedNodes.has(node.id)) return
    setExpandedNodes(prev => new Set(prev).add(node.id))

    let newData: CodeGraphData | null = null
    if (node.type === 'file') {
      newData = await expandFile(projectId, node.id)
    } else {
      newData = await expandSymbol(projectId, node.id)
    }
    if (newData) {
      setGraphData(prev => mergeCodeGraphData(prev, newData!))
    }
  }, [projectId, blastMode, expandedNodes, fetchBlastRadius, expandFile, expandSymbol])

  // Search handler
  const handleSearch = useCallback((query: string) => {
    setSearchQuery(query)
    if (searchDebounceRef.current) clearTimeout(searchDebounceRef.current)
    if (!query.trim() || !projectId) { setSearchResults([]); return }
    searchDebounceRef.current = window.setTimeout(async () => {
      const results = await searchSymbols(projectId, query)
      setSearchResults(results)
    }, 300)
  }, [projectId, searchSymbols])

  // Search result click
  const handleSearchResultClick = useCallback(async (result: any) => {
    if (!projectId) return
    setSearchQuery('')
    setSearchResults([])

    const exists = graphData.nodes.some(n => n.id === result.id)
    if (!exists) {
      const data = await expandSymbol(projectId, result.id)
      if (data) {
        const resultNode: CodeGraphNode = {
          id: result.id, name: result.name,
          type: result.type || result.kind || 'function',
          kind: result.kind, file_path: result.file_path,
        }
        const merged = mergeCodeGraphData({ nodes: [resultNode], links: [] }, data)
        setGraphData(prev => mergeCodeGraphData(prev, merged))
      }
    }

    // Center on node in 3D
    if (fgRef.current) {
      const node = fgRef.current.graphData().nodes.find((n: any) => n.id === result.id)
      if (node) {
        const distance = 200
        const hyp = Math.hypot(node.x, node.y, node.z)
        const distRatio = hyp === 0 ? 1 : 1 + distance / hyp
        fgRef.current.cameraPosition(
          { x: node.x * distRatio, y: node.y * distRatio, z: node.z * distRatio },
          node, 1000
        )
      }
    }
  }, [projectId, graphData.nodes, expandSymbol])

  // Zoom to fit
  const handleZoomToFit = useCallback(() => {
    fgRef.current?.zoomToFit(400, 40)
  }, [])

  // Toggle blast radius mode
  const toggleBlastMode = useCallback(() => {
    setBlastMode(prev => {
      if (prev) setBlastData(null)
      return !prev
    })
  }, [])

  // 3D node rendering — SpriteText (same pattern as KnowledgeGraph)
  const nodeThreeObject = useCallback((node: any) => {
    try {
      const label = node.type === 'file'
        ? (node.name as string).split('/').pop() || node.name
        : node.name as string
      const color = node.color as string
      const dimmed = blastData ? !blastData.has(node.id) : false
      const searchDimmed = isSearchActive && !label.toLowerCase().includes(searchLower)
      const isDimmed = dimmed || searchDimmed

      const sprite = new SpriteText(label)
      sprite.color = isDimmed ? resolveCssVar('--text-muted') : color
      sprite.fontFace = 'JetBrains Mono, SF Mono, Menlo, monospace'

      if (IS_MOBILE) {
        sprite.textHeight = 2
      } else {
        sprite.textHeight = 3
        sprite.backgroundColor = isDimmed
          ? resolveCssVar('--bg-primary', 0.3)
          : resolveCssVar('--bg-primary', 0.75)
        sprite.borderColor = isDimmed ? 'transparent' : color
        sprite.borderWidth = 0.3
        sprite.borderRadius = 3
        sprite.padding = [2, 4] as any
      }
      return sprite
    } catch {
      const fallback = new SpriteText('?')
      fallback.color = resolveCssVar('--text-muted')
      fallback.textHeight = 3
      return fallback
    }
  }, [blastData, isSearchActive, searchLower])

  // Link color — must always return a visible color by default
  const linkColor = useCallback((link: any) => {
    const srcId = typeof link.source === 'object' ? link.source.id : link.source
    const tgtId = typeof link.target === 'object' ? link.target.id : link.target

    if (blastData) {
      if (!blastData.has(srcId) || !blastData.has(tgtId)) return resolveCssVar('--text-muted', 0.1)
    }
    if (isSearchActive) {
      const srcLabel = typeof link.source === 'object'
        ? (link.source.name ?? link.source.id)
        : link.source
      const tgtLabel = typeof link.target === 'object'
        ? (link.target.name ?? link.target.id)
        : link.target
      const srcMatch = String(srcLabel).toLowerCase().includes(searchLower)
      const tgtMatch = String(tgtLabel).toLowerCase().includes(searchLower)
      if (!srcMatch && !tgtMatch) return resolveCssVar('--text-muted', 0.15)
    }
    return link.color || edgeColor(link.type) || resolveCssVar('--text-muted', 0.4)
  }, [blastData, isSearchActive, searchLower])

  const linkLabel = useCallback((link: any) => link.type as string, [])

  if (!projectId) {
    return (
      <div className={EMPTY_CLS}>
        Select a project to explore its code graph.
      </div>
    )
  }

  if (webglError) {
    return (
      <div className={EMPTY_CLS}>
        WebGL error — your browser may not support 3D rendering.
        Try refreshing the page.
      </div>
    )
  }

  return (
    <div className={ROOT_CLS} ref={containerRef}>
      {/* Controls */}
      <div className={CONTROLS_CLS}>
        <button
          className={cn(BTN_CLS, blastMode && BTN_ACTIVE_CLS)}
          onClick={toggleBlastMode}
          title="Blast Radius Mode"
        >
          {blastMode ? 'Blast On' : 'Blast Radius'}
        </button>
        <button className={BTN_CLS} onClick={handleZoomToFit} title="Zoom to Fit">
          Fit
        </button>
        <button
          className={cn(BTN_CLS, showPhysics && BTN_ACTIVE_CLS)}
          onClick={() => setShowPhysics(p => !p)}
          title="Physics controls"
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
              className={PHYSICS_SLIDER_CLS}
              min={-500}
              max={-20}
              step={10}
              value={charge}
              onChange={e => {
                const v = Number(e.target.value)
                setCharge(v)
                try { localStorage.setItem('gobby-cg-charge', String(v)) } catch { /* noop */ }
              }}
            />
            <span className={PHYSICS_VALUE_CLS}>{charge}</span>
          </label>
          <label className={PHYSICS_ROW_CLS}>
            <span className={PHYSICS_LABEL_CLS}>Link dist</span>
            <input
              type="range"
              className={PHYSICS_SLIDER_CLS}
              min={10}
              max={200}
              step={5}
              value={linkDist}
              onChange={e => {
                const v = Number(e.target.value)
                setLinkDist(v)
                try { localStorage.setItem('gobby-cg-link-dist', String(v)) } catch { /* noop */ }
              }}
            />
            <span className={PHYSICS_VALUE_CLS}>{linkDist}</span>
          </label>
          <label className={PHYSICS_ROW_CLS}>
            <span className={PHYSICS_LABEL_CLS}>Gravity</span>
            <input
              type="range"
              className={PHYSICS_SLIDER_CLS}
              min={0.005}
              max={0.15}
              step={0.005}
              value={centerStrength}
              onChange={e => {
                const v = Number(e.target.value)
                setCenterStrength(v)
                try { localStorage.setItem('gobby-cg-center', String(v)) } catch { /* noop */ }
              }}
            />
            <span className={PHYSICS_VALUE_CLS}>{centerStrength.toFixed(3)}</span>
          </label>
          <label className={PHYSICS_ROW_CLS}>
            <span className={PHYSICS_LABEL_CLS}>Limit</span>
            <input
              type="range"
              className={PHYSICS_SLIDER_CLS}
              min={CODE_GRAPH_LIMIT_MIN}
              max={CODE_GRAPH_LIMIT_MAX}
              step={CODE_GRAPH_LIMIT_STEP}
              value={limit}
              onChange={e => {
                const v = Number(e.target.value)
                setLimit(v)
                try { localStorage.setItem('gobby-cg-limit', String(v)) } catch { /* noop */ }
              }}
            />
            <span className={PHYSICS_VALUE_CLS}>{limit}</span>
          </label>
          <button
            className={PHYSICS_RESET_CLS}
            onClick={() => {
              setCharge(DEFAULT_CHARGE)
              setLinkDist(DEFAULT_LINK_DIST)
              setCenterStrength(DEFAULT_CENTER)
              setLimit(DEFAULT_CODE_GRAPH_LIMIT)
              try {
                localStorage.removeItem('gobby-cg-charge')
                localStorage.removeItem('gobby-cg-link-dist')
                localStorage.removeItem('gobby-cg-center')
                localStorage.removeItem('gobby-cg-limit')
              } catch { /* noop */ }
            }}
          >
            Reset
          </button>
        </div>
      )}

      {/* Search */}
      <div className={SEARCH_WRAP_CLS}>
        <input
          type="text"
          placeholder="Search symbols..."
          value={searchQuery}
          onChange={e => handleSearch(e.target.value)}
          className={SEARCH_INPUT_CLS}
        />
        {searchResults.length > 0 && (
          <div className={SEARCH_RESULTS_CLS}>
            {searchResults.map(r => (
              <button
                key={r.id}
                className={SEARCH_RESULT_CLS}
                onClick={() => handleSearchResultClick(r)}
              >
                <span className={SEARCH_KIND_CLS} style={{ color: getNodeColorCss(r.kind) }}>
                  {r.kind || r.type}
                </span>
                <span className={SEARCH_NAME_CLS}>{r.name}</span>
                {r.file_path && (
                  <span className={SEARCH_PATH_CLS}>{r.file_path}</span>
                )}
              </button>
            ))}
          </div>
        )}
      </div>

      {/* Info overlay */}
      <div className={INFO_CLS}>
        {forceData.nodes.length} nodes &middot; {forceData.links.length} edges
        {isLoading && ' (loading...)'}
      </div>

      {/* Legend */}
      <div className={LEGEND_CLS}>
        {Object.keys(NODE_COLOR_VARS).slice(0, 7).map(type => (
          <div key={type} className={LEGEND_ITEM_CLS}>
            <span className={LEGEND_DOT_CLS} style={{ background: getNodeColorCss(type) }} />
            <span>{type}</span>
          </div>
        ))}
        <div className={LEGEND_SEPARATOR_CLS} />
        {Object.keys(EDGE_COLOR_VARS).map(type => (
          <div key={type} className={LEGEND_ITEM_CLS}>
            <span className={LEGEND_LINE_CLS} style={{ background: `var(${edgeColorVar(type)})` }} />
            <span>{type.toLowerCase()}</span>
          </div>
        ))}
      </div>

      {/* Detail panel */}
      {selectedNode && (
        <div className={DETAIL_CLS}>
          <div className={DETAIL_HEADER_CLS}>
            <span className={DETAIL_TYPE_CLS} style={{ color: getNodeColorCss(selectedNode.type) }}>
              {selectedNode.type}
            </span>
            <button className={DETAIL_CLOSE_CLS} onClick={() => setSelectedNode(null)}>&times;</button>
          </div>
          <div className={DETAIL_NAME_CLS}>{selectedNode.name}</div>
          {selectedNode.signature && (
            <div className={DETAIL_SIG_CLS}>{selectedNode.signature}</div>
          )}
          {selectedNode.file_path && selectedNode.type !== 'file' && (
            <div className={DETAIL_PATH_CLS}>
              {selectedNode.file_path}{selectedNode.line_start ? `:${selectedNode.line_start}` : ''}
            </div>
          )}
          {selectedNode.symbol_count !== undefined && (
            <div className={DETAIL_META_CLS}>{selectedNode.symbol_count} symbols</div>
          )}
        </div>
      )}

      {/* 3D Graph */}
      <ForceGraph3D
        ref={fgRef}
        graphData={forceData}
        width={dimensions.width}
        height={dimensions.height}
        nodeId="id"
        nodeThreeObject={nodeThreeObject}
        nodeThreeObjectExtend={false}
        onNodeClick={handleNodeClick}
        nodeLabel={(node: any) => {
          const name = escapeHtml(String(node.name || ''))
          const parts = [`<b>${name}</b>`]
          if (node.kind) parts.push(`<br/><span style="color:${getNodeColorCss(node.type)};text-transform:uppercase;font-size:9px">${escapeHtml(String(node.kind))}</span>`)
          if (node.signature) parts.push(`<br/><span style="color:var(--color-warning-foreground);font-size:9px">${escapeHtml(String(node.signature))}</span>`)
          if (node.file_path && node.type !== 'file') parts.push(`<br/><span style="color:var(--text-muted);font-size:9px">${escapeHtml(String(node.file_path))}${node.line_start ? ':' + node.line_start : ''}</span>`)
          return `<div style="text-align:center;font-family:monospace;font-size:11px;line-height:1.4">${parts.join('')}</div>`
        }}
        linkSource="source"
        linkTarget="target"
        linkColor={linkColor}
        linkLabel={linkLabel}
        linkWidth={0.5}
        linkOpacity={0.6}
        linkDirectionalArrowLength={IS_MOBILE ? 0 : 3}
        linkDirectionalArrowRelPos={1}
        linkDirectionalParticles={IS_MOBILE ? 0 : 2}
        linkDirectionalParticleSpeed={0.004}
        linkDirectionalParticleWidth={0.8}
        linkDirectionalParticleColor={linkColor}
        backgroundColor="rgba(0,0,0,0)"
        showNavInfo={false}
        enableNodeDrag={true}
        {...(IS_MOBILE ? { rendererConfig: { antialias: false, powerPreference: 'low-power' as const } } : {})}
      />
    </div>
  )
}
