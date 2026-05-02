import { useState, useMemo, useCallback, useRef, useEffect, useId } from 'react'
import dagre from '@dagrejs/dagre'
import type { GobbyTask } from '../../hooks/useTasks'
import { getTaskBucket, TASK_BUCKET_COLORS } from '../../lib/taskState'

interface GraphNode {
  id: string
  task: GobbyTask
  x: number
  y: number
}

interface GraphEdge {
  from: string
  to: string
}

interface ViewBox {
  x: number
  y: number
  scale: number
}

const NODE_WIDTH = 160
const NODE_HEIGHT = 36

const CONTAINER_CLS =
  'relative overflow-hidden rounded-lg border border-[var(--border)] bg-[var(--bg-primary)]'
const SVG_CLS = 'block h-[520px] w-full'
const EMPTY_CLS =
  'p-12 text-center text-[length:calc(var(--font-size-base)*0.8)] text-[var(--text-muted)]'
const CONTROLS_CLS =
  'absolute right-2 top-2 z-10 flex items-center gap-1 rounded-md border border-[var(--border)] bg-[var(--bg-secondary)] p-[3px]'
const CTRL_BTN_CLS =
  'flex h-7 w-7 cursor-pointer items-center justify-center rounded border-none bg-transparent text-[length:calc(var(--font-size-base)*0.85)] text-[var(--text-secondary)] transition-colors duration-100 hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)]'
const CTRL_LABEL_CLS =
  'min-w-9 px-1 text-center text-[length:calc(var(--font-size-base)*0.6)] text-[var(--text-muted)]'

function buildGraph(tasks: GobbyTask[]): { nodes: GraphNode[]; edges: GraphEdge[] } {
  const taskMap = new Map(tasks.map(t => [t.id, t]))
  const edges: GraphEdge[] = []

  for (const task of tasks) {
    if (task.parent_task_id && taskMap.has(task.parent_task_id)) {
      edges.push({ from: task.parent_task_id, to: task.id })
    }
  }

  const g = new dagre.graphlib.Graph()
  g.setGraph({ rankdir: 'LR', nodesep: 16, ranksep: 60, marginx: 20, marginy: 20 })
  g.setDefaultEdgeLabel(() => ({}))

  for (const task of tasks) {
    g.setNode(task.id, { width: NODE_WIDTH, height: NODE_HEIGHT })
  }
  for (const edge of edges) {
    g.setEdge(edge.from, edge.to)
  }

  dagre.layout(g)

  const nodes: GraphNode[] = []
  for (const task of tasks) {
    const nodeData = g.node(task.id)
    if (!nodeData) continue
    nodes.push({
      id: task.id,
      task,
      x: nodeData.x - NODE_WIDTH / 2,
      y: nodeData.y - NODE_HEIGHT / 2,
    })
  }

  return { nodes, edges }
}

function ArrowDefs({ markerId }: { markerId: string }) {
  return (
    <defs>
      <marker
        id={markerId}
        viewBox="0 0 10 10"
        refX="10"
        refY="5"
        markerWidth="8"
        markerHeight="8"
        orient="auto-start-reverse"
      >
        <path d="M 0 0 L 10 5 L 0 10 z" fill="var(--text-muted)" />
      </marker>
    </defs>
  )
}

interface DependencyGraphProps {
  tasks: GobbyTask[]
  onSelectTask: (id: string) => void
}

