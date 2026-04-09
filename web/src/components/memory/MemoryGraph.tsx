import { useState, useEffect, useRef, useMemo, useCallback } from 'react'
import ForceGraph2D from 'react-force-graph-2d'
import type { GobbyMemory, MemoryGraphData } from '../../hooks/useMemory'

interface MemoryGraphProps {
  fetchGraphData: (memoryLimit?: number) => Promise<MemoryGraphData | null>
  onSelect: (memory: GobbyMemory) => void
  memoryLimit?: number
}

interface GraphNode {
  id: string
  name: string
  memory: GobbyMemory
  color: string
  val: number
}

interface GraphLink {
  source: string
  target: string
  value: number
}

const TYPE_COLORS: Record<string, string> = {
  fact: '#60a5fa',
  preference: '#c084fc',
  pattern: '#34d399',
  context: '#fbbf24',
}

function getTypeColor(type: string): string {
  return TYPE_COLORS[type] || '#8b8b8b'
}

function buildForceData(
  data: MemoryGraphData
): { nodes: GraphNode[]; links: GraphLink[] } {
  const memoryIds = new Set(data.memories.map(m => m.id))

  const nodes: GraphNode[] = data.memories.map(m => ({
    id: m.id,
    name: m.content.length > 40 ? m.content.slice(0, 40) + '...' : m.content,
    memory: m,
    color: getTypeColor(m.memory_type),
    val: 1 + m.importance * 3,
  }))

  const links: GraphLink[] = data.crossrefs
    .filter(c => memoryIds.has(c.source_id) && memoryIds.has(c.target_id))
    .map(c => ({
      source: c.source_id,
      target: c.target_id,
      value: c.similarity,
    }))

  return { nodes, links }
}

