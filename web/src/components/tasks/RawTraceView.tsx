import { useState, useEffect, useCallback, useMemo } from 'react'

interface SessionMessage {
  tool_name: string | null
  tool_input: string | null
  tool_result: string | null
  content: string | null
  role: string
  timestamp: string
}

interface TraceEntry {
  index: number
  toolName: string
  input: string | null
  result: string | null
  timestamp: string
  hasError: boolean
}

const ROOT_CLS = 'mt-2'
const TOOLBAR_CLS = 'flex flex-wrap items-center gap-[0.4rem] py-[0.4rem]'
const SEARCH_CLS =
  'min-w-[8rem] flex-1 rounded border border-[var(--border)] bg-[var(--bg-secondary)] px-2 py-1 font-[inherit] text-[length:calc(var(--font-size-base)*0.7)] text-[var(--text-primary)] outline-none focus:border-[var(--accent)]'
const TOOLBAR_BTN_CLS =
  'cursor-pointer whitespace-nowrap rounded border border-[var(--border)] bg-[var(--bg-secondary)] px-2 py-1 font-[inherit] text-[length:calc(var(--font-size-base)*0.65)] text-[var(--text-muted)] transition-colors duration-150 hover:border-[var(--text-muted)] hover:text-[var(--text-secondary)]'
const TOOLBAR_BTN_ACTIVE_CLS =
  'border-[color-mix(in_srgb,var(--color-error)_40%,transparent)] bg-[color-mix(in_srgb,var(--color-error)_8%,transparent)] text-[var(--color-error)] hover:border-[color-mix(in_srgb,var(--color-error)_40%,transparent)] hover:text-[var(--color-error)]'
const COUNT_CLS =
  'ml-auto font-[inherit] text-[length:calc(var(--font-size-base)*0.65)] text-[var(--text-muted)]'
const STATE_CLS = 'py-4 text-[length:calc(var(--font-size-base)*0.75)] text-[var(--text-muted)]'
const ENTRIES_CLS = 'flex max-h-[32rem] flex-col gap-0.5 overflow-y-auto'
const ENTRY_CLS = 'overflow-hidden rounded border border-[var(--border)] bg-[var(--bg-secondary)]'
const ENTRY_ERROR_CLS = 'border-[color-mix(in_srgb,var(--color-error)_25%,transparent)]'
const ENTRY_HEADER_CLS =
  'flex w-full cursor-pointer items-center gap-[0.4rem] border-none bg-transparent px-2 py-[0.3rem] text-left font-[inherit] text-[length:calc(var(--font-size-base)*0.7)] text-[var(--text-primary)] transition-colors duration-100 hover:bg-[var(--bg-tertiary)]'
const ENTRY_EXPAND_CLS =
  'w-[0.8rem] shrink-0 text-[length:calc(var(--font-size-base)*0.6)] text-[var(--text-muted)]'
const ENTRY_DOT_CLS = 'h-1.5 w-1.5 shrink-0 rounded-full'
const ENTRY_DOT_OK_CLS = 'bg-[var(--color-success-foreground)]'
const ENTRY_DOT_ERROR_CLS = 'bg-[var(--color-error)]'
const ENTRY_NAME_CLS = 'flex-1 overflow-hidden text-ellipsis whitespace-nowrap font-semibold text-[var(--text-primary)]'
const ENTRY_TIME_CLS =
  'shrink-0 text-[length:calc(var(--font-size-base)*0.6)] text-[var(--text-muted)]'
const ENTRY_IDX_CLS =
  'min-w-8 shrink-0 text-right text-[length:calc(var(--font-size-base)*0.6)] text-[var(--text-muted)]'
const ENTRY_BODY_CLS =
  'flex flex-col gap-[0.4rem] border-t border-[var(--border)] px-2 py-[0.4rem]'
const ENTRY_SECTION_CLS = 'flex flex-col gap-[0.15rem]'
const ENTRY_SECTION_HEADER_CLS = 'flex items-center justify-between'
const ENTRY_SECTION_LABEL_CLS =
  'font-[inherit] text-[length:calc(var(--font-size-base)*0.6)] font-semibold uppercase tracking-[0.05em] text-[var(--text-muted)]'
const COPY_BTN_CLS =
  'cursor-pointer rounded-[3px] border border-[var(--border)] bg-transparent px-[0.35rem] py-[0.1rem] font-[inherit] text-[length:calc(var(--font-size-base)*0.55)] text-[var(--text-muted)] transition-colors duration-150 hover:border-[var(--text-muted)] hover:text-[var(--text-secondary)]'
const JSON_CLS =
  'm-0 max-h-80 overflow-x-auto overflow-y-auto whitespace-pre rounded border border-[var(--border)] bg-[var(--bg-primary)] px-2 py-[0.4rem] font-[inherit] text-[length:calc(var(--font-size-base)*0.65)] leading-[1.5] text-[var(--text-secondary)]'