export function DependencyGraph({ tasks, onSelectTask }: DependencyGraphProps) {
  const markerId = `arrow${useId()}`
  const svgRef = useRef<SVGSVGElement>(null)
  const [view, setView] = useState<ViewBox>({ x: 0, y: 0, scale: 1 })
  const [dragging, setDragging] = useState(false)
  const dragStart = useRef<{ x: number; y: number; vx: number; vy: number } | null>(null)
  const [hoveredId, setHoveredId] = useState<string | null>(null)

  const { nodes, edges } = useMemo(() => buildGraph(tasks), [tasks])

  const nodeMap = useMemo(() => {
    const m = new Map<string, GraphNode>()
    for (const n of nodes) m.set(n.id, n)
    return m
  }, [nodes])

  const bounds = useMemo(() => {
    if (nodes.length === 0) return { width: 400, height: 300 }
    const maxX = Math.max(...nodes.map(n => n.x + NODE_WIDTH)) + 40
    const maxY = Math.max(...nodes.map(n => n.y + NODE_HEIGHT)) + 40
    return { width: maxX, height: maxY }
  }, [nodes])

  useEffect(() => {
    if (!svgRef.current || nodes.length === 0) return
    const rect = svgRef.current.getBoundingClientRect()
    const scaleX = rect.width / bounds.width
    const scaleY = rect.height / bounds.height
    const scale = Math.min(scaleX, scaleY, 1) * 0.9
    setView({
      x: (rect.width - bounds.width * scale) / 2,
      y: (rect.height - bounds.height * scale) / 2,
      scale,
    })
  }, [nodes.length, bounds.width, bounds.height])

  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    if (e.button !== 0) return
    setDragging(true)
    dragStart.current = { x: e.clientX, y: e.clientY, vx: view.x, vy: view.y }
  }, [view.x, view.y])

  const handleMouseMove = useCallback((e: React.MouseEvent) => {
    if (!dragging || !dragStart.current) return
    setView(v => ({
      ...v,
      x: dragStart.current!.vx + (e.clientX - dragStart.current!.x),
      y: dragStart.current!.vy + (e.clientY - dragStart.current!.y),
    }))
  }, [dragging])

  const handleMouseUp = useCallback(() => {
    setDragging(false)
    dragStart.current = null
  }, [])

  const handleWheel = useCallback((e: React.WheelEvent) => {
    if (!e.ctrlKey && !e.metaKey) return
    e.preventDefault()
    const delta = e.deltaY > 0 ? 0.9 : 1.1
    setView(v => {
      const newScale = Math.min(Math.max(v.scale * delta, 0.2), 3)
      const rect = svgRef.current?.getBoundingClientRect()
      if (!rect) return { ...v, scale: newScale }
      const cx = e.clientX - rect.left
      const cy = e.clientY - rect.top
      return {
        scale: newScale,
        x: cx - (cx - v.x) * (newScale / v.scale),
        y: cy - (cy - v.y) * (newScale / v.scale),
      }
    })
  }, [])

  const zoomIn = useCallback(() => {
    setView(v => ({ ...v, scale: Math.min(v.scale * 1.2, 3) }))
  }, [])

  const zoomOut = useCallback(() => {
    setView(v => ({ ...v, scale: Math.max(v.scale / 1.2, 0.2) }))
  }, [])

  const fitView = useCallback(() => {
    if (!svgRef.current) return
    const rect = svgRef.current.getBoundingClientRect()
    const scaleX = rect.width / bounds.width
    const scaleY = rect.height / bounds.height
    const scale = Math.min(scaleX, scaleY, 1) * 0.9
    setView({
      x: (rect.width - bounds.width * scale) / 2,
      y: (rect.height - bounds.height * scale) / 2,
      scale,
    })
  }, [bounds])

  if (tasks.length === 0) {
    return <div className={EMPTY_CLS}>No tasks to visualize</div>
  }

  return (
    <div className={CONTAINER_CLS}>
      <div className={CONTROLS_CLS}>
        <button className={CTRL_BTN_CLS} onClick={zoomIn} title="Zoom in" aria-label="Zoom in">+</button>
        <button className={CTRL_BTN_CLS} onClick={zoomOut} title="Zoom out" aria-label="Zoom out">−</button>
        <button className={CTRL_BTN_CLS} onClick={fitView} title="Fit to view" aria-label="Fit to view">⤢</button>
        <span className={CTRL_LABEL_CLS}>{Math.round(view.scale * 100)}%</span>
      </div>
      <svg
        ref={svgRef}
        className={SVG_CLS}
        onMouseDown={handleMouseDown}
        onMouseMove={handleMouseMove}
        onMouseUp={handleMouseUp}
        onMouseLeave={handleMouseUp}
        onWheel={handleWheel}
        style={{ cursor: dragging ? 'grabbing' : 'grab' }}
      >
        <ArrowDefs markerId={markerId} />
        <g transform={`translate(${view.x}, ${view.y}) scale(${view.scale})`}>
          {edges.map((edge) => {
            const from = nodeMap.get(edge.from)
            const to = nodeMap.get(edge.to)
            if (!from || !to) return null
            const x1 = from.x + NODE_WIDTH
            const y1 = from.y + NODE_HEIGHT / 2
            const x2 = to.x
            const y2 = to.y + NODE_HEIGHT / 2
            const cx1 = x1 + (x2 - x1) * 0.4
            const cx2 = x2 - (x2 - x1) * 0.4
            const isHighlighted = hoveredId === edge.from || hoveredId === edge.to
            return (
              <path
                key={`${edge.from}-${edge.to}`}
                d={`M ${x1} ${y1} C ${cx1} ${y1}, ${cx2} ${y2}, ${x2} ${y2}`}
                fill="none"
                stroke={isHighlighted ? 'var(--accent)' : 'var(--border)'}
                strokeWidth={isHighlighted ? 2 : 1.5}
                markerEnd={`url(#${markerId})`}
                opacity={isHighlighted ? 1 : 0.6}
              />
            )
          })}

          {nodes.map(node => {
            const color = TASK_BUCKET_COLORS[getTaskBucket(node.task)] || 'var(--text-muted)'
            const isHovered = hoveredId === node.id
            return (
              <g
                key={node.id}
                transform={`translate(${node.x}, ${node.y})`}
                onClick={e => { e.stopPropagation(); onSelectTask(node.id) }}
                onMouseEnter={() => setHoveredId(node.id)}
                onMouseLeave={() => setHoveredId(null)}
                style={{ cursor: 'pointer' }}
              >
                <rect
                  width={NODE_WIDTH}
                  height={NODE_HEIGHT}
                  rx={6}
                  fill="var(--bg-secondary)"
                  stroke={isHovered ? 'var(--accent)' : color}
                  strokeWidth={isHovered ? 2 : 1.5}
                />
                <rect
                  x={0}
                  y={0}
                  width={4}
                  height={NODE_HEIGHT}
                  rx={2}
                  fill={color}
                />
                <text
                  x={12}
                  y={14}
                  fontSize={10}
                  fontFamily="var(--font-mono)"
                  fill="var(--text-muted)"
                >
                  {node.task.ref}
                </text>
                <text
                  x={12}
                  y={28}
                  fontSize={11}
                  fontFamily="var(--font-sans)"
                  fill="var(--text-primary)"
                >
                  {node.task.title.length > 18
                    ? node.task.title.slice(0, 18) + '...'
                    : node.task.title}
                </text>
              </g>
            )
          })}
        </g>
      </svg>
    </div>
  )
}
