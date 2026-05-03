import { useState } from 'react'
import { cn } from '../../lib/utils'

const MODAL_OVERLAY_CLS =
  'fixed inset-0 z-[1000] flex items-center justify-center bg-[var(--surface-scrim)]'
const MODAL_CLS =
  'max-h-[80vh] w-[480px] max-w-[90vw] overflow-y-auto rounded-lg border border-[var(--border)] bg-[var(--bg-primary)] p-5'
const MODAL_TITLE_CLS = 'mb-4 text-[length:var(--font-size-base)] font-semibold'

const FORM_GROUP_CLS = 'mb-3'
const FORM_LABEL_CLS =
  'mb-1 block text-[length:var(--text-sm)] font-medium text-[var(--text-secondary)]'
const FORM_INPUT_CLS =
  'box-border w-full rounded border border-[var(--border)] bg-[var(--bg-secondary)] px-2 py-1.5 text-[length:var(--text-sm)] text-[var(--text-primary)] outline-none focus:border-[var(--accent)] pointer-coarse:min-h-11'
const FORM_ROW_CLS = 'flex gap-2 [&>*]:flex-1'
const FORM_ERROR_CLS = 'mt-1 text-[length:var(--text-sm)] text-[var(--color-error)]'
const FORM_LABEL_INLINE_CLS = 'flex items-center gap-2 text-[length:var(--text-sm)] font-medium text-[var(--text-secondary)]'

const MODAL_ACTIONS_CLS = 'mt-4 flex justify-end gap-2'
const MODAL_BTN_CLS =
  'cursor-pointer rounded-md border border-[var(--border)] bg-[var(--bg-secondary)] px-3.5 py-1.5 text-[length:var(--text-sm)] text-[var(--text-primary)] transition-colors duration-150 hover:bg-[rgba(255,255,255,0.05)] disabled:cursor-not-allowed disabled:opacity-50 pointer-coarse:min-h-11'
const MODAL_BTN_PRIMARY_CLS =
  'border-[var(--accent)] bg-[var(--accent)] text-[var(--accent-foreground)] hover:opacity-90'

const IMPORT_TABS_CLS = 'mb-4 flex border-b border-[var(--border)]'
const IMPORT_TAB_CLS =
  'cursor-pointer border-0 border-b-2 border-transparent bg-transparent px-4 py-2 text-[length:var(--text-sm)] font-medium text-[var(--text-secondary)] [margin-bottom:-1px] hover:text-[var(--text-primary)] pointer-coarse:min-h-11'
const IMPORT_TAB_ACTIVE_CLS = 'border-[var(--accent)] text-[var(--accent)]'

interface McpAddServerModalProps {
  onAdd: (params: {
    name: string
    transport: string
    url?: string
    command?: string
    args?: string[]
    enabled?: boolean
  }) => Promise<boolean>
  onClose: () => void
}

export function McpAddServerModal({ onAdd, onClose }: McpAddServerModalProps) {
  const [name, setName] = useState('')
  const [transport, setTransport] = useState('http')
  const [url, setUrl] = useState('')
  const [command, setCommand] = useState('')
  const [args, setArgs] = useState('')
  const [enabled, setEnabled] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  const needsUrl = transport === 'http' || transport === 'websocket' || transport === 'sse'
  const needsCommand = transport === 'stdio'

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!name.trim()) { setError('Name is required'); return }
    if (needsUrl && !url.trim()) { setError('URL is required'); return }
    if (needsCommand && !command.trim()) { setError('Command is required'); return }

    setSaving(true)
    setError(null)
    const params: Parameters<typeof onAdd>[0] = {
      name: name.trim(),
      transport,
      enabled,
    }
    if (needsUrl) params.url = url.trim()
    if (needsCommand) {
      params.command = command.trim()
      if (args.trim()) params.args = args.trim().split(/\s+/)
    }

    const ok = await onAdd(params)
    setSaving(false)
    if (ok) onClose()
    else setError('Failed to add server')
  }

  return (
    <div className={MODAL_OVERLAY_CLS} onClick={onClose}>
      <form className={MODAL_CLS} onClick={e => e.stopPropagation()} onSubmit={handleSubmit}>
        <h2 className={MODAL_TITLE_CLS}>Add MCP Server</h2>

        {error && <div className={FORM_ERROR_CLS}>{error}</div>}

        <div className={FORM_ROW_CLS}>
          <div className={FORM_GROUP_CLS}>
            <label className={FORM_LABEL_CLS}>Name</label>
            <input
              className={FORM_INPUT_CLS}
              value={name}
              onChange={e => setName(e.target.value)}
              placeholder="my-server"
              autoFocus
            />
          </div>
          <div className={FORM_GROUP_CLS}>
            <label className={FORM_LABEL_CLS}>Transport</label>
            <select className={FORM_INPUT_CLS} value={transport} onChange={e => setTransport(e.target.value)}>
              <option value="http">HTTP</option>
              <option value="stdio">Stdio</option>
              <option value="websocket">WebSocket</option>
              <option value="sse">SSE</option>
            </select>
          </div>
        </div>

        {needsUrl && (
          <div className={FORM_GROUP_CLS}>
            <label className={FORM_LABEL_CLS}>URL</label>
            <input
              className={FORM_INPUT_CLS}
              value={url}
              onChange={e => setUrl(e.target.value)}
              placeholder="http://localhost:8080"
            />
          </div>
        )}

        {needsCommand && (
          <>
            <div className={FORM_GROUP_CLS}>
              <label className={FORM_LABEL_CLS}>Command</label>
              <input
                className={FORM_INPUT_CLS}
                value={command}
                onChange={e => setCommand(e.target.value)}
                placeholder="npx"
              />
            </div>
            <div className={FORM_GROUP_CLS}>
              <label className={FORM_LABEL_CLS}>Arguments (space-separated)</label>
              <input
                className={FORM_INPUT_CLS}
                value={args}
                onChange={e => setArgs(e.target.value)}
                placeholder="-y @modelcontextprotocol/server-x"
              />
            </div>
          </>
        )}

        <div className={FORM_GROUP_CLS}>
          <label className={FORM_LABEL_INLINE_CLS}>
            <input
              type="checkbox"
              checked={enabled}
              onChange={e => setEnabled(e.target.checked)}
            />
            Enabled
          </label>
        </div>

        <div className={MODAL_ACTIONS_CLS}>
          <button type="button" className={MODAL_BTN_CLS} onClick={onClose}>Cancel</button>
          <button type="submit" className={cn(MODAL_BTN_CLS, MODAL_BTN_PRIMARY_CLS)} disabled={saving}>
            {saving ? 'Adding...' : 'Add Server'}
          </button>
        </div>
      </form>
    </div>
  )
}

