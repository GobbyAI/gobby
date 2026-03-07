import { useState, useMemo, useCallback, useRef } from 'react'
import type { GobbyTask } from '../../hooks/useTasks'

// =============================================================================
// Types
// =============================================================================

type ZoomLevel = 'day' | 'week' | 'month'

interface TaskBar {
  task: GobbyTask
  startDate: Date
  endDate: Date
  row: number
  isMilestone: boolean
}

interface DepArrow {
  from: TaskBar
  to: TaskBar
}

// =============================================================================
// Helpers
// =============================================================================

const STATUS_COLORS: Record<string, string> = {
  open: '#737373',
  in_progress: '#3b82f6',
  needs_review: '#f59e0b',
  review_approved: '#22c55e',
  closed: '#16a34a',
  escalated: '#f97316',
}

const ROW_HEIGHT = 32
const ROW_GAP = 4
const HEADER_HEIGHT = 40
const LABEL_WIDTH = 180

function daysBetween(a: Date, b: Date): number {
  return Math.ceil((b.getTime() - a.getTime()) / (1000 * 60 * 60 * 24))
}

function addDays(d: Date, n: number): Date {
  const r = new Date(d)
  r.setDate(r.getDate() + n)
  return r
}

function startOfDay(d: Date): Date {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate())
}

function formatHeaderDate(d: Date, zoom: ZoomLevel): string {
  if (zoom === 'day') {
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
  } else if (zoom === 'week') {
    return `W${getWeekNumber(d)} ${d.toLocaleDateString(undefined, { month: 'short' })}`
  } else {
    return d.toLocaleDateString(undefined, { month: 'short', year: '2-digit' })
  }
}

function getWeekNumber(d: Date): number {
  const start = new Date(d.getFullYear(), 0, 1)
  const diff = d.getTime() - start.getTime()
  return Math.ceil((diff / (1000 * 60 * 60 * 24) + start.getDay() + 1) / 7)
}

function getColumnWidth(zoom: ZoomLevel): number {
  if (zoom === 'day') return 28
  if (zoom === 'week') return 56
  return 80
}

// =============================================================================
// Build bars and arrows from tasks
// =============================================================================

function buildBars(tasks: GobbyTask[]): TaskBar[] {
  // Sort by created_at for row assignment
  const sorted = [...tasks].sort((a, b) =>
    new Date(a.created_at).getTime() - new Date(b.created_at).getTime()
  )

  return sorted.map((task, i) => {
    const startDate = startOfDay(new Date(task.start_date ?? task.created_at))
    const rawEnd = task.due_date ?? task.updated_at
    const endDate_ = startOfDay(new Date(rawEnd))
    // If task has no duration (start == end), show at least 1 day
    const endDate = endDate_ > startDate ? endDate_ : addDays(startDate, 1)
    const isMilestone = task.task_type === 'epic'

    return { task, startDate, endDate, row: i, isMilestone }
  })
}

function buildArrows(bars: TaskBar[], tasks: GobbyTask[]): DepArrow[] {
  // Build parent→child arrows (parent_task_id relationships)
  const barMap = new Map<string, TaskBar>()
  for (const bar of bars) {
    barMap.set(bar.task.id, bar)
  }

  const arrows: DepArrow[] = []
  for (const task of tasks) {
    if (task.parent_task_id && barMap.has(task.parent_task_id) && barMap.has(task.id)) {
      arrows.push({
        from: barMap.get(task.parent_task_id)!,
        to: barMap.get(task.id)!,
      })
    }
  }
  return arrows
}

// =============================================================================
// GanttChart
// =============================================================================

interface DragState {
  taskId: string
  barIndex: number
  startMouseX: number
  originalBarX: number
  originalBarW: number
  currentOffsetDays: number
  snappedDate: Date | null
}

interface GanttChartProps {
  tasks: GobbyTask[]
  onSelectTask: (id: string) => void
  onReschedule?: (taskId: string, offsetDays: number) => void
}

