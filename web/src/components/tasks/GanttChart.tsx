import { useState, useMemo, useCallback, useRef } from 'react'
import type { GobbyTask } from '../../hooks/useTasks'
import { getTaskDisplayState, TASK_STATE_COLORS } from '../../lib/taskState'
import { cn } from '../../lib/utils'

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

const ROW_HEIGHT = 32
const ROW_GAP = 4
const HEADER_HEIGHT = 40
const LABEL_WIDTH = 180

const WRAPPER_CLS = 'flex flex-col gap-2'
const TOOLBAR_CLS = 'flex items-center gap-1.5 py-1.5'
const TOOLBAR_LABEL_CLS = 'text-[length:var(--text-sm)] text-[var(--text-muted)]'
const ZOOM_BTN_CLS =
  'cursor-pointer rounded border border-[var(--border)] bg-[var(--bg-secondary)] px-2.5 py-[3px] font-[inherit] text-[length:var(--text-xs)] text-[var(--text-secondary)] pointer-coarse:min-h-11'
const ZOOM_BTN_ACTIVE_CLS =
  'border-[color-mix(in_srgb,var(--color-info)_30%,transparent)] bg-[color-mix(in_srgb,var(--color-info)_15%,transparent)] text-[var(--color-info)]'
const TASK_COUNT_CLS = 'ml-auto text-[length:var(--text-xs)] text-[var(--text-muted)]'

const SCROLL_CLS = 'max-h-[600px] overflow-auto rounded-md border border-[var(--border)] bg-[var(--bg-primary)]'
const SVG_CLS = 'block'

const HEADER_TEXT_CLS = 'font-[inherit] text-[length:var(--text-2xs)] [fill:var(--text-muted)]'
const ROW_LABEL_CLS = 'font-[inherit] text-[length:var(--text-xs)] [fill:var(--text-secondary)] hover:[fill:var(--text-primary)]'
const DRAG_TOOLTIP_TEXT_CLS = 'font-[inherit] text-[length:var(--text-2xs)] [fill:var(--text-primary)]'
const BAR_CLS = 'opacity-85 transition-opacity duration-150 hover:opacity-100'
const BAR_DRAGGING_CLS = '!opacity-100 [filter:brightness(1.2)] [stroke:var(--text-primary)] [stroke-width:1]'
const MILESTONE_CLS = 'opacity-90 [stroke:var(--bg-primary)] [stroke-width:1]'
const DEP_ARROW_CLS = 'fill-none opacity-50 [stroke:var(--text-muted)] [stroke-width:1]'
const ARROWHEAD_FILL_CLS = 'opacity-50 [fill:var(--text-muted)]'
const SNAP_GUIDE_CLS = '[stroke:color-mix(in_srgb,var(--color-info)_40%,transparent)] [stroke-dasharray:4_3] [stroke-width:1]'
const GRID_LINE_CLS = '[stroke:var(--border)] [stroke-width:0.5]'
const GRID_LINE_TODAY_CLS = '[stroke:var(--color-info)] [stroke-width:1.5]'
const HEADER_BG_CLS = '[fill:var(--bg-secondary)]'
const TODAY_BG_CLS = '[fill:color-mix(in_srgb,var(--color-info)_4%,transparent)]'
const ROW_STRIPE_CLS = '[fill:color-mix(in_srgb,var(--bg-secondary)_45%,transparent)]'

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

function buildBars(tasks: GobbyTask[]): TaskBar[] {
  const sorted = [...tasks].sort((a, b) =>
    new Date(a.created_at).getTime() - new Date(b.created_at).getTime()
  )

  return sorted.map((task, i) => {
    const startDate = startOfDay(new Date(task.start_date ?? task.created_at))
    const rawEnd = task.due_date ?? task.updated_at
    const endDate_ = startOfDay(new Date(rawEnd))
    const endDate = endDate_ > startDate ? endDate_ : addDays(startDate, 1)
    const isMilestone = task.task_type === 'epic'

    return { task, startDate, endDate, row: i, isMilestone }
  })
}

