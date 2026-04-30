import { useState, useEffect, useRef, useMemo, useCallback } from 'react'
import ForceGraph3D from 'react-force-graph-3d'
import SpriteText from 'three-spritetext'
import { useCodeGraph, mergeCodeGraphData } from '../../hooks/useCodeGraph'
import type { CodeGraphData, CodeGraphNode, CodeGraphSearchResult } from '../../hooks/useCodeGraph'
import { IS_MOBILE, IS_IOS } from '../../utils/platform'
import { resolveCssVar } from '../../lib/utils'
import './CodeGraphExplorer.css'

const DEFAULT_CODE_GRAPH_LIMIT = IS_IOS ? 30 : IS_MOBILE ? 50 : 100
const CODE_GRAPH_LIMIT_MIN = 10
const CODE_GRAPH_LIMIT_MAX = IS_IOS ? 100 : IS_MOBILE ? 200 : 1000
const CODE_GRAPH_LIMIT_STEP = 10

const DEFAULT_CHARGE = -120
const DEFAULT_LINK_DIST = 60
const DEFAULT_CENTER = 0.05

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
      <div className="code-graph-empty">
        Select a project to explore its code graph.
      </div>
    )
  }

  if (webglError) {
    return (
      <div className="code-graph-empty">
        WebGL error — your browser may not support 3D rendering.
        Try refreshing the page.
      </div>
    )
  }

  return (
    <div className="code-graph-explorer" ref={containerRef}>
      {/* Controls */}
      <div className="code-graph-controls">
        <button
          className={`code-graph-btn ${blastMode ? 'active' : ''}`}
          onClick={toggleBlastMode}
          title="Blast Radius Mode"
        >
          {blastMode ? 'Blast On' : 'Blast Radius'}
        </button>
        <button className="code-graph-btn" onClick={handleZoomToFit} title="Zoom to Fit">
          Fit
        </button>
        <button
          className={`code-graph-btn${showPhysics ? ' active' : ''}`}
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
        <div className="code-graph-physics">
          <label className="code-graph-physics-row">
            <span className="code-graph-physics-label">Repulsion</span>
            <input
              type="range"
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
            <span className="code-graph-physics-value">{charge}</span>
          </label>
          <label className="code-graph-physics-row">
            <span className="code-graph-physics-label">Link dist</span>
            <input
              type="range"
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
            <span className="code-graph-physics-value">{linkDist}</span>
          </label>
          <label className="code-graph-physics-row">
            <span className="code-graph-physics-label">Gravity</span>
            <input
              type="range"
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
            <span className="code-graph-physics-value">{centerStrength.toFixed(3)}</span>
          </label>
          <label className="code-graph-physics-row">
            <span className="code-graph-physics-label">Limit</span>
            <input
              type="range"
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
            <span className="code-graph-physics-value">{limit}</span>
          </label>
          <button
            className="code-graph-physics-reset"
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
      <div className="code-graph-search">
        <input
          type="text"
          placeholder="Search symbols..."
          value={searchQuery}
          onChange={e => handleSearch(e.target.value)}
          className="code-graph-search-input"
        />
        {searchResults.length > 0 && (
          <div className="code-graph-search-results">
            {searchResults.map(r => (
              <button
                key={r.id}
                className="code-graph-search-result"
                onClick={() => handleSearchResultClick(r)}
              >
                <span className="code-graph-search-kind" style={{ color: getNodeColorCss(r.kind) }}>
                  {r.kind || r.type}
                </span>
                <span className="code-graph-search-name">{r.name}</span>
                {r.file_path && (
                  <span className="code-graph-search-path">{r.file_path}</span>
                )}
              </button>
            ))}
          </div>
        )}
      </div>

      {/* Info overlay */}
      <div className="code-graph-info">
        {forceData.nodes.length} nodes &middot; {forceData.links.length} edges
        {isLoading && ' (loading...)'}
      </div>

      {/* Legend */}
      <div className="code-graph-legend">
        {Object.keys(NODE_COLOR_VARS).slice(0, 7).map(type => (
          <div key={type} className="code-graph-legend-item">
            <span className="code-graph-legend-dot" style={{ background: getNodeColorCss(type) }} />
            <span>{type}</span>
          </div>
        ))}
        <div className="code-graph-legend-separator" />
        {Object.keys(EDGE_COLOR_VARS).map(type => (
          <div key={type} className="code-graph-legend-item">
            <span className="code-graph-legend-line" style={{ background: `var(${edgeColorVar(type)})` }} />
            <span>{type.toLowerCase()}</span>
          </div>
        ))}
      </div>

      {/* Detail panel */}
      {selectedNode && (
        <div className="code-graph-detail">
          <div className="code-graph-detail-header">
            <span className="code-graph-detail-type" style={{ color: getNodeColorCss(selectedNode.type) }}>
              {selectedNode.type}
            </span>
            <button className="code-graph-detail-close" onClick={() => setSelectedNode(null)}>&times;</button>
          </div>
          <div className="code-graph-detail-name">{selectedNode.name}</div>
          {selectedNode.signature && (
            <div className="code-graph-detail-sig">{selectedNode.signature}</div>
          )}
          {selectedNode.file_path && selectedNode.type !== 'file' && (
            <div className="code-graph-detail-path">
              {selectedNode.file_path}{selectedNode.line_start ? `:${selectedNode.line_start}` : ''}
            </div>
          )}
          {selectedNode.symbol_count !== undefined && (
            <div className="code-graph-detail-meta">{selectedNode.symbol_count} symbols</div>
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
