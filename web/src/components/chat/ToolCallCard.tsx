import React, { memo, useCallback, useMemo, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter'
import { oneDark } from 'react-syntax-highlighter/dist/esm/styles/prism'
import type { ToolCall, ToolResult } from '../../types/chat'
import type { ArtifactType } from '../../types/artifacts'
import { cn } from '../../lib/utils'
import { Badge } from './ui/Badge'
import { Button } from './ui/Button'
import type { A2UISurfaceState, UserAction } from '../canvas'
import { A2UIRenderer } from '../canvas'
import { useArtifactContext } from './artifacts/ArtifactContext'
import {
  buildChainSummary,
  COMPACT_HEADER_NAMES,
  COMPACT_HEADER_TOOL_TYPES,
  computeLineDiff,
  extractBase64Image,
  extractResultContent,
  extractResultMetadata,
  FILE_TOOL_TYPES,
  formatToolName,
  getLanguageFromPath,
  getToolSummary,
  groupToolCalls,
  parseGrepOutput,
  parseReadOutput,
  pathBasename,
  resolveToolType,
  type ToolCallGroup,
} from './ToolCallCard.helpers'

interface ToolCallCardProps {
  toolCalls: ToolCall[]
  onRespond?: (toolCallId: string, answers: Record<string, string>) => boolean | void
  onRespondToApproval?: (toolCallId: string, decision: 'approve' | 'reject' | 'approve_always') => boolean | void
  canvasSurfaces?: Map<string, A2UISurfaceState>
  onCanvasInteraction?: (canvasId: string, action: UserAction) => void
}

interface AskUserOption {
  label: string
  description: string
}

interface AskUserQuestionItem {
  question: string
  header: string
  options: AskUserOption[]
  multiSelect: boolean
}

const highlighterTheme = {
  ...oneDark,
  'pre[class*="language-"]': {
    ...oneDark['pre[class*="language-"]'],
    background: '#0d0d0d',
    margin: '0',
    padding: '0.75rem',
    fontSize: '0.75rem',
  },
  'code[class*="language-"]': {
    ...oneDark['code[class*="language-"]'],
    background: 'transparent',
    fontFamily: "'SF Mono', 'Fira Code', 'JetBrains Mono', monospace",
  },
}

function InlineDiff({ oldStr, newStr, language }: { oldStr: string; newStr: string; language: string }) {
  const diff = useMemo(() => computeLineDiff(oldStr, newStr), [oldStr, newStr])
  const content = useMemo(() => diff.map(e => {
    const prefix = e.type === 'add' ? '+' : e.type === 'remove' ? '-' : ' '
    return `${prefix} ${e.line}`
  }).join('\n'), [diff])

  const lineProps = useCallback((lineNumber: number): React.HTMLProps<HTMLElement> => {
    const entry = diff[lineNumber - 1]
    if (!entry) return { style: { display: 'block' } }
    const bg = entry.type === 'add' ? 'rgba(63, 185, 80, 0.15)'
             : entry.type === 'remove' ? 'rgba(248, 81, 73, 0.3)'
             : 'transparent'
    return { style: { background: bg, display: 'block' } }
  }, [diff])

  const diffLineNumberStyle = useCallback((lineNumber: number) => {
    const entry = diff[lineNumber - 1]
    const color = entry?.type === 'add' ? '#3fb950'
               : entry?.type === 'remove' ? '#f85149'
               : '#555'
    return { ...lineNumberStyle, color }
  }, [diff])

  return (
    <SyntaxHighlighter
      style={highlighterTheme}
      language={language}
      PreTag="div"
      showLineNumbers
      startingLineNumber={1}
      wrapLines
      lineProps={lineProps}
      lineNumberStyle={diffLineNumberStyle}
      customStyle={{ margin: 0, borderRadius: '0.25rem', maxHeight: '24rem', overflow: 'auto' }}
    >
      {content}
    </SyntaxHighlighter>
  )
}

const lineNumberStyle = {
  minWidth: '2.5em',
  paddingRight: '1em',
  textAlign: 'right' as const,
  userSelect: 'none' as const,
  color: '#555',
}

function ToolArgumentsContent({ args }: { args: Record<string, unknown> }) {
  const filePath = args.file_path as string | undefined

  // Write pattern: file_path + content
  if (filePath && typeof args.content === 'string') {
    const language = getLanguageFromPath(filePath)
    return (
      <div>
        <div className="text-muted-foreground mb-1 font-medium">
          Write <span className="font-mono text-foreground">{filePath}</span>
        </div>
        <SyntaxHighlighter
          style={highlighterTheme}
          language={language}
          PreTag="div"
          showLineNumbers
          startingLineNumber={1}
          lineNumberStyle={lineNumberStyle}
          customStyle={{ margin: 0, borderRadius: '0.25rem', maxHeight: '24rem', overflow: 'auto' }}
        >
          {args.content as string}
        </SyntaxHighlighter>
      </div>
    )
  }

  // Edit pattern: file_path + old_string + new_string — unified diff
  if (filePath && typeof args.old_string === 'string' && typeof args.new_string === 'string') {
    const language = getLanguageFromPath(filePath)
    return (
      <div>
        <div className="text-muted-foreground mb-1 font-medium">
          Edit <span className="font-mono text-foreground">{filePath}</span>
        </div>
        <InlineDiff oldStr={args.old_string as string} newStr={args.new_string as string} language={language} />
      </div>
    )
  }

  // Fallback: syntax-highlighted JSON
  return (
    <div>
      <div className="text-muted-foreground mb-1 font-medium">Arguments</div>
      <SyntaxHighlighter
        style={highlighterTheme}
        language="json"
        PreTag="div"
        customStyle={{ margin: 0, borderRadius: '0.25rem', maxHeight: '24rem', overflow: 'auto' }}
      >
        {JSON.stringify(args, null, 2)}
      </SyntaxHighlighter>
    </div>
  )
}

function PanelIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="3" y="3" width="18" height="18" rx="2" ry="2" />
      <line x1="12" y1="3" x2="12" y2="21" />
    </svg>
  )
}