const JSON_STRING_CLS = 'text-[var(--color-info)]'
const JSON_NUMBER_CLS = 'text-[var(--color-info)]'
const JSON_KEYWORD_CLS = 'text-[var(--color-error)]'
const SEARCH_HIT_CLS =
  'rounded-sm bg-[color-mix(in_srgb,var(--color-warning-foreground)_30%,transparent)] px-px text-[var(--color-warning-foreground)]'

function getBaseUrl(): string {
  return ''
}

function formatTime(iso: string): string {
  const d = new Date(iso)
  return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

function prettyJson(raw: string | null): string {
  if (!raw) return ''
  try {
    return JSON.stringify(JSON.parse(raw), null, 2)
  } catch {
    return raw
  }
}

function highlightJson(json: string, searchTerm: string): (JSX.Element | string)[] {
  const tokens = json.split(/("(?:[^"\\]|\\.)*")|(\b(?:true|false|null)\b)|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g)
  const parts: (JSX.Element | string)[] = []
  let keyIdx = 0

  for (const token of tokens) {
    if (token === undefined || token === '') continue
    keyIdx++

    let className = ''
    if (/^"/.test(token)) {
      className = JSON_STRING_CLS
    } else if (/^(true|false|null)$/.test(token)) {
      className = JSON_KEYWORD_CLS
    } else if (/^-?\d/.test(token)) {
      className = JSON_NUMBER_CLS
    }

    if (searchTerm && token.toLowerCase().includes(searchTerm.toLowerCase())) {
      const idx = token.toLowerCase().indexOf(searchTerm.toLowerCase())
      parts.push(
        <span key={`${keyIdx}-a`} className={className}>{token.slice(0, idx)}</span>,
        <mark key={`${keyIdx}-h`} className={SEARCH_HIT_CLS}>{token.slice(idx, idx + searchTerm.length)}</mark>,
        <span key={`${keyIdx}-b`} className={className}>{token.slice(idx + searchTerm.length)}</span>,
      )
    } else {
      parts.push(
        className
          ? <span key={keyIdx} className={className}>{token}</span>
          : token
      )
    }
  }

  return parts
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

async function copyToClipboard(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    return false
  }
}

function TraceEntryCard({
  entry,
  searchTerm,
  defaultExpanded,
}: {
  entry: TraceEntry
  searchTerm: string
  defaultExpanded: boolean
}) {
  const [expanded, setExpanded] = useState(defaultExpanded)
  const [copiedField, setCopiedField] = useState<'input' | 'result' | null>(null)

  const inputJson = useMemo(() => prettyJson(entry.input), [entry.input])
  const resultJson = useMemo(() => prettyJson(entry.result), [entry.result])

  useEffect(() => {
    if (searchTerm) {
      const lower = searchTerm.toLowerCase()
      const matches = entry.toolName.toLowerCase().includes(lower)
        || inputJson.toLowerCase().includes(lower)
        || resultJson.toLowerCase().includes(lower)
      if (matches) setExpanded(true)
    }
  }, [searchTerm, entry.toolName, inputJson, resultJson])

  const handleCopy = async (field: 'input' | 'result') => {
    const text = field === 'input' ? inputJson : resultJson
    const ok = await copyToClipboard(text)
    if (ok) {
      setCopiedField(field)
      setTimeout(() => setCopiedField(null), 1500)
    }
  }

  return (
    <div className={entry.hasError ? `${ENTRY_CLS} ${ENTRY_ERROR_CLS}` : ENTRY_CLS}>
      <button
        className={ENTRY_HEADER_CLS}
        onClick={() => setExpanded(!expanded)}
      >
        <span className={ENTRY_EXPAND_CLS}>{expanded ? '▾' : '▸'}</span>
        <span className={`${ENTRY_DOT_CLS} ${entry.hasError ? ENTRY_DOT_ERROR_CLS : ENTRY_DOT_OK_CLS}`} />
        <span className={ENTRY_NAME_CLS}>{entry.toolName}</span>
        <span className={ENTRY_TIME_CLS}>{formatTime(entry.timestamp)}</span>
        <span className={ENTRY_IDX_CLS}>#{entry.index}</span>
      </button>

      {expanded && (
        <div className={ENTRY_BODY_CLS}>
          {inputJson && (
            <div className={ENTRY_SECTION_CLS}>
              <div className={ENTRY_SECTION_HEADER_CLS}>
                <span className={ENTRY_SECTION_LABEL_CLS}>Input</span>
                <button
                  className={COPY_BTN_CLS}
                  onClick={() => handleCopy('input')}
                  title="Copy to clipboard"
                >
                  {copiedField === 'input' ? 'Copied' : 'Copy'}
                </button>
              </div>
              <pre className={JSON_CLS}>{highlightJson(inputJson, searchTerm)}</pre>
            </div>
          )}
          {resultJson && (
            <div className={ENTRY_SECTION_CLS}>
              <div className={ENTRY_SECTION_HEADER_CLS}>
                <span className={ENTRY_SECTION_LABEL_CLS}>Result</span>
                <button
                  className={COPY_BTN_CLS}
                  onClick={() => handleCopy('result')}
                  title="Copy to clipboard"
                >
                  {copiedField === 'result' ? 'Copied' : 'Copy'}
                </button>
              </div>
              <pre className={JSON_CLS}>{highlightJson(resultJson, searchTerm)}</pre>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

interface RawTraceViewProps {
  sessionId: string | null
}

export function RawTraceView({ sessionId }: RawTraceViewProps) {
  const [entries, setEntries] = useState<TraceEntry[]>([])
  const [isLoading, setIsLoading] = useState(false)
  const [searchTerm, setSearchTerm] = useState('')
  const [expandAll, setExpandAll] = useState(false)
  const [showErrors, setShowErrors] = useState(false)

  const fetchTrace = useCallback(async () => {
    if (!sessionId) return
    setIsLoading(true)
    try {
      const baseUrl = getBaseUrl()
      const response = await fetch(
        `${baseUrl}/api/sessions/${encodeURIComponent(sessionId)}/messages?limit=500`
      )
      if (response.ok) {
        const data = await response.json()
        const messages: SessionMessage[] = data.messages || []
        let idx = 0
        const traceEntries: TraceEntry[] = messages
          .filter(m => m.tool_name)
          .map(m => ({
            index: ++idx,
            toolName: m.tool_name!,
            input: m.tool_input,
            result: m.tool_result,
            timestamp: m.timestamp,
            hasError: isErrorResult(m.tool_result),
          }))
        setEntries(traceEntries)
      }
    } catch (e) {
      console.error('Failed to fetch trace data:', e)
    }
    setIsLoading(false)
  }, [sessionId])

  useEffect(() => {
    fetchTrace()
  }, [fetchTrace])

  const filtered = useMemo(() => {
    let result = entries
    if (showErrors) {
      result = result.filter(e => e.hasError)
    }
    if (searchTerm) {
      const lower = searchTerm.toLowerCase()
      result = result.filter(e =>
        e.toolName.toLowerCase().includes(lower)
        || (e.input && e.input.toLowerCase().includes(lower))
        || (e.result && e.result.toLowerCase().includes(lower))
      )
    }
    return result
  }, [entries, searchTerm, showErrors])

  function tryParse(str: string): unknown {
    try {
      return JSON.parse(str)
    } catch {
      return str
    }
  }

  const handleCopyAll = async () => {
    const allJson = filtered.map(e => ({
      index: e.index,
      tool: e.toolName,
      timestamp: e.timestamp,
      input: e.input ? tryParse(e.input) : null,
      result: e.result ? tryParse(e.result) : null,
    }))
    await copyToClipboard(JSON.stringify(allJson, null, 2))
  }

  if (!sessionId) return null
  if (isLoading) return <div className={STATE_CLS}>Loading trace data...</div>
  if (entries.length === 0) return <div className={STATE_CLS}>No tool calls recorded</div>

  const errorCount = entries.filter(e => e.hasError).length

  return (
    <div className={ROOT_CLS}>
      <div className={TOOLBAR_CLS}>
        <input
          type="text"
          className={SEARCH_CLS}
          placeholder="Search trace..."
          value={searchTerm}
          onChange={e => setSearchTerm(e.target.value)}
        />
        <button
          className={showErrors ? `${TOOLBAR_BTN_CLS} ${TOOLBAR_BTN_ACTIVE_CLS}` : TOOLBAR_BTN_CLS}
          onClick={() => setShowErrors(!showErrors)}
          title="Show errors only"
        >
          Errors ({errorCount})
        </button>
        <button
          className={TOOLBAR_BTN_CLS}
          onClick={() => setExpandAll(!expandAll)}
        >
          {expandAll ? 'Collapse all' : 'Expand all'}
        </button>
        <button
          className={TOOLBAR_BTN_CLS}
          onClick={handleCopyAll}
          title="Copy all as JSON"
        >
          Copy all
        </button>
        <span className={COUNT_CLS}>{filtered.length} / {entries.length} calls</span>
      </div>

      <div className={ENTRIES_CLS}>
        {filtered.map(entry => (
          <TraceEntryCard
            key={`${entry.index}-${entry.toolName}`}
            entry={entry}
            searchTerm={searchTerm}
            defaultExpanded={expandAll}
          />
        ))}
      </div>
    </div>
  )
}