function buildArrows(bars: TaskBar[], tasks: GobbyTask[]): DepArrow[] {
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
    const start = addDays(min, -1)
    const end = addDays(max, 2)
    return { timelineStart: start, timelineEnd: end, totalDays: daysBetween(start, end) }
  }, [bars])

  const colWidth = getColumnWidth(zoom)

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

  const dateToX = (d: Date): number => {
    const days = daysBetween(timelineStart, d)
    const frac = days / (totalDays || 1)
    return LABEL_WIDTH + frac * (columns.length * colWidth)
  }

  const rowToY = (row: number): number => {
    return HEADER_HEIGHT + row * (ROW_HEIGHT + ROW_GAP)
  }

  const xToDate = useCallback((x: number): Date => {
    const timelineWidth = columns.length * colWidth
    const frac = (x - LABEL_WIDTH) / (timelineWidth || 1)
    const days = Math.round(frac * totalDays)
    return addDays(timelineStart, days)
  }, [columns.length, colWidth, totalDays, timelineStart])

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
    <div className={WRAPPER_CLS}>
      <div className={TOOLBAR_CLS}>
        <span className={TOOLBAR_LABEL_CLS}>Zoom:</span>
        {(['day', 'week', 'month'] as const).map(level => (
          <button
            key={level}
            className={cn(ZOOM_BTN_CLS, zoom === level && ZOOM_BTN_ACTIVE_CLS)}
            onClick={() => setZoom(level)}
          >
            {level.charAt(0).toUpperCase() + level.slice(1)}
          </button>
        ))}
        <span className={TASK_COUNT_CLS}>{tasks.length} tasks</span>
      </div>

      <div className={SCROLL_CLS}>
        <svg
          ref={svgRef}
          width={svgWidth}
          height={svgHeight}
          className={SVG_CLS}
          onMouseMove={handleDragMove}
          onMouseUp={handleDragEnd}
          onMouseLeave={handleDragEnd}
        >
          <rect x={0} y={0} width={svgWidth} height={HEADER_HEIGHT} className={HEADER_BG_CLS} />

          {columns.map((col, i) => {
            const x = LABEL_WIDTH + i * colWidth
            const isToday = startOfDay(new Date()).getTime() === startOfDay(col).getTime()
            return (
              <g key={i}>
                <line
                  x1={x} y1={HEADER_HEIGHT} x2={x} y2={svgHeight}
                  className={isToday ? GRID_LINE_TODAY_CLS : GRID_LINE_CLS}
                />
                <text
                  x={x + colWidth / 2} y={HEADER_HEIGHT - 10}
                  className={HEADER_TEXT_CLS}
                  textAnchor="middle"
                >
                  {formatHeaderDate(col, zoom)}
                </text>
                {isToday && (
                  <rect x={x} y={HEADER_HEIGHT} width={colWidth} height={svgHeight} className={TODAY_BG_CLS} />
                )}
              </g>
            )
          })}

          {bars.map(bar => {
            const y = rowToY(bar.row)
            return (
              <g key={`label-${bar.task.id}`}>
                {bar.row % 2 === 0 && (
                  <rect x={0} y={y} width={svgWidth} height={ROW_HEIGHT} className={ROW_STRIPE_CLS} />
                )}
                <text
                  x={8} y={y + ROW_HEIGHT / 2 + 4}
                  className={ROW_LABEL_CLS}
                  onClick={() => onSelectTask(bar.task.id)}
                  style={{ cursor: 'pointer' }}
                >
                  {bar.task.ref} {bar.task.title.length > 18 ? bar.task.title.slice(0, 18) + '...' : bar.task.title}
                </text>
              </g>
            )
          })}

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
                className={DEP_ARROW_CLS}
                markerEnd="url(#gantt-arrowhead)"
              />
            )
          })}

          {bars.map(bar => {
            const isDragging = drag?.taskId === bar.task.id
            const dragOffsetPx = isDragging
              ? dateToX(addDays(bar.startDate, drag!.currentOffsetDays)) - dateToX(bar.startDate)
              : 0
            const x = dateToX(bar.startDate) + dragOffsetPx
            const w = Math.max(dateToX(bar.endDate) - dateToX(bar.startDate), 8)
            const y = rowToY(bar.row)
            const displayState = getTaskDisplayState(bar.task)
            const color = TASK_STATE_COLORS[displayState] || 'var(--text-muted)'

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
                    style={{ fill: color }}
                    className={MILESTONE_CLS}
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
                {isDragging && (
                  <line
                    x1={x} y1={HEADER_HEIGHT} x2={x} y2={svgHeight}
                    className={SNAP_GUIDE_CLS}
                  />
                )}
                <rect
                  x={x} y={y + 4} width={w} height={ROW_HEIGHT - 8}
                  rx={3} ry={3}
                  style={{ fill: color }}
                  className={cn(BAR_CLS, isDragging && BAR_DRAGGING_CLS)}
                />
                {(displayState === 'closed' || displayState === 'review_approved') && (
                  <rect
                    x={x} y={y + 4} width={w} height={ROW_HEIGHT - 8}
                    rx={3} ry={3}
                    style={{ fill: color, opacity: 0.3 }}
                  />
                )}
                {isDragging && drag!.snappedDate && (
                  <g>
                    <rect
                      x={x} y={y - 20} width={80} height={18}
                      rx={3} fill="var(--bg-secondary)" stroke="var(--border)"
                    />
                    <text
                      x={x + 40} y={y - 7}
                      textAnchor="middle"
                      className={DRAG_TOOLTIP_TEXT_CLS}
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

          <defs>
            <marker id="gantt-arrowhead" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto">
              <polygon points="0 0, 8 3, 0 6" className={ARROWHEAD_FILL_CLS} />
            </marker>
          </defs>
        </svg>
      </div>
    </div>
  )
}