const IMAGE_EXTENSIONS = new Set(['png', 'jpg', 'jpeg', 'gif', 'svg', 'webp'])
const SHEET_EXTENSIONS = new Set(['csv', 'tsv'])

function getArtifactTypeForFile(filePath: string): { type: ArtifactType; language: string } {
  const ext = filePath.split('.').pop()?.toLowerCase() || ''
  if (IMAGE_EXTENSIONS.has(ext)) return { type: 'image', language: ext }
  if (SHEET_EXTENSIONS.has(ext)) return { type: 'sheet', language: ext }
  const language = getLanguageFromPath(filePath)
  if (language === 'markdown') return { type: 'text', language: 'markdown' }
  return { type: 'code', language }
}

function ToolResultContent({ call }: { call: ToolCall }) {
  const rawContent = extractResultContent(call.result)
  const metadata = extractResultMetadata(call.result)
  const toolType = resolveToolType(call)

  const imageSrc = useMemo(() => extractBase64Image(rawContent), [rawContent])

  const resultStr = useMemo(() => {
    try {
      if (typeof rawContent === 'string') {
        try {
          return JSON.stringify(JSON.parse(rawContent), null, 2)
        } catch {
          return rawContent
        }
      } else {
        return JSON.stringify(rawContent, null, 2)
      }
    } catch (e) {
      if (process.env.NODE_ENV === 'development') console.error('Failed to serialize tool call result:', e)
      return String(rawContent)
    }
  }, [rawContent])
  const filePath = call.arguments?.file_path as string | undefined

  // Base64 image — render inline
  if (imageSrc) {
    return (
      <img
        src={imageSrc}
        alt="Tool result image"
        className="max-w-full max-h-96 rounded-lg border border-border"
      />
    )
  }

  if (filePath) {
    const parsed = parseReadOutput(resultStr)
    if (parsed) {
      const language = getLanguageFromPath(filePath)
      const fileName = pathBasename(filePath)
      const lineCount = metadata?.line_count as number | undefined
      return (
        <div className="rounded overflow-hidden">
          <div className="flex items-center justify-between bg-muted/50 px-3 py-1 text-xs">
            <span className="text-muted-foreground font-mono truncate">{fileName}</span>
            {lineCount != null && (
              <span className="text-muted-foreground/60 ml-2">{lineCount} lines</span>
            )}
          </div>
          <SyntaxHighlighter
            style={highlighterTheme}
            language={language}
            PreTag="div"
            showLineNumbers
            startingLineNumber={parsed.startLine}
            lineNumberStyle={{
              minWidth: '2.5em',
              paddingRight: '1em',
              textAlign: 'right',
              userSelect: 'none',
              color: '#555',
            }}
            customStyle={{
              margin: 0,
              borderRadius: 0,
              maxHeight: '24rem',
              overflow: 'auto',
            }}
          >
            {parsed.content}
          </SyntaxHighlighter>
        </div>
      )
    }
  }

  // Grep content mode: parse file:line:content and render per-file with highlighting
  if (toolType === 'grep') {
    const groups = parseGrepOutput(resultStr)
    if (groups) {
      const matchCount = metadata?.match_count as number | undefined
      return (
        <div className="space-y-2">
          {matchCount != null && (
            <div className="text-muted-foreground/60 text-xs">{matchCount} match{matchCount !== 1 ? 'es' : ''}</div>
          )}
          {groups.map((group, i) => {
            const lang = getLanguageFromPath(group.filePath)
            const content = group.lines.map(l => l.content).join('\n')
            const startLine = group.lines[0].lineNum
            return (
              <div key={i}>
                <div className="text-muted-foreground text-xs mb-1 font-mono">{group.filePath}</div>
                <SyntaxHighlighter
                  style={highlighterTheme}
                  language={lang}
                  PreTag="div"
                  showLineNumbers
                  startingLineNumber={startLine}
                  lineNumberStyle={lineNumberStyle}
                  customStyle={{ margin: 0, borderRadius: '0.25rem', maxHeight: '24rem', overflow: 'auto' }}
                >
                  {content}
                </SyntaxHighlighter>
              </div>
            )
          })}
        </div>
      )
    }
    // files_with_matches / count mode — render as a clean file list
    const fileLines = resultStr.trim().split('\n').filter(l => l.trim())
    if (fileLines.length > 0) {
      return (
        <div className="font-mono text-xs space-y-0.5 py-1">
          {fileLines.map((f, i) => (
            <div key={i} className="text-muted-foreground">{f}</div>
          ))}
        </div>
      )
    }
  }

  // Bash results: show exit code from metadata when available
  if (toolType === 'bash' && metadata?.exit_code != null) {
    const exitCode = metadata.exit_code as number
    return (
      <div>
        {exitCode !== 0 && (
          <div className="text-destructive-foreground/70 text-xs mb-1">exit code {exitCode}</div>
        )}
        <pre className="bg-muted rounded p-2 overflow-x-auto text-foreground max-h-96 overflow-y-auto font-mono text-xs">
          {resultStr}
        </pre>
      </div>
    )
  }

  // Agent/Task results: render as markdown
  const toolName = formatToolName(call.tool_name)
  if (toolName === 'Agent' || toolName === 'Task') {
    return (
      <div className="max-h-96 overflow-y-auto text-xs p-2">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{resultStr}</ReactMarkdown>
      </div>
    )
  }

  // Detect if result looks like JSON for syntax highlighting
  const looksLikeJson = resultStr.trimStart().startsWith('{') || resultStr.trimStart().startsWith('[')

  return looksLikeJson ? (
    <SyntaxHighlighter
      style={highlighterTheme}
      language="json"
      PreTag="div"
      customStyle={{ margin: 0, borderRadius: '0.25rem', maxHeight: '24rem', overflow: 'auto' }}
    >
      {resultStr}
    </SyntaxHighlighter>
  ) : (
    <pre className="bg-muted rounded p-2 overflow-x-auto text-foreground max-h-96 overflow-y-auto font-mono text-xs">
      {resultStr}
    </pre>
  )
}

