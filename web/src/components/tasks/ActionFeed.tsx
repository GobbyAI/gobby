import { useState, useEffect, useCallback } from 'react'
import { RiskDot } from './RiskBadges'
import { classifyRisk, type RiskLevel } from './riskUtils'
import { relativeTime } from '../../utils/formatTime'

interface SessionMessage {
  tool_name: string | null
  tool_input: string | null
  tool_result: string | null
  content: string | null
  content_type: string | null
  role: string
  timestamp: string
}

interface ActionEntry {
  toolName: string
  description: string
  resultPreview: string | null
  success: boolean
  timestamp: string
  riskLevel: RiskLevel
}

const ROOT_CLS = 'flex max-h-[280px] flex-col gap-0.5 overflow-y-auto'
const STATE_CLS = 'py-2 text-[length:var(--text-sm)] text-[var(--text-muted)]'
const ITEM_CLS =
  'flex w-full cursor-pointer flex-wrap items-center gap-1.5 rounded border-none bg-transparent px-2 py-[5px] text-left text-[length:var(--text-sm)] text-[var(--text-secondary)] transition-colors duration-100 hover:bg-[var(--bg-tertiary)]'
const ITEM_ERROR_CLS = 'text-[var(--color-error)]'
const DOT_CLS = 'h-1.5 w-1.5 shrink-0 rounded-full'
const DOT_SUCCESS_CLS = 'bg-[var(--color-success-foreground)]'
const DOT_ERROR_CLS = 'bg-[var(--color-error)]'
const DESC_CLS = 'min-w-0 flex-1 overflow-hidden text-ellipsis whitespace-nowrap'
const TIME_CLS =
  'whitespace-nowrap font-[inherit] text-[length:var(--text-2xs)] text-[var(--text-muted)]'
const RESULT_CLS =
  'mt-0.5 w-full whitespace-pre-wrap break-all rounded border border-[var(--border)] bg-[var(--bg-primary)] px-2 py-1.5 font-[inherit] text-[length:var(--text-xs)] leading-[1.4] text-[var(--text-muted)]'

function getBaseUrl(): string {
  return ''
}

function describeAction(toolName: string, inputStr: string | null): string {
  const DESCRIPTIONS: Record<string, string> = {
    read_file: 'Read file',
    write_file: 'Write file',
    edit_file: 'Edit file',
    bash: 'Run command',
    search: 'Search codebase',
    glob: 'Find files',
    grep: 'Search content',
    list_directory: 'List directory',
    create_task: 'Create task',
    update_task: 'Update task',
    close_task: 'Close task',
    claim_task: 'Claim task',
    get_task: 'Get task details',
    suggest_next_task: 'Get next task',
    create_memory: 'Store memory',
    search_memories: 'Search memories',
  }

  const base = DESCRIPTIONS[toolName] || toolName.replace(/_/g, ' ')

  if (!inputStr) return base
  try {
    const input = JSON.parse(inputStr)
    if (input.path || input.file_path) return `${base}: ${(input.path || input.file_path).split('/').pop()}`
    if (input.command) return `${base}: ${input.command.slice(0, 60)}`
    if (input.query) return `${base}: "${input.query.slice(0, 40)}"`
    if (input.title) return `${base}: ${input.title.slice(0, 50)}`
    if (input.task_id) return `${base}: ${input.task_id}`
  } catch {
    // ignore
  }
  return base
}

function previewResult(resultStr: string | null): string | null {
  if (!resultStr) return null
  try {
    const result = JSON.parse(resultStr)
    const text = typeof result === 'string' ? result : JSON.stringify(result)
    return text.length > 120 ? text.slice(0, 120) + '...' : text
  } catch {
    return resultStr.length > 120 ? resultStr.slice(0, 120) + '...' : resultStr
  }
}

function isErrorResult(resultStr: string | null): boolean {
  if (!resultStr) return false
  try {
    const parsed = JSON.parse(resultStr)
    if (typeof parsed === 'object' && parsed !== null) {
      return 'error' in parsed || parsed.success === false
    }
  } catch {
    // Not JSON — fall back to string check
  }
  return resultStr.includes('"error"')
}

function toActions(messages: SessionMessage[]): ActionEntry[] {
  return messages
    .filter(m => m.tool_name)
    .map(m => ({
      toolName: m.tool_name!,
      description: describeAction(m.tool_name!, m.tool_input),
      resultPreview: previewResult(m.tool_result),
      success: !isErrorResult(m.tool_result),
      timestamp: m.timestamp,
      riskLevel: classifyRisk(m.tool_name!, m.tool_input),
    }))
}

interface ActionFeedProps {
  sessionId: string | null
}

export function ActionFeed({ sessionId }: ActionFeedProps) {
  const [actions, setActions] = useState<ActionEntry[]>([])
  const [isLoading, setIsLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [expanded, setExpanded] = useState<Set<number>>(new Set())

  const fetchActions = useCallback(async (signal?: AbortSignal) => {
    if (!sessionId) return
    setIsLoading(true)
    setError(null)
    try {
      const baseUrl = getBaseUrl()
      const response = await fetch(
        `${baseUrl}/api/sessions/${encodeURIComponent(sessionId)}/messages?limit=200`,
        { signal }
      )
      if (response.ok) {
        const data = await response.json()
        const messages: SessionMessage[] = data.messages || []
        setActions(toActions(messages))
      } else {
        throw new Error(`Failed to fetch actions: ${response.statusText}`)
      }
    } catch (e) {
      if (e instanceof DOMException && e.name === 'AbortError') return
      console.error('Failed to fetch session messages:', e)
      setError('Failed to load actions')
    } finally {
      setIsLoading(false)
    }
  }, [sessionId])

  useEffect(() => {
    const controller = new AbortController()
    fetchActions(controller.signal)
    return () => controller.abort()
  }, [fetchActions])

  const toggle = (idx: number) => {
    setExpanded(prev => {
      const next = new Set(prev)
      if (next.has(idx)) next.delete(idx)
      else next.add(idx)
      return next
    })
  }

  if (!sessionId) return null
  if (isLoading) return <div className={STATE_CLS}>Loading actions...</div>
  if (error) return <div className={STATE_CLS}>{error}</div>
  if (actions.length === 0) return <div className={STATE_CLS}>No tool calls recorded</div>

  return (
    <div className={ROOT_CLS}>
      {actions.map((action, i) => (
        <button
          key={`${action.timestamp}-${i}`}
          className={action.success ? ITEM_CLS : `${ITEM_CLS} ${ITEM_ERROR_CLS}`}
          onClick={() => action.resultPreview && toggle(i)}
          aria-expanded={action.resultPreview ? expanded.has(i) : undefined}
        >
          <span className={`${DOT_CLS} ${action.success ? DOT_SUCCESS_CLS : DOT_ERROR_CLS}`} />
          <span className={DESC_CLS}>{action.description}</span>
          <RiskDot level={action.riskLevel} />
          <span className={TIME_CLS}>{relativeTime(action.timestamp)}</span>
          {expanded.has(i) && action.resultPreview && (
            <span className={RESULT_CLS}>{action.resultPreview}</span>
          )}
        </button>
      ))}
    </div>
  )
}
