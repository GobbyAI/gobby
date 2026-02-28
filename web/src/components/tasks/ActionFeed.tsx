import { useState, useEffect, useCallback } from 'react'
import { classifyRisk, RiskDot } from './RiskBadges'
import type { RiskLevel } from './RiskBadges'
import { relativeTime } from '../../utils/formatTime'

// =============================================================================
// Types
// =============================================================================

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

// =============================================================================
// Helpers
// =============================================================================

function getBaseUrl(): string {
  return ''
}

/** Generate human-readable description from tool name and input */
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

/** Truncate result to a preview */
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

/** Check if a tool result indicates an error using JSON parsing with string fallback. */
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

// =============================================================================
// ActionFeed
// =============================================================================

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
  if (isLoading) return <div className="action-feed-loading">Loading actions...</div>
  if (error) return <div className="action-feed-error">{error}</div>
  if (actions.length === 0) return <div className="action-feed-empty">No tool calls recorded</div>

  return (
    <div className="action-feed">
      {actions.map((action, i) => (
        <button
          key={`${action.timestamp}-${i}`}
          className={`action-feed-item ${action.success ? '' : 'action-feed-item--error'}`}
          onClick={() => action.resultPreview && toggle(i)}
          aria-expanded={action.resultPreview ? expanded.has(i) : undefined}
        >
          <span className={`action-feed-dot ${action.success ? 'action-feed-dot--success' : 'action-feed-dot--error'}`} />
          <span className="action-feed-desc">{action.description}</span>
          <RiskDot level={action.riskLevel} />
          <span className="action-feed-time">{relativeTime(action.timestamp)}</span>
          {expanded.has(i) && action.resultPreview && (
            <span className="action-feed-result">{action.resultPreview}</span>
          )}
        </button>
      ))}
    </div>
  )
}