const ToolCallItem = memo(function ToolCallItem({ call, onRespond, onRespondToApproval, canvasSurfaces, onCanvasInteraction, nested = false }: { call: ToolCall; onRespond?: (toolCallId: string, answers: Record<string, string>) => boolean | void; onRespondToApproval?: (toolCallId: string, decision: 'approve' | 'reject' | 'approve_always') => boolean | void; canvasSurfaces?: Map<string, A2UISurfaceState>; onCanvasInteraction?: (canvasId: string, action: UserAction) => void; nested?: boolean }) {
  const isActive = call.status === 'calling' || call.status === 'pending_approval'
  const [expanded, setExpanded] = useState(isActive)
  const displayName = formatToolName(call.tool_name)
  const toolType = resolveToolType(call)
  const summary = useMemo(() => getToolSummary(call), [call])
  const isCompact = summary !== null && (COMPACT_HEADER_TOOL_TYPES.has(toolType) || COMPACT_HEADER_NAMES.has(displayName))
  const isFileHeader = FILE_TOOL_TYPES.has(toolType)
  const { openFileAsArtifact } = useArtifactContext()

  // Compute artifact info for Read tools to show button in toolbar
  const artifactButton = useMemo(() => {
    if (toolType !== 'read' || call.status !== 'completed' || !call.result) return null
    const filePath = call.arguments?.file_path as string | undefined
    if (!filePath) return null
    const content = extractResultContent(call.result)
    const resultStr = typeof content === 'string' ? content : String(content)
    const parsed = parseReadOutput(resultStr)
    if (!parsed) return null
    const artifactInfo = getArtifactTypeForFile(filePath)
    const fileName = pathBasename(filePath)
    return { artifactInfo, parsed, fileName }
  }, [toolType, call.status, call.result, call.arguments])

  if (call.tool_name === 'render_surface') {
    return <CanvasSurfaceCard call={call} canvasSurfaces={canvasSurfaces} onCanvasInteraction={onCanvasInteraction} />
  }

  if (call.tool_name === 'AskUserQuestion') {
    return <AskUserQuestionCard call={call} onRespond={onRespond} />
  }

  if (call.status === 'pending_approval') {
    return <ToolApprovalCard call={call} onRespondToApproval={onRespondToApproval} />
  }

  const hasDetails = call.arguments || call.result || call.error

  return (
    <div className={cn(
      nested
        ? 'border-b border-border last:border-b-0 overflow-hidden'
        : 'rounded-lg border border-border overflow-hidden my-1.5',
      call.status === 'error' && 'border-destructive-foreground/30'
    )}>
      <div
        className="flex items-center gap-2 px-3 py-1.5 text-sm cursor-pointer hover:bg-muted/50 transition-colors"
        onClick={() => hasDetails && setExpanded(!expanded)}
      >
        <StatusIcon status={call.status} />
        <span className="font-mono text-foreground">{displayName}</span>
        {summary && isFileHeader ? (
          <>
            <span className="text-muted-foreground text-xs truncate hidden sm:inline">{summary}</span>
            <span className="text-muted-foreground text-xs truncate sm:hidden">{pathBasename(summary)}</span>
          </>
        ) : summary ? (
          <span className="text-muted-foreground text-xs truncate max-w-[12rem] sm:max-w-[24rem]">{summary}</span>
        ) : null}
        <div className="flex-1" />
        {artifactButton && (
          <button
            className="flex items-center gap-1 rounded px-1.5 py-0.5 text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
            onClick={(e) => {
              e.stopPropagation()
              openFileAsArtifact(artifactButton.artifactInfo.type, artifactButton.artifactInfo.language, artifactButton.parsed.content, artifactButton.fileName)
            }}
            title="Open in artifacts panel"
          >
            <PanelIcon />
          </button>
        )}
        {hasDetails && (
          <span className="text-muted-foreground text-xs">{expanded ? '\u25BC' : '\u25B6'}</span>
        )}
      </div>
      {expanded && hasDetails && (
        <div className="border-t border-border px-3 py-2 text-xs space-y-2">
          {call.arguments && Object.keys(call.arguments).length > 0 && !isCompact && (
            <ToolArgumentsContent args={call.arguments} />
          )}
          {call.status === 'completed' && call.result !== undefined && toolType !== 'edit' && (
            <div>
              <div className="text-muted-foreground mb-1 font-medium">Result</div>
              <ToolResultContent call={call} />
            </div>
          )}
          {call.status === 'error' && call.error && (
            <div>
              <div className="text-destructive-foreground mb-1 font-medium">Error</div>
              <pre className="bg-destructive/30 rounded p-2 overflow-x-auto text-destructive-foreground">
                {call.error.replace(/<\/?tool_use_error>/g, '').trim()}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  )
})

function ToolApprovalCard({ call, onRespondToApproval }: { call: ToolCall; onRespondToApproval?: (toolCallId: string, decision: 'approve' | 'reject' | 'approve_always') => boolean | void }) {
  const displayName = formatToolName(call.tool_name)
  const isLive = onRespondToApproval && call.status === 'pending_approval'
  const [sendError, setSendError] = useState<string | null>(null)

  const handleDecision = (decision: 'approve' | 'reject' | 'approve_always') => {
    const sent = onRespondToApproval?.(call.id, decision)
    if (sent === false) {
      setSendError('Disconnected — reconnecting...')
    } else {
      setSendError(null)
    }
  }

  // Read-only: show what happened
  if (!isLive) {
    const wasApproved = call.status === 'completed'
    const wasError = call.status === 'error'
    return (
      <div className="rounded-lg border border-border/30 bg-muted/5 overflow-hidden my-1.5 opacity-75">
        <div className="flex items-center gap-2 px-3 py-2 text-sm">
          <span className="font-mono text-foreground">{displayName}</span>
          {wasApproved && <Badge variant="success">Approved</Badge>}
          {wasError && <Badge variant="error">Rejected</Badge>}
          {!wasApproved && !wasError && <Badge variant="warning">Pending</Badge>}
        </div>
        {call.arguments && Object.keys(call.arguments).length > 0 && (
          <div className="px-3 pb-2 text-xs">
            <ToolArgumentsContent args={call.arguments} />
          </div>
        )}
      </div>
    )
  }

  return (
    <div className="rounded-lg border border-warning-foreground/30 bg-warning/20 overflow-hidden my-1.5">
      <div className="flex items-center gap-2 px-3 py-2 text-sm">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-warning-foreground shrink-0">
          <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
          <line x1="12" y1="9" x2="12" y2="13" />
          <line x1="12" y1="17" x2="12.01" y2="17" />
        </svg>
        <span className="font-mono text-foreground">{displayName}</span>
        <Badge variant="warning">Approval Required</Badge>
      </div>
      {call.arguments && Object.keys(call.arguments).length > 0 && (
        <div className="px-3 pb-2 text-xs">
          <ToolArgumentsContent args={call.arguments} />
        </div>
      )}
      <div className="flex items-center gap-2 px-3 pb-2">
        <Button size="sm" variant="primary" onClick={() => handleDecision('approve')}>
          Approve
        </Button>
        <Button size="sm" variant="ghost" onClick={() => handleDecision('approve_always')}>
          Always Approve
        </Button>
        <Button size="sm" variant="destructive" onClick={() => handleDecision('reject')}>
          Reject
        </Button>
      </div>
      {sendError && (
        <div className="px-3 pb-2 text-xs text-warning-foreground">{sendError}</div>
      )}
    </div>
  )
}

/** Parse answered values from AskUserQuestion result content. */
function parseAnsweredValues(result: ToolResult | undefined): Record<string, string> | null {
  if (!result?.content) return null
  try {
    const text = typeof result.content === 'string' ? result.content : JSON.stringify(result.content)
    // The result is JSON with {answers: {question: answer}} or just {question: answer}
    const parsed = JSON.parse(text)
    if (parsed && typeof parsed === 'object') {
      return parsed.answers ?? parsed
    }
  } catch {
    // Fall back to treating content as a plain string
    if (typeof result.content === 'string' && result.content.trim()) {
      return { _raw: result.content }
    }
  }
  return null
}

function AskUserQuestionCard({ call, onRespond }: { call: ToolCall; onRespond?: (toolCallId: string, answers: Record<string, string>) => boolean | void }) {
  const args = call.arguments as { questions?: AskUserQuestionItem[] } | undefined
  const questions = args?.questions
  const [selectedOptions, setSelectedOptions] = useState<Record<number, string[]>>({})
  const [otherTexts, setOtherTexts] = useState<Record<number, string>>({})
  const [submitted, setSubmitted] = useState(false)
  const [sendError, setSendError] = useState<string | null>(null)

  if (!questions || !Array.isArray(questions)) return null

  const isLive = onRespond && call.status === 'calling'

  // Read-only mode: show what was answered
  if (!isLive) {
    const answered = parseAnsweredValues(call.result)
    return (
      <div className="rounded-lg border border-border/30 bg-muted/5 overflow-hidden my-1.5 p-3 opacity-75">
        {questions.map((q, qi) => {
          const answer = answered?.[q.question]
          const answerLabels = answer ? answer.split(', ') : []
          return (
            <div key={qi} className="mb-3 last:mb-0">
              <div className="flex items-center gap-2 mb-1.5">
                <Badge variant="info">{q.header}</Badge>
                {answer ? <Badge variant="success">Answered</Badge> : <Badge variant="default">No response</Badge>}
              </div>
              <div className="text-sm text-foreground mb-2">{q.question}</div>
              <div className="flex flex-wrap gap-1.5">
                {q.options.map((opt, oi) => {
                  const wasSelected = answerLabels.includes(opt.label)
                  return (
                    <div
                      key={oi}
                      className={cn(
                        'rounded-md border px-3 py-1.5 text-sm text-left',
                        wasSelected
                          ? 'border-accent bg-accent/20 text-foreground'
                          : 'border-border/50 text-muted-foreground/50'
                      )}
                    >
                      <div className="font-medium">{opt.label}</div>
                    </div>
                  )
                })}
              </div>
              {answer && !q.options.some((o) => answerLabels.includes(o.label)) && (
                <div className="mt-1.5 text-sm text-foreground italic">&ldquo;{answer}&rdquo;</div>
              )}
            </div>
          )
        })}
      </div>
    )
  }

  const handleOptionClick = (qi: number, label: string, multiSelect: boolean) => {
    if (submitted) return
    setSelectedOptions((prev) => {
      const current = prev[qi] || []
      if (label === '__other__') {
        if (current.includes('__other__')) return { ...prev, [qi]: current.filter((l) => l !== '__other__') }
        return multiSelect ? { ...prev, [qi]: [...current, '__other__'] } : { ...prev, [qi]: ['__other__'] }
      }
      if (multiSelect) {
        return current.includes(label)
          ? { ...prev, [qi]: current.filter((l) => l !== label) }
          : { ...prev, [qi]: [...current.filter((l) => l !== '__other__'), label] }
      }
      return { ...prev, [qi]: [label] }
    })
  }

  const handleSubmit = () => {
    if (!onRespond || submitted) return
    const answers: Record<string, string> = {}
    questions.forEach((q, qi) => {
      const selected = selectedOptions[qi] || []
      if (selected.includes('__other__')) answers[q.question] = otherTexts[qi] || ''
      else if (selected.length > 0) answers[q.question] = selected.join(', ')
    })
    const sent = onRespond(call.id, answers)
    if (sent === false) {
      setSendError('Disconnected — reconnecting...')
    } else {
      setSendError(null)
      setSubmitted(true)
    }
  }

  const hasSelection = Object.values(selectedOptions).some((s) => s.length > 0)

  return (
    <div className={cn('rounded-lg border border-accent/30 bg-accent/5 overflow-hidden my-1.5 p-3', submitted && 'opacity-60')}>
      {questions.map((q, qi) => (
        <div key={qi} className="mb-3 last:mb-0">
          <div className="flex items-center gap-2 mb-1.5">
            <Badge variant="info">{q.header}</Badge>
            {q.multiSelect && <span className="text-xs text-muted-foreground">Select multiple</span>}
          </div>
          <div className="text-sm text-foreground mb-2">{q.question}</div>
          <div className="flex flex-wrap gap-1.5">
            {q.options.map((opt, oi) => {
              const isSelected = (selectedOptions[qi] || []).includes(opt.label)
              return (
                <button
                  key={oi}
                  className={cn(
                    'rounded-md border px-3 py-1.5 text-sm transition-colors text-left',
                    isSelected ? 'border-accent bg-accent/20 text-foreground' : 'border-border hover:bg-muted text-muted-foreground'
                  )}
                  onClick={() => handleOptionClick(qi, opt.label, q.multiSelect)}
                  disabled={submitted}
                >
                  <div className="font-medium">{opt.label}</div>
                  {opt.description && <div className="text-xs opacity-75">{opt.description}</div>}
                </button>
              )
            })}
            <button
              className={cn(
                'rounded-md border px-3 py-1.5 text-sm transition-colors',
                (selectedOptions[qi] || []).includes('__other__') ? 'border-accent bg-accent/20 text-foreground' : 'border-border hover:bg-muted text-muted-foreground'
              )}
              onClick={() => handleOptionClick(qi, '__other__', q.multiSelect)}
              disabled={submitted}
            >
              Other
            </button>
          </div>
          {(selectedOptions[qi] || []).includes('__other__') && (
            <input
              className="mt-2 w-full rounded-md border border-border bg-transparent px-3 py-1.5 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-accent"
              type="text"
              placeholder="Type your answer..."
              value={otherTexts[qi] || ''}
              onChange={(e) => setOtherTexts((p) => ({ ...p, [qi]: e.target.value }))}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault()
                  handleSubmit()
                }
              }}
              disabled={submitted}
            />
          )}
        </div>
      ))}
      {!submitted && hasSelection && (
        <Button size="sm" variant="primary" onClick={handleSubmit} className="mt-2">
          Submit
        </Button>
      )}
      {sendError && (
        <div className="mt-1.5 text-xs text-warning-foreground">{sendError}</div>
      )}
    </div>
  )
}

function StatusIcon({ status }: { status: string }) {
  if (status === 'calling') {
    return (
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-accent animate-spin">
        <circle cx="12" cy="12" r="10" strokeDasharray="32" strokeDashoffset="16" />
      </svg>
    )
  }
  if (status === 'completed') {
    return (
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-success-foreground">
        <polyline points="20 6 9 17 4 12" />
      </svg>
    )
  }
  if (status === 'error') {
    return (
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-destructive-foreground">
        <line x1="18" y1="6" x2="6" y2="18" />
        <line x1="6" y1="6" x2="18" y2="18" />
      </svg>
    )
  }
  if (status === 'pending_approval') {
    return (
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-warning-foreground">
        <circle cx="12" cy="12" r="10" />
        <polyline points="12 6 12 12 16 14" />
      </svg>
    )
  }
  return null
}

function CanvasSurfaceCard({ call, canvasSurfaces, onCanvasInteraction }: { call: ToolCall; canvasSurfaces?: Map<string, A2UISurfaceState>; onCanvasInteraction?: (canvasId: string, action: UserAction) => void }) {
  const [expanded, setExpanded] = useState(true)
  const args = call.arguments as { canvas_id?: string } | undefined
  const canvasId = args?.canvas_id
  const surfaceState = canvasId ? canvasSurfaces?.get(canvasId) : undefined
  const displayName = formatToolName(call.tool_name)

  return (
    <div className={cn(
      'rounded-lg border border-accent/20 overflow-hidden my-1.5',
      call.status === 'error' && 'border-destructive-foreground/30'
    )}>
      <div
        className="flex items-center gap-2 px-3 py-1.5 text-sm cursor-pointer hover:bg-muted/50 transition-colors bg-accent/5"
        onClick={() => setExpanded(!expanded)}
      >
        <StatusIcon status={call.status} />
        <span className="font-mono text-foreground">{displayName}</span>
        <span className="text-muted-foreground text-xs">{call.server_name}</span>
        {surfaceState && <Badge variant="info" className="ml-2">Interactive</Badge>}
        <div className="flex-1" />
        <span className="text-muted-foreground text-xs">{expanded ? '\u25BC' : '\u25B6'}</span>
      </div>
      {expanded && (
        <div className="border-t border-border px-3 py-2 text-xs space-y-2">
          {surfaceState && onCanvasInteraction ? (
            <A2UIRenderer surfaceState={surfaceState} onAction={onCanvasInteraction} />
          ) : (
            <div className="text-muted-foreground italic">Targeting {canvasId || 'an unknown canvas'}</div>
          )}
          {call.status === 'error' && call.error && (
            <div>
              <div className="text-destructive-foreground mb-1 font-medium mt-2">Error</div>
              <pre className="bg-destructive/30 rounded p-2 overflow-x-auto text-destructive-foreground">
                {call.error.replace(/<\/?tool_use_error>/g, '').trim()}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function GroupStatusIcon({ hasErrors, allCompleted, hasInFlight }: { hasErrors: boolean; allCompleted: boolean; hasInFlight: boolean }) {
  if (hasInFlight) {
    return (
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-accent animate-spin">
        <circle cx="12" cy="12" r="10" strokeDasharray="32" strokeDashoffset="16" />
      </svg>
    )
  }
  if (hasErrors) {
    return (
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-destructive-foreground">
        <line x1="18" y1="6" x2="6" y2="18" />
        <line x1="6" y1="6" x2="18" y2="18" />
      </svg>
    )
  }
  if (allCompleted) {
    return (
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-success-foreground">
        <polyline points="20 6 9 17 4 12" />
      </svg>
    )
  }
  return null
}

function ToolCallGroupHeader({ group, expanded, onToggle, onRespond, onRespondToApproval, canvasSurfaces, onCanvasInteraction }: {
  group: ToolCallGroup
  expanded: boolean
  onToggle: () => void
  onRespond?: (toolCallId: string, answers: Record<string, string>) => boolean | void
  onRespondToApproval?: (toolCallId: string, decision: 'approve' | 'reject' | 'approve_always') => boolean | void
  canvasSurfaces?: Map<string, A2UISurfaceState>
  onCanvasInteraction?: (canvasId: string, action: UserAction) => void
}) {
  const serverName = group.tool_calls[0]?.server_name

  return (
    <div className={cn(
      'rounded-lg border overflow-hidden my-1.5',
      group.hasErrors ? 'border-destructive-foreground/30' : group.hasInFlight ? 'border-accent/30' : 'border-border'
    )}>
      <div
        className="flex items-center gap-2 px-3 py-1.5 text-sm cursor-pointer hover:bg-muted/50 transition-colors"
        onClick={onToggle}
      >
        <GroupStatusIcon hasErrors={group.hasErrors} allCompleted={group.allCompleted} hasInFlight={group.hasInFlight} />
        <span className="font-mono text-foreground">{group.displayName}</span>
        <Badge variant="default">×{group.tool_calls.length}</Badge>
        {serverName && serverName !== 'builtin' && <span className="text-muted-foreground text-xs">{serverName}</span>}
        <div className="flex-1" />
        <span className="text-muted-foreground text-xs">{expanded ? '\u25BC' : '\u25B6'}</span>
      </div>
      {expanded && (
        <div className="border-t border-border pl-4">
          {group.tool_calls.map(call => (
            <ToolCallItem
              key={call.id}
              call={call}
              nested
              onRespond={onRespond}
              onRespondToApproval={onRespondToApproval}
              canvasSurfaces={canvasSurfaces}
              onCanvasInteraction={onCanvasInteraction}
            />
          ))}
        </div>
      )}
    </div>
  )
}

// --- Tier 1: Tool Chain wrapper (collapsible group of all calls between text blocks) ---

interface ToolChainGroupProps {
  toolCalls: ToolCall[]
  onRespond?: (toolCallId: string, answers: Record<string, string>) => boolean | void
  onRespondToApproval?: (toolCallId: string, decision: 'approve' | 'reject' | 'approve_always') => boolean | void
  canvasSurfaces?: Map<string, A2UISurfaceState>
  onCanvasInteraction?: (canvasId: string, action: UserAction) => void
}

export const ToolChainGroup = memo(function ToolChainGroup({ toolCalls, onRespond, onRespondToApproval, canvasSurfaces, onCanvasInteraction }: ToolChainGroupProps) {
  const hasInFlight = toolCalls.some(tc => tc.status === 'calling')
  const hasErrors = toolCalls.some(tc => tc.status === 'error')
  const allCompleted = toolCalls.every(tc => tc.status === 'completed')
  const [expanded, setExpanded] = useState(hasInFlight || !allCompleted)

  const summary = useMemo(() => buildChainSummary(toolCalls), [toolCalls])
  const count = toolCalls.length

  if (!count) return null

  // Single call — no point wrapping in a "1 tool call" group, render directly
  if (count === 1) {
    return (
      <ToolCallCards
        toolCalls={toolCalls}
        onRespond={onRespond}
        onRespondToApproval={onRespondToApproval}
        canvasSurfaces={canvasSurfaces}
        onCanvasInteraction={onCanvasInteraction}
      />
    )
  }

  return (
    <div className={cn(
      'rounded-lg border overflow-hidden my-1.5',
      hasErrors ? 'border-destructive-foreground/30' : hasInFlight ? 'border-accent/30' : 'border-border'
    )}>
      <div
        className="flex items-center gap-2 px-3 py-1.5 text-sm cursor-pointer hover:bg-muted/50 transition-colors"
        onClick={() => setExpanded(!expanded)}
      >
        <GroupStatusIcon hasErrors={hasErrors} allCompleted={allCompleted} hasInFlight={hasInFlight} />
        <span className="text-muted-foreground">
          {count} tool call{count !== 1 ? 's' : ''}
        </span>
        <span className="text-muted-foreground/60 text-xs truncate">{summary}</span>
        <div className="flex-1" />
        <span className="text-muted-foreground text-xs">{expanded ? '\u25BC' : '\u25B6'}</span>
      </div>
      {expanded && (
        <div className="border-t border-border px-3">
          <ToolCallCards
            toolCalls={toolCalls}
            onRespond={onRespond}
            onRespondToApproval={onRespondToApproval}
            canvasSurfaces={canvasSurfaces}
            onCanvasInteraction={onCanvasInteraction}
          />
        </div>
      )}
    </div>
  )
})

export const ToolCallCards = memo(function ToolCallCards({ toolCalls, onRespond, onRespondToApproval, canvasSurfaces, onCanvasInteraction }: ToolCallCardProps) {
  const segments = useMemo(() => groupToolCalls(toolCalls), [toolCalls])
  const [collapsedGroups, setCollapsedGroups] = useState<Set<string>>(() => new Set())

  const toggleGroup = useCallback((key: string) => {
    setCollapsedGroups(prev => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }, [])

  if (!toolCalls.length) return null

  return (
    <div className="my-1">
      {segments.map(segment => {
        if (segment.kind === 'single') {
          return (
            <ToolCallItem
              key={segment.call.id}
              call={segment.call}
              onRespond={onRespond}
              onRespondToApproval={onRespondToApproval}
              canvasSurfaces={canvasSurfaces}
              onCanvasInteraction={onCanvasInteraction}
            />
          )
        }
        const groupKey = `${segment.tool_calls[0].id}-${segment.toolName}`
        return (
          <ToolCallGroupHeader
            key={groupKey}
            group={segment}
            expanded={!collapsedGroups.has(groupKey)}
            onToggle={() => toggleGroup(groupKey)}
            onRespond={onRespond}
            onRespondToApproval={onRespondToApproval}
            canvasSurfaces={canvasSurfaces}
            onCanvasInteraction={onCanvasInteraction}
          />
        )
      })}
    </div>
  )
})
