import { useState, useEffect } from 'react'
import type { McpToolSchema } from '../../hooks/useMcp'
import { cn } from '../../lib/utils'

const BACKDROP_CLS =
  'pointer-events-none fixed inset-0 z-[900] bg-[var(--surface-scrim)] opacity-0 transition-opacity duration-200'
const BACKDROP_OPEN_CLS = 'pointer-events-auto opacity-100'

const SLIDE_CLS =
  'fixed bottom-0 right-0 top-0 z-[901] w-[520px] max-w-[90vw] translate-x-full overflow-y-auto border-l border-[var(--border)] bg-[var(--bg-primary)] transition-transform duration-[250ms] ease-[cubic-bezier(0.4,0,0.2,1)] max-md:w-screen max-md:max-w-none'
const SLIDE_OPEN_CLS = 'translate-x-0'

const DETAIL_CLS = 'p-5'
const DETAIL_HEADER_CLS = 'mb-4 flex items-start justify-between'
const DETAIL_HEADER_TITLE_CLS = 'm-0 break-all text-[length:var(--font-size-base)] font-semibold'
const DETAIL_CLOSE_CLS =
  'shrink-0 cursor-pointer border-0 bg-transparent px-2 py-1 text-[length:var(--text-2xl)] text-[var(--text-secondary)] hover:text-[var(--text-primary)] pointer-coarse:h-11 pointer-coarse:w-11'

const STATUS_MESSAGE_CLS = 'flex items-center justify-center p-10 text-[length:var(--text-base)] text-[var(--text-secondary)]'

const DETAIL_GRID_CLS =
  'mb-5 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-[length:var(--text-sm)]'
const DETAIL_LABEL_CLS = 'text-[length:var(--text-sm)] text-[var(--text-secondary)]'
const DETAIL_VALUE_CLS = 'text-[length:var(--text-sm)]'

const DETAIL_SECTION_CLS = 'mb-5'
const DETAIL_SECTION_TITLE_CLS = 'mb-2 text-[length:var(--text-sm)] font-semibold'

const DETAIL_SCHEMA_CLS =
  'max-h-[300px] overflow-x-auto overflow-y-auto whitespace-pre-wrap break-all rounded-md border border-[var(--border)] bg-[var(--bg-secondary)] p-3 text-[length:var(--text-sm)]'
const EXECUTE_AREA_CLS =
  'box-border min-h-20 w-full resize-y rounded-md border border-[var(--border)] bg-[var(--bg-secondary)] p-2 text-[length:var(--text-sm)] text-[var(--text-primary)] outline-none focus:border-[var(--accent)]'
const EXECUTE_BTN_CLS =
  'mt-2 cursor-pointer rounded-md border-0 bg-[var(--accent)] px-4 py-1.5 text-[length:var(--text-sm)] font-medium text-[var(--accent-foreground)] transition-opacity duration-150 hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 pointer-coarse:min-h-11'
const RESULT_CLS =
  'mt-3 max-h-[400px] overflow-x-auto overflow-y-auto whitespace-pre-wrap break-all rounded-md border border-[var(--border)] bg-[var(--bg-secondary)] p-3 text-[length:var(--text-sm)]'
const RESULT_ERROR_CLS = 'border-[color-mix(in_srgb,var(--color-error)_30%,transparent)] text-[var(--color-error)]'

interface McpToolDetailProps {
  serverName: string | null
  toolName: string | null
  schema: McpToolSchema | null
  isLoading: boolean
  onClose: () => void
  onCallTool: (server: string, tool: string, args: Record<string, unknown>) => Promise<{ success: boolean; result?: unknown; error?: string }>
}

export function McpToolDetail({ serverName, toolName, schema, isLoading, onClose, onCallTool }: McpToolDetailProps) {
  const isOpen = serverName !== null && toolName !== null
  const [argsText, setArgsText] = useState('{}')
  const [executing, setExecuting] = useState(false)
  const [result, setResult] = useState<{ success: boolean; data: string } | null>(null)

  // Reset state when tool changes
  useEffect(() => {
    setArgsText('{}')
    setResult(null)
  }, [serverName, toolName])

  // Escape key handler
  useEffect(() => {
    if (!isOpen) return
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [isOpen, onClose])

  const handleExecute = async () => {
    if (!serverName || !toolName) return
    setExecuting(true)
    setResult(null)
    try {
      const args = JSON.parse(argsText)
      const res = await onCallTool(serverName, toolName, args)
      setResult({
        success: res.success,
        data: JSON.stringify(res.success ? res.result : res.error, null, 2),
      })
    } catch (e) {
      setResult({ success: false, data: String(e) })
    } finally {
      setExecuting(false)
    }
  }

  return (
    <>
      <div
        className={cn(BACKDROP_CLS, isOpen && BACKDROP_OPEN_CLS)}
        onClick={onClose}
      />
      <aside
        className={cn(SLIDE_CLS, isOpen && SLIDE_OPEN_CLS)}
        aria-labelledby={isOpen ? 'mcp-tool-detail-title' : undefined}
        aria-hidden={!isOpen}
      >
        {isOpen && (
          <div className={DETAIL_CLS}>
            <div className={DETAIL_HEADER_CLS}>
              <h2 id="mcp-tool-detail-title" className={DETAIL_HEADER_TITLE_CLS}>{toolName}</h2>
              <button className={DETAIL_CLOSE_CLS} onClick={onClose} aria-label="Close tool details">
                &times;
              </button>
            </div>

            {isLoading ? (
              <div className={STATUS_MESSAGE_CLS}>Loading schema...</div>
            ) : schema ? (
              <>
                <div className={DETAIL_GRID_CLS}>
                  {schema.description && (
                    <>
                      <div className={DETAIL_LABEL_CLS}>Description</div>
                      <div className={DETAIL_VALUE_CLS}>{schema.description}</div>
                    </>
                  )}
                  <div className={DETAIL_LABEL_CLS}>Server</div>
                  <div className={DETAIL_VALUE_CLS}>{serverName}</div>
                </div>

                <div className={DETAIL_SECTION_CLS}>
                  <h3 className={DETAIL_SECTION_TITLE_CLS}>Input Schema</h3>
                  <pre className={DETAIL_SCHEMA_CLS}>
                    <code>{JSON.stringify(schema.inputSchema, null, 2)}</code>
                  </pre>
                </div>

                <div className={DETAIL_SECTION_CLS}>
                  <h3 className={DETAIL_SECTION_TITLE_CLS}>Execute</h3>
                  <textarea
                    className={EXECUTE_AREA_CLS}
                    value={argsText}
                    onChange={e => setArgsText(e.target.value)}
                    placeholder='{"key": "value"}'
                  />
                  <button
                    className={EXECUTE_BTN_CLS}
                    onClick={handleExecute}
                    disabled={executing}
                  >
                    {executing ? 'Executing...' : 'Execute'}
                  </button>

                  {result && (
                    <pre className={cn(RESULT_CLS, !result.success && RESULT_ERROR_CLS)}>
                      {result.data}
                    </pre>
                  )}
                </div>
              </>
            ) : (
              <div className={STATUS_MESSAGE_CLS}>Failed to load schema</div>
            )}
          </div>
        )}
      </aside>
    </>
  )
}