export function GanttChart({ tasks, onSelectTask, onReschedule }: GanttChartProps) {
  const [zoom, setZoom] = useState<ZoomLevel>('day')
  const [drag, setDrag] = useState<DragState | null>(null)
  const svgRef = useRef<SVGSVGElement>(null)

  const bars = useMemo(() => buildBars(tasks), [tasks])
  const arrows = useMemo(() => buildArrows(bars, tasks), [bars, tasks])

  // Compute timeline range
  const { timelineStart, timelineEnd, totalDays } = useMemo(() => {
    if (bars.length === 0) {
      const now = startOfDay(new Date())
      return { timelineStart: now, timelineEnd: addDays(now, 14), totalDays: 14 }
    }
    let min = bars[0].startDate
    let max = bars[0].endDate
    for (const b of bars) {
      if (b.startDate < min) min = b.startDate
      if (b.endDate > max) max = b.endDate
    }
    // Add padding
    const start = addDays(min, -1)
    const end = addDays(max, 2)
    return { timelineStart: start, timelineEnd: end, totalDays: daysBetween(start, end) }
  }, [bars])

  const colWidth = getColumnWidth(zoom)

  // Generate date columns
  const columns = useMemo(() => {
    const cols: Date[] = []
    let step = 1
    if (zoom === 'week') step = 7
    if (zoom === 'month') step = 30

    let d = new Date(timelineStart)
    while (d <= timelineEnd) {
      cols.push(new Date(d))
      d = addDays(d, step)
    }
    return cols
  }, [timelineStart, timelineEnd, zoom])

  const svgWidth = LABEL_WIDTH + columns.length * colWidth
  const svgHeight = HEADER_HEIGHT + bars.length * (ROW_HEIGHT + ROW_GAP) + 20

  // Position helpers
  const dateToX = (d: Date): number => {
    const days = daysBetween(timelineStart, d)
    const frac = days / (totalDays || 1)
    return LABEL_WIDTH + frac * (columns.length * colWidth)
  }

  const rowToY = (row: number): number => {
    return HEADER_HEIGHT + row * (ROW_HEIGHT + ROW_GAP)
  }

  // Inverse: X position to date
  const xToDate = useCallback((x: number): Date => {
    const timelineWidth = columns.length * colWidth
    const frac = (x - LABEL_WIDTH) / (timelineWidth || 1)
    const days = Math.round(frac * totalDays)
    return addDays(timelineStart, days)
  }, [columns.length, colWidth, totalDays, timelineStart])

  // Drag handlers
  const handleDragStart = useCallback((e: React.MouseEvent, bar: TaskBar, barX: number, barW: number) => {
    if (bar.isMilestone) return
    e.stopPropagation()
    e.preventDefault()
    setDrag({
      taskId: bar.task.id,
      barIndex: bar.row,
      startMouseX: e.clientX,
      originalBarX: barX,
      originalBarW: barW,
      currentOffsetDays: 0,
      snappedDate: null,
    })
  }, [])

  const handleDragMove = useCallback((e: React.MouseEvent) => {
    if (!drag) return
    const dx = e.clientX - drag.startMouseX
    const newX = drag.originalBarX + dx
    const snapped = xToDate(newX)
    const origDate = xToDate(drag.originalBarX)
    const offsetDays = daysBetween(origDate, snapped)

    setDrag(prev => prev ? { ...prev, currentOffsetDays: offsetDays, snappedDate: snapped } : null)
  }, [drag, xToDate])

  const handleDragEnd = useCallback(() => {
    if (!drag) return
    if (drag.currentOffsetDays !== 0 && onReschedule) {
      onReschedule(drag.taskId, drag.currentOffsetDays)
    }
    setDrag(null)
  }, [drag, onReschedule])

  return (
    <div className="gantt-wrapper">
      {/* Zoom controls */}
      <div className="gantt-toolbar">
        <span className="gantt-toolbar-label">Zoom:</span>
        {(['day', 'week', 'month'] as const).map(level => (
          <button
            key={level}
            className={`gantt-zoom-btn ${zoom === level ? 'active' : ''}`}
            onClick={() => setZoom(level)}
          >
            {level.charAt(0).toUpperCase() + level.slice(1)}
          </button>
        ))}
        <span className="gantt-task-count">{tasks.length} tasks</span>
      </div>

      <div className="gantt-scroll">
        <svg
          ref={svgRef}
          width={svgWidth}
          height={svgHeight}
          className="gantt-svg"
          onMouseMove={handleDragMove}
          onMouseUp={handleDragEnd}
          onMouseLeave={handleDragEnd}
        >
          {/* Header background */}
          <rect x={0} y={0} width={svgWidth} height={HEADER_HEIGHT} className="gantt-header-bg" />

          {/* Column grid lines and headers */}
          {columns.map((col, i) => {
            const x = LABEL_WIDTH + i * colWidth
            const isToday = startOfDay(new Date()).getTime() === startOfDay(col).getTime()
            return (
              <g key={i}>
                <line
                  x1={x} y1={HEADER_HEIGHT} x2={x} y2={svgHeight}
                  className={`gantt-grid-line ${isToday ? 'gantt-grid-line--today' : ''}`}
                />
                <text
                  x={x + colWidth / 2} y={HEADER_HEIGHT - 10}
                  className="gantt-header-text"
                  textAnchor="middle"
                >
                  {formatHeaderDate(col, zoom)}
                </text>
                {isToday && (
                  <rect x={x} y={HEADER_HEIGHT} width={colWidth} height={svgHeight} className="gantt-today-bg" />
                )}
              </g>
            )
          })}

          {/* Row labels */}
          {bars.map(bar => {
            const y = rowToY(bar.row)
            return (
              <g key={`label-${bar.task.id}`}>
                {/* Row stripe */}
                {bar.row % 2 === 0 && (
                  <rect x={0} y={y} width={svgWidth} height={ROW_HEIGHT} className="gantt-row-stripe" />
                )}
                <text
                  x={8} y={y + ROW_HEIGHT / 2 + 4}
                  className="gantt-row-label"
                  onClick={() => onSelectTask(bar.task.id)}
                  style={{ cursor: 'pointer' }}
                >
                  {bar.task.ref} {bar.task.title.length > 18 ? bar.task.title.slice(0, 18) + '...' : bar.task.title}
                </text>
              </g>
            )
          })}

          {/* Dependency arrows */}
          {arrows.map((arrow, i) => {
            const fromX = dateToX(arrow.from.endDate)
            const fromY = rowToY(arrow.from.row) + ROW_HEIGHT / 2
            const toX = dateToX(arrow.to.startDate)
            const toY = rowToY(arrow.to.row) + ROW_HEIGHT / 2
            const midX = (fromX + toX) / 2

            return (
              <path
                key={`arrow-${i}`}
                d={`M ${fromX} ${fromY} C ${midX} ${fromY}, ${midX} ${toY}, ${toX} ${toY}`}
                className="gantt-dep-arrow"
                markerEnd="url(#gantt-arrowhead)"
              />
            )
          })}

          {/* Task bars */}
          {bars.map(bar => {
            const isDragging = drag?.taskId === bar.task.id
            const dragOffsetPx = isDragging
              ? dateToX(addDays(bar.startDate, drag!.currentOffsetDays)) - dateToX(bar.startDate)
              : 0
            const x = dateToX(bar.startDate) + dragOffsetPx
            const w = Math.max(dateToX(bar.endDate) - dateToX(bar.startDate), 8)
            const y = rowToY(bar.row)
            const color = STATUS_COLORS[bar.task.status] || '#737373'

            if (bar.isMilestone) {
              const cx = x + w / 2
              const cy = y + ROW_HEIGHT / 2
              const size = 8
              return (
                <g
                  key={`bar-${bar.task.id}`}
                  onClick={() => onSelectTask(bar.task.id)}
                  onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSelectTask(bar.task.id) } }}
                  tabIndex={0}
                  role="button"
                  aria-label={`Milestone: ${bar.task.ref} ${bar.task.title}`}
                  style={{ cursor: 'pointer' }}
                >
                  <polygon
                    points={`${cx},${cy - size} ${cx + size},${cy} ${cx},${cy + size} ${cx - size},${cy}`}
                    fill={color}
                    className="gantt-milestone"
                  />
                </g>
              )
            }

            return (
              <g
                key={`bar-${bar.task.id}`}
                style={{ cursor: isDragging ? 'grabbing' : 'grab' }}
                onClick={() => { if (!isDragging) onSelectTask(bar.task.id) }}
                onMouseDown={(e) => handleDragStart(e, bar, dateToX(bar.startDate), w)}
              >
                {/* Snap guide line during drag */}
                {isDragging && (
                  <line
                    x1={x} y1={HEADER_HEIGHT} x2={x} y2={svgHeight}
                    className="gantt-snap-guide"
                  />
                )}
                <rect
                  x={x} y={y + 4} width={w} height={ROW_HEIGHT - 8}
                  rx={3} ry={3}
                  fill={color}
                  className={`gantt-bar ${isDragging ? 'gantt-bar--dragging' : ''}`}
                />
                {(bar.task.status === 'closed' || bar.task.status === 'review_approved') && (
                  <rect
                    x={x} y={y + 4} width={w} height={ROW_HEIGHT - 8}
                    rx={3} ry={3}
                    fill={color}
                    opacity={0.3}
                    className="gantt-bar-complete"
                  />
                )}
                {/* Date tooltip during drag */}
                {isDragging && drag!.snappedDate && (
                  <g>
                    <rect
                      x={x} y={y - 20} width={80} height={18}
                      rx={3} fill="var(--bg-secondary)" stroke="var(--border)"
                    />
                    <text
                      x={x + 40} y={y - 7}
                      textAnchor="middle"
                      className="gantt-drag-tooltip-text"
                    >
                      {drag!.snappedDate.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}
                      {drag!.currentOffsetDays !== 0 && (
                        ` (${drag!.currentOffsetDays > 0 ? '+' : ''}${drag!.currentOffsetDays}d)`
                      )}
                    </text>
                  </g>
                )}
                <title>{bar.task.ref}: {bar.task.title}</title>
              </g>
            )
          })}

          {/* Arrow marker definition */}
          <defs>
            <marker id="gantt-arrowhead" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto">
              <polygon points="0 0, 8 3, 0 6" className="gantt-arrowhead-fill" />
            </marker>
          </defs>
        </svg>
      </div>
    </div>
  )
}