export function MemoryGraph({ fetchGraphData, onSelect, memoryLimit }: MemoryGraphProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const fgRef = useRef<any>(null)  
  const [graphData, setGraphData] = useState<MemoryGraphData | null>(null)
  const [loading, setLoading] = useState(true)
  const [dimensions, setDimensions] = useState({ width: 800, height: 600 })

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

  // Fetch graph data
  useEffect(() => {
    let cancelled = false
    setLoading(true)
    fetchGraphData(memoryLimit)
      .then(data => {
        if (!cancelled && data) setGraphData(data)
      })
      .catch(e => {
        if (!cancelled) console.error('Failed to fetch graph data:', e)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => { cancelled = true }
  }, [fetchGraphData, memoryLimit])

  const forceData = useMemo(() => {
    if (!graphData) return { nodes: [], links: [] }
    return buildForceData(graphData)
  }, [graphData])

  // Configure forces after data loads
  useEffect(() => {
    const fg = fgRef.current
    if (!fg) return
    fg.d3Force('charge')?.strength(-200)
    fg.d3Force('link')?.distance(120)
    fg.d3Force('center')?.strength(0.03)
  }, [forceData])

  // Zoom to fit on first load
  const hasZoomedRef = useRef(false)
  const zoomTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => {
    if (forceData.nodes.length > 0 && !hasZoomedRef.current) {
      hasZoomedRef.current = true
      // Wait for simulation to settle a bit
      zoomTimeoutRef.current = setTimeout(() => fgRef.current?.zoomToFit(400, 40), 500)
    }
    return () => { if (zoomTimeoutRef.current) clearTimeout(zoomTimeoutRef.current) }
  }, [forceData.nodes.length])

  const handleNodeClick = useCallback((node: any) => {  
    if (node.memory) onSelect(node.memory)
  }, [onSelect])

  // Custom node rendering on canvas
  const nodeCanvasObject = useCallback((node: any, ctx: CanvasRenderingContext2D, globalScale: number) => {  
    const label = node.name as string
    const color = node.color as string
    const x = node.x as number
    const y = node.y as number
    const fontSize = Math.max(10 / globalScale, 2)
    const nodeHeight = fontSize * 2.8
    const padding = fontSize * 0.6

    ctx.font = `${fontSize}px SF Mono, Menlo, monospace`
    const textWidth = ctx.measureText(label).width
    const nodeWidth = textWidth + padding * 2 + fontSize * 0.5 // room for type bar

    // Background
    const rx = x - nodeWidth / 2
    const ry = y - nodeHeight / 2
    ctx.fillStyle = 'rgba(30, 30, 35, 0.9)'
    ctx.strokeStyle = color
    ctx.lineWidth = 1.2 / globalScale
    ctx.beginPath()
    ctx.roundRect(rx, ry, nodeWidth, nodeHeight, 4 / globalScale)
    ctx.fill()
    ctx.stroke()

    // Type color bar (left edge)
    ctx.fillStyle = color
    ctx.beginPath()
    ctx.roundRect(rx, ry, 3 / globalScale, nodeHeight, [4 / globalScale, 0, 0, 4 / globalScale])
    ctx.fill()

    // Type label (small, top)
    const typeSize = Math.max(fontSize * 0.65, 1.5)
    ctx.font = `${typeSize}px SF Mono, Menlo, monospace`
    ctx.fillStyle = 'rgba(180, 180, 190, 0.8)'
    ctx.textAlign = 'left'
    ctx.textBaseline = 'top'
    ctx.fillText(
      (node.memory as GobbyMemory).memory_type.toUpperCase(),
      rx + 5 / globalScale,
      ry + 2 / globalScale
    )

    // Content label (bottom)
    ctx.font = `${fontSize}px SF Mono, Menlo, monospace`
    ctx.fillStyle = 'rgba(230, 230, 240, 0.95)'
    ctx.textAlign = 'left'
    ctx.textBaseline = 'bottom'
    ctx.fillText(label, rx + 5 / globalScale, ry + nodeHeight - 2 / globalScale)
  }, [])

  const nodePointerAreaPaint = useCallback((node: any, color: string, ctx: CanvasRenderingContext2D, globalScale: number) => {  
    const fontSize = Math.max(10 / globalScale, 2)
    const nodeHeight = fontSize * 2.8
    const padding = fontSize * 0.6
    ctx.font = `${fontSize}px SF Mono, Menlo, monospace`
    const textWidth = ctx.measureText(node.name as string).width
    const nodeWidth = textWidth + padding * 2 + fontSize * 0.5
    ctx.fillStyle = color
    ctx.fillRect(node.x - nodeWidth / 2, node.y - nodeHeight / 2, nodeWidth, nodeHeight)
  }, [])

  // Legend types
  const legendTypes = useMemo(() => {
    if (!graphData) return []
    return [...new Set(graphData.memories.map(m => m.memory_type))]
  }, [graphData])

  if (loading) {
    return (
      <div className="memory-graph-container" ref={containerRef}>
        <div className="memory-graph-empty">Loading graph data...</div>
      </div>
    )
  }

  if (!graphData || (forceData.nodes.length === 0 && forceData.links.length === 0)) {
    return (
      <div className="memory-graph-container" ref={containerRef}>
        <div className="memory-graph-empty">
          <div className="memory-empty-icon">&#x1f578;</div>
          <div>No connections found</div>
          <div className="memory-empty-hint">
            Memory crossrefs are created when related memories are detected.
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="memory-graph-container" ref={containerRef}>
      <ForceGraph2D
        ref={fgRef}
        width={dimensions.width}
        height={dimensions.height}
        graphData={forceData}
        nodeId="id"
        nodeLabel=""
        nodeCanvasObject={nodeCanvasObject}
        nodePointerAreaPaint={nodePointerAreaPaint}
        onNodeClick={handleNodeClick}
        linkSource="source"
        linkTarget="target"
        linkColor={() => 'rgba(120, 120, 140, 0.3)'}
        linkWidth={(link: any) => 0.5 + (link.value as number) * 2}  
        linkDirectionalParticles={1}
        linkDirectionalParticleSpeed={0.005}
        linkDirectionalParticleWidth={2}
        backgroundColor="rgba(0,0,0,0)"
        enableNodeDrag={true}
      />

      {/* Controls */}
      <div className="memory-graph-controls">
        <button
          className="memory-graph-ctrl-btn"
          onClick={() => fgRef.current?.zoomToFit(400, 40)}
          title="Zoom to fit"
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7" />
          </svg>
        </button>
      </div>

      {/* Info */}
      <div className="memory-graph-info">
        {forceData.nodes.length} nodes &middot; {forceData.links.length} edges
      </div>

      {/* Legend */}
      {legendTypes.length > 0 && (
        <div className="memory-graph-legend">
          {legendTypes.map(type => (
            <div key={type} className="memory-graph-legend-item">
              <span className="memory-graph-legend-dot" style={{ backgroundColor: getTypeColor(type) }} />
              {type}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
