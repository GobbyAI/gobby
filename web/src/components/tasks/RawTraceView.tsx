import { useState, useEffect, useCallback, useMemo } from 'react'

// =============================================================================
// Types
// =============================================================================

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

// =============================================================================
// Helpers
// =============================================================================

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

/** Simple JSON syntax highlighting via spans */
function highlightJson(json: string, searchTerm: string): (JSX.Element | string)[] {
  // Tokenize JSON for coloring
  const tokens = json.split(/("(?:[^"\\]|\\.)*")|(\b(?:true|false|null)\b)|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g)
  const parts: (JSX.Element | string)[] = []
  let keyIdx = 0

  for (const token of tokens) {
    if (token === undefined || token === '') continue
    keyIdx++

    let className = ''
    if (/^"/.test(token)) {
      // Check if it's a key (followed by colon in context) - simplify: color all strings
      className = 'trace-json-string'
    } else if (/^(true|false|null)$/.test(token)) {
      className = 'trace-json-keyword'
    } else if (/^-?\d/.test(token)) {
      className = 'trace-json-number'
    }

    // Apply search highlight within token
    if (searchTerm && token.toLowerCase().includes(searchTerm.toLowerCase())) {
      const idx = token.toLowerCase().indexOf(searchTerm.toLowerCase())
      parts.push(
        <span key={`${keyIdx}-a`} className={className}>{token.slice(0, idx)}</span>,
        <mark key={`${keyIdx}-h`} className="trace-search-hit">{token.slice(idx, idx + searchTerm.length)}</mark>,
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

async function copyToClipboard(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    return false
  }
}

// =============================================================================
// TraceEntryCard
// =============================================================================

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

  // Auto-expand when search matches
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
    <div className={`trace-entry ${entry.hasError ? 'trace-entry--error' : ''}`}>
      <button
        className="trace-entry-header"
        onClick={() => setExpanded(!expanded)}
      >
        <span className="trace-entry-expand">{expanded ? '\u25BE' : '\u25B8'}</span>
        <span className={`trace-entry-dot ${entry.hasError ? 'trace-entry-dot--error' : 'trace-entry-dot--ok'}`} />
        <span className="trace-entry-name">{entry.toolName}</span>
        <span className="trace-entry-time">{formatTime(entry.timestamp)}</span>
        <span className="trace-entry-idx">#{entry.index}</span>
      </button>

      {expanded && (
        <div className="trace-entry-body">
          {inputJson && (
            <div className="trace-entry-section">
              <div className="trace-entry-section-header">
                <span className="trace-entry-section-label">Input</span>
                <button
                  className="trace-copy-btn"
                  onClick={() => handleCopy('input')}
                  title="Copy to clipboard"
                >
                  {copiedField === 'input' ? 'Copied' : 'Copy'}
                </button>
              </div>
              <pre className="trace-json">{highlightJson(inputJson, searchTerm)}</pre>
            </div>
          )}
          {resultJson && (
            <div className="trace-entry-section">
              <div className="trace-entry-section-header">
                <span className="trace-entry-section-label">Result</span>
                <button
                  className="trace-copy-btn"
                  onClick={() => handleCopy('result')}
                  title="Copy to clipboard"
                >
                  {copiedField === 'result' ? 'Copied' : 'Copy'}
                </button>
              </div>
              <pre className="trace-json">{highlightJson(resultJson, searchTerm)}</pre>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// =============================================================================
// RawTraceView
// =============================================================================

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
  if (isLoading) return <div className="trace-loading">Loading trace data...</div>
  if (entries.length === 0) return <div className="trace-empty">No tool calls recorded</div>

  const errorCount = entries.filter(e => e.hasError).length

  return (
    <div className="raw-trace-view">
      {/* Toolbar */}
      <div className="trace-toolbar">
        <input
          type="text"
          className="trace-search"
          placeholder="Search trace..."
          value={searchTerm}
          onChange={e => setSearchTerm(e.target.value)}
        />
        <button
          className={`trace-toolbar-btn ${showErrors ? 'active' : ''}`}
          onClick={() => setShowErrors(!showErrors)}
          title="Show errors only"
        >
          Errors ({errorCount})
        </button>
        <button
          className="trace-toolbar-btn"
          onClick={() => setExpandAll(!expandAll)}
        >
          {expandAll ? 'Collapse all' : 'Expand all'}
        </button>
        <button
          className="trace-toolbar-btn"
          onClick={handleCopyAll}
          title="Copy all as JSON"
        >
          Copy all
        </button>
        <span className="trace-count">{filtered.length} / {entries.length} calls</span>
      </div>

      {/* Entries */}
      <div className="trace-entries">
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