interface McpImportModalProps {
  onImport: (params: {
    from_project?: string
    github_url?: string
    query?: string
  }) => Promise<boolean>
  onClose: () => void
}

export function McpImportModal({ onImport, onClose }: McpImportModalProps) {
  const [activeTab, setActiveTab] = useState<'project' | 'github' | 'search'>('github')
  const [value, setValue] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [importing, setImporting] = useState(false)

  const placeholder = activeTab === 'project'
    ? 'other-project-name'
    : activeTab === 'github'
      ? 'https://github.com/org/mcp-server'
      : 'search query...'

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!value.trim()) { setError('Value is required'); return }

    setImporting(true)
    setError(null)
    const params: Parameters<typeof onImport>[0] = {}
    if (activeTab === 'project') params.from_project = value.trim()
    else if (activeTab === 'github') params.github_url = value.trim()
    else params.query = value.trim()

    const ok = await onImport(params)
    setImporting(false)
    if (ok) onClose()
    else setError('Import failed')
  }

  return (
    <div className={MODAL_OVERLAY_CLS} onClick={onClose}>
      <form className={MODAL_CLS} onClick={e => e.stopPropagation()} onSubmit={handleSubmit}>
        <h2 className={MODAL_TITLE_CLS}>Import MCP Server</h2>

        <div className={IMPORT_TABS_CLS}>
          <button
            type="button"
            className={cn(IMPORT_TAB_CLS, activeTab === 'project' && IMPORT_TAB_ACTIVE_CLS)}
            onClick={() => { setActiveTab('project'); setValue(''); setError(null) }}
          >
            From Project
          </button>
          <button
            type="button"
            className={cn(IMPORT_TAB_CLS, activeTab === 'github' && IMPORT_TAB_ACTIVE_CLS)}
            onClick={() => { setActiveTab('github'); setValue(''); setError(null) }}
          >
            GitHub URL
          </button>
          <button
            type="button"
            className={cn(IMPORT_TAB_CLS, activeTab === 'search' && IMPORT_TAB_ACTIVE_CLS)}
            onClick={() => { setActiveTab('search'); setValue(''); setError(null) }}
          >
            Search
          </button>
        </div>

        {error && <div className={FORM_ERROR_CLS}>{error}</div>}

        <div className={FORM_GROUP_CLS}>
          <label className={FORM_LABEL_CLS}>
            {activeTab === 'project' ? 'Project Name' : activeTab === 'github' ? 'GitHub URL' : 'Search Query'}
          </label>
          <input
            className={FORM_INPUT_CLS}
            value={value}
            onChange={e => setValue(e.target.value)}
            placeholder={placeholder}
            autoFocus
          />
        </div>

        <div className={MODAL_ACTIONS_CLS}>
          <button type="button" className={MODAL_BTN_CLS} onClick={onClose}>Cancel</button>
          <button type="submit" className={cn(MODAL_BTN_CLS, MODAL_BTN_PRIMARY_CLS)} disabled={importing}>
            {importing ? 'Importing...' : 'Import'}
          </button>
        </div>
      </form>
    </div>
  )
}
