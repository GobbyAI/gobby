import { memo, useState, useEffect, useCallback, useRef } from 'react'
import { useConfirmDialog } from '../../hooks/useConfirmDialog'
import { useIsMobile } from '../../hooks/useIsMobile'
import { ResizeHandle } from '../chat/artifacts/ResizeHandle'
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { markdownComponents } from '../shared/MarkdownComponents'
import { CodeMirrorEditor } from '../shared/CodeMirrorEditor'
import { codeTheme } from '../shared/codeTheme'

interface FilesTabProps {
  projectId?: string | null
  onAddToChat?: (filePath: string) => void
  layout?: 'stack' | 'responsive-split'
}

const FILES_TAB_LEFT_WIDTH_KEY = 'gobby:files-tab:left-width'
const FILES_TAB_LEFT_WIDTH_DEFAULT = 320
const FILES_TAB_LEFT_WIDTH_MIN = 200
const FILES_TAB_LEFT_WIDTH_MAX = 600

function readPersistedLeftWidth(): number {
  if (typeof window === 'undefined') return FILES_TAB_LEFT_WIDTH_DEFAULT
  try {
    const stored = window.localStorage.getItem(FILES_TAB_LEFT_WIDTH_KEY)
    if (!stored) return FILES_TAB_LEFT_WIDTH_DEFAULT
    const parsed = Number.parseInt(stored, 10)
    if (Number.isNaN(parsed)) return FILES_TAB_LEFT_WIDTH_DEFAULT
    return Math.max(FILES_TAB_LEFT_WIDTH_MIN, Math.min(FILES_TAB_LEFT_WIDTH_MAX, parsed))
  } catch {
    return FILES_TAB_LEFT_WIDTH_DEFAULT
  }
}

interface FileEntry {
  name: string
  path: string
  is_dir: boolean
  size?: number
  extension?: string
}

interface ContextMenuState {
  x: number
  y: number
  entry: FileEntry
}

const EXT_TO_LANG: Record<string, string> = {
  js: 'javascript', jsx: 'jsx', ts: 'typescript', tsx: 'tsx',
  py: 'python', rb: 'ruby', go: 'go', rs: 'rust',
  java: 'java', kt: 'kotlin', swift: 'swift', c: 'c', cpp: 'cpp',
  cs: 'csharp', php: 'php', sh: 'bash', bash: 'bash', zsh: 'bash',
  json: 'json', yaml: 'yaml', yml: 'yaml', toml: 'toml',
  xml: 'xml', html: 'html', css: 'css', scss: 'scss', less: 'less',
  md: 'markdown', sql: 'sql', graphql: 'graphql',
  dockerfile: 'docker', makefile: 'makefile',
}

function detectLanguage(path: string): string {
  const name = path.split('/').pop()?.toLowerCase() ?? ''
  if (name === 'dockerfile') return 'docker'
  if (name === 'makefile') return 'makefile'
  const ext = name.split('.').pop() ?? ''
  return EXT_TO_LANG[ext] ?? 'text'
}

function getBaseUrl(): string {
  return import.meta.env.VITE_API_BASE_URL || ''
}

function getFileIconColor(ext: string): string {
  const e = ext.toLowerCase()
  if (['ts', 'tsx'].includes(e)) return '#3178c6'
  if (['js', 'jsx'].includes(e)) return '#f7df1e'
  if (e === 'py') return '#3776ab'
  if (e === 'rs') return '#ce422b'
  if (e === 'go') return '#00add8'
  if (['json', 'yaml', 'yml', 'toml'].includes(e)) return '#cb8742'
  if (['md', 'txt', 'rst'].includes(e)) return '#737373'
  if (['css', 'scss', 'less'].includes(e)) return '#563d7c'
  if (['html', 'htm'].includes(e)) return '#e34c26'
  if (['sh', 'bash', 'zsh'].includes(e)) return '#4eaa25'
  if (['sql'].includes(e)) return '#e38c00'
  if (['rb'].includes(e)) return '#cc342d'
  return '#a3a3a3'
}

function FolderIcon({ open }: { open: boolean }) {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#facc15" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }}>
      <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
      {open && <line x1="9" y1="14" x2="15" y2="14" />}
    </svg>
  )
}

function FileIconSvg({ extension }: { extension: string }) {
  const color = getFileIconColor(extension)
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }}>
      <path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z" />
      <polyline points="13 2 13 9 20 9" />
    </svg>
  )
}

export const FilesTab = memo(function FilesTab({ projectId, onAddToChat, layout = 'stack' }: FilesTabProps) {
  const isMobile = useIsMobile()
  const useHorizontal = layout === 'responsive-split' && !isMobile
  const [rootEntries, setRootEntries] = useState<FileEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [expandedPaths, setExpandedPaths] = useState<Set<string>>(new Set())
  const [childrenMap, setChildrenMap] = useState<Map<string, FileEntry[]>>(new Map())
  const [selectedFile, setSelectedFile] = useState<string | null>(null)
  const [topHeight, setTopHeight] = useState(40)
  const [leftWidth, setLeftWidth] = useState<number>(() =>
    layout === 'responsive-split' ? readPersistedLeftWidth() : FILES_TAB_LEFT_WIDTH_DEFAULT,
  )

  useEffect(() => {
    if (layout !== 'responsive-split') return
    if (typeof window === 'undefined') return
    try {
      window.localStorage.setItem(FILES_TAB_LEFT_WIDTH_KEY, String(leftWidth))
    } catch {
      // Ignore storage write failures (quota, private mode, etc.)
    }
  }, [layout, leftWidth])
  const [fileContent, setFileContent] = useState<string | null>(null)
  const [fileLoading, setFileLoading] = useState(false)
  const [isEditing, setIsEditing] = useState(false)
  const [editContent, setEditContent] = useState<string>('')
  const [ctxMenu, setCtxMenu] = useState<ContextMenuState | null>(null)
  const [renaming, setRenaming] = useState<{ path: string; name: string } | null>(null)
  const [gitStatus, setGitStatus] = useState<Record<string, string>>({})
  const renameInputRef = useRef<HTMLInputElement>(null)
  const { confirm, ConfirmDialogElement } = useConfirmDialog()

  // Fetch git status
  useEffect(() => {
    if (!projectId) return
    const baseUrl = getBaseUrl()
    fetch(`${baseUrl}/api/files/git-status?project_id=${encodeURIComponent(projectId)}`)
      .then((res) => (res.ok ? res.json() : { files: {} }))
      .then((data) => setGitStatus(data.files ?? {}))
      .catch(() => setGitStatus({}))
  }, [projectId])

  // Fetch root directory
  useEffect(() => {
    if (!projectId) { setRootEntries([]); setLoading(false); return }
    setLoading(true)
    const baseUrl = getBaseUrl()
    fetch(`${baseUrl}/api/files/tree?project_id=${encodeURIComponent(projectId)}&path=`)
      .then((res) => (res.ok ? res.json() : []))
      .then((data) => setRootEntries(Array.isArray(data) ? data : []))
      .catch(() => setRootEntries([]))
      .finally(() => setLoading(false))
  }, [projectId])

  const loadChildren = useCallback((dirPath: string) => {
    if (!projectId || childrenMap.has(dirPath)) return
    const baseUrl = getBaseUrl()
    fetch(`${baseUrl}/api/files/tree?project_id=${encodeURIComponent(projectId)}&path=${encodeURIComponent(dirPath)}`)
      .then((res) => (res.ok ? res.json() : []))
      .then((data) => {
        setChildrenMap((prev) => {
          const next = new Map(prev)
          next.set(dirPath, Array.isArray(data) ? data : [])
          return next
        })
      })
      .catch(() => {
        setChildrenMap((prev) => { const next = new Map(prev); next.set(dirPath, []); return next })
      })
  }, [projectId, childrenMap])

  const toggleDir = useCallback((path: string) => {
    setExpandedPaths((prev) => {
      const next = new Set(prev)
      if (next.has(path)) { next.delete(path) } else { next.add(path); loadChildren(path) }
      return next
    })
  }, [loadChildren])

  const openFile = useCallback((path: string) => {
    if (!projectId) return
    setSelectedFile(path)
    setFileLoading(true)
    setFileContent(null)
    setIsEditing(false)
    const baseUrl = getBaseUrl()
    fetch(`${baseUrl}/api/files/read?project_id=${encodeURIComponent(projectId)}&path=${encodeURIComponent(path)}`)
      .then((res) => (res.ok ? res.json() : { content: 'Failed to load file' }))
      .then((data) => {
        const content = data.content ?? data.error ?? 'No content'
        setFileContent(content)
        setEditContent(content)
      })
      .catch(() => setFileContent('Error loading file'))
      .finally(() => setFileLoading(false))
  }, [projectId])

  // Context menu actions
  const handleContextMenu = useCallback((e: React.MouseEvent, entry: FileEntry) => {
    e.preventDefault()
    e.stopPropagation()
    setCtxMenu({ x: e.clientX, y: e.clientY, entry })
  }, [])

  const closeCtxMenu = useCallback(() => setCtxMenu(null), [])

  const handleDelete = useCallback(async (entry: FileEntry) => {
    closeCtxMenu()
    if (!projectId) return
    const ok = await confirm({ title: 'Delete file', description: `Delete "${entry.name}"?`, confirmLabel: 'Delete', destructive: true })
    if (!ok) return
    const baseUrl = getBaseUrl()
    const response = await fetch(`${baseUrl}/api/files/delete`, {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project_id: projectId, path: entry.path }),
    })
    if (!response.ok) {
      console.error(`Delete failed (${response.status}):`, await response.text().catch(() => ''))
      return
    }
    // Refresh parent directory
    const parentPath = entry.path.includes('/') ? entry.path.substring(0, entry.path.lastIndexOf('/')) : ''
    setChildrenMap((prev) => { const next = new Map(prev); next.delete(parentPath); return next })
    loadChildren(parentPath)
    if (selectedFile === entry.path) { setSelectedFile(null); setFileContent(null) }
  }, [projectId, confirm, closeCtxMenu, loadChildren, selectedFile])

  const handleRename = useCallback((entry: FileEntry) => {
    closeCtxMenu()
    setRenaming({ path: entry.path, name: entry.name })
    requestAnimationFrame(() => renameInputRef.current?.focus())
  }, [closeCtxMenu])

  const submitRename = useCallback(async () => {
    if (!renaming || !projectId) return
    const newName = renameInputRef.current?.value?.trim()
    if (!newName || newName === renaming.name) { setRenaming(null); return }
    const baseUrl = getBaseUrl()
    const parentPath = renaming.path.includes('/') ? renaming.path.substring(0, renaming.path.lastIndexOf('/')) : ''
    const newPath = parentPath ? `${parentPath}/${newName}` : newName
    const response = await fetch(`${baseUrl}/api/files/rename`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project_id: projectId, path: renaming.path, new_path: newPath }),
    })
    if (!response.ok) {
      console.error(`Rename failed (${response.status}):`, await response.text().catch(() => ''))
      setRenaming(null)
      return
    }
    setRenaming(null)
    setChildrenMap((prev) => { const next = new Map(prev); next.delete(parentPath); return next })
    loadChildren(parentPath)
  }, [renaming, projectId, loadChildren])

  const handleMove = useCallback(async (entry: FileEntry) => {
    closeCtxMenu()
    if (!projectId) return
    const newPath = window.prompt('Move to path:', entry.path)
    if (!newPath || newPath === entry.path) return
    const baseUrl = getBaseUrl()
    const response = await fetch(`${baseUrl}/api/files/move`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project_id: projectId, path: entry.path, new_path: newPath }),
    })
    if (!response.ok) {
      console.error(`Move failed (${response.status}):`, await response.text().catch(() => ''))
      return
    }
    const parentPath = entry.path.includes('/') ? entry.path.substring(0, entry.path.lastIndexOf('/')) : ''
    setChildrenMap((prev) => { const next = new Map(prev); next.delete(parentPath); return next })
    loadChildren(parentPath)
  }, [projectId, closeCtxMenu, loadChildren])

  const handleDuplicate = useCallback(async (entry: FileEntry) => {
    closeCtxMenu()
    if (!projectId || entry.is_dir) return
    const baseUrl = getBaseUrl()
    const readRes = await fetch(`${baseUrl}/api/files/read?project_id=${encodeURIComponent(projectId)}&path=${encodeURIComponent(entry.path)}`)
    if (!readRes.ok) {
      console.error(`Read failed (${readRes.status}):`, await readRes.text().catch(() => ''))
      return
    }
    const { content } = await readRes.json()
    const dotIdx = entry.name.lastIndexOf('.')
    const newName = dotIdx > 0
      ? `${entry.name.substring(0, dotIdx)} copy${entry.name.substring(dotIdx)}`
      : `${entry.name} copy`
    const parentPath = entry.path.includes('/') ? entry.path.substring(0, entry.path.lastIndexOf('/')) : ''
    const newPath = parentPath ? `${parentPath}/${newName}` : newName
    const writeRes = await fetch(`${baseUrl}/api/files/write`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project_id: projectId, path: newPath, content }),
    })
    if (!writeRes.ok) {
      console.error(`Duplicate failed (${writeRes.status}):`, await writeRes.text().catch(() => ''))
      return
    }
    setChildrenMap((prev) => { const next = new Map(prev); next.delete(parentPath); return next })
    loadChildren(parentPath)
  }, [projectId, closeCtxMenu, loadChildren])

  const handleSaveEdit = useCallback(async () => {
    if (!projectId || !selectedFile) return
    const baseUrl = getBaseUrl()
    const response = await fetch(`${baseUrl}/api/files/write`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project_id: projectId, path: selectedFile, content: editContent }),
    })
    if (!response.ok) {
      console.error(`Save failed (${response.status}):`, await response.text().catch(() => ''))
      return
    }
    setFileContent(editContent)
    setIsEditing(false)
  }, [projectId, selectedFile, editContent])

  // Close context menu on outside click
  useEffect(() => {
    if (!ctxMenu) return
    const handler = () => setCtxMenu(null)
    window.addEventListener('click', handler)
    return () => window.removeEventListener('click', handler)
  }, [ctxMenu])

  if (loading) return <div className="activity-tab-empty"><p>Loading files...</p></div>
  if (!projectId) return <div className="activity-tab-empty"><p>No project selected</p></div>

  const renderEntry = (entry: FileEntry, depth: number) => {
    const isDir = entry.is_dir
    const isExpanded = expandedPaths.has(entry.path)
    const isSelected = entry.path === selectedFile
    const children = childrenMap.get(entry.path)
    const isRenaming = renaming?.path === entry.path
    const ext = entry.name.split('.').pop() ?? ''

    if (isDir) {
      return (
        <div key={entry.path}>
          <div
            className={`files-tree-item${isSelected ? ' file-tree-entry--active' : ''}`}
            style={{ paddingLeft: `${depth * 16 + 4}px` }}
            onClick={() => toggleDir(entry.path)}
            onContextMenu={(e) => handleContextMenu(e, entry)}
          >
            <span className="files-tree-arrow">{isExpanded ? '\u25BE' : '\u25B8'}</span>
            <FolderIcon open={isExpanded} />
            {isRenaming ? (
              <input
                ref={renameInputRef}
                className="file-tree-rename-input"
                defaultValue={renaming.name}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') submitRename()
                  if (e.key === 'Escape') setRenaming(null)
                }}
                onBlur={submitRename}
                onClick={(e) => e.stopPropagation()}
              />
            ) : (
              <span className={`files-tree-name${getDirStatus(entry.path) ? ' files-tree-name--modified' : ''}`}>{entry.name}</span>
            )}
          </div>
          {isExpanded && children?.map((c) => renderEntry(c, depth + 1))}
          {isExpanded && !children && (
            <div className="files-tree-loading" style={{ paddingLeft: `${(depth + 1) * 16 + 4}px` }}>Loading...</div>
          )}
        </div>
      )
    }

    return (
      <div key={entry.path}>
        <div
          className={`files-tree-item files-tree-file${isSelected ? ' file-tree-entry--active' : ''}`}
          style={{ paddingLeft: `${depth * 16 + 20}px` }}
          onClick={() => openFile(entry.path)}
          onContextMenu={(e) => handleContextMenu(e, entry)}
          draggable
          onDragStart={(e) => {
            e.dataTransfer.setData('application/x-gobby-file', entry.path)
            e.dataTransfer.effectAllowed = 'copy'
          }}
        >
          <FileIconSvg extension={ext} />
          {isRenaming ? (
            <input
              ref={renameInputRef}
              className="file-tree-rename-input"
              defaultValue={renaming.name}
              onKeyDown={(e) => {
                if (e.key === 'Enter') submitRename()
                if (e.key === 'Escape') setRenaming(null)
              }}
              onBlur={submitRename}
              onClick={(e) => e.stopPropagation()}
            />
          ) : (
            <span className={`files-tree-name${getFileStatus(entry.path) ? ` files-tree-name--${getFileStatus(entry.path)?.replace('?', 'untracked')}` : ''}`}>{entry.name}</span>
          )}
          {(() => {
            const status = getFileStatus(entry.path)
            if (status) return <GitStatusBadge status={status} />
            return null
          })()}
        </div>
      </div>
    )
  }

  // Git status helpers
  const getFileStatus = (path: string): string | null => gitStatus[path] ?? null
  const getDirStatus = (dirPath: string): boolean => {
    const prefix = dirPath ? dirPath + '/' : ''
    return Object.keys(gitStatus).some((p) => p.startsWith(prefix))
  }

  const language = selectedFile ? detectLanguage(selectedFile) : 'text'

  return (
    <div className={`flex h-full ${useHorizontal ? 'flex-row' : 'flex-col'}`}>
      {/* File tree */}
      <div
        className={`overflow-y-auto ${
          selectedFile
            ? useHorizontal ? 'border-r border-border' : 'border-b border-border'
            : 'flex-1'
        }`}
        style={
          selectedFile
            ? useHorizontal
              ? { flex: 'none', width: `${leftWidth}px` }
              : { flex: 'none', height: `${topHeight}%` }
            : undefined
        }
      >
        {rootEntries.length === 0 ? (
          <div className="activity-tab-empty"><p>No files</p></div>
        ) : (
          rootEntries.map((e) => renderEntry(e, 0))
        )}
      </div>

      {/* Resize handle */}
      {selectedFile && (
        useHorizontal
          ? <ResizeHandle direction="horizontal" onResize={setLeftWidth} panelWidth={leftWidth} minWidth={FILES_TAB_LEFT_WIDTH_MIN} maxWidth={FILES_TAB_LEFT_WIDTH_MAX} />
          : <ResizeHandle direction="vertical" onResize={setTopHeight} panelHeight={topHeight} minHeight={15} maxHeight={80} />
      )}

      {/* File viewer */}
      {selectedFile && (
        <div className={`flex-1 ${useHorizontal ? 'min-w-0' : ''} flex flex-col min-h-0`}>
          <div className="file-viewer-toolbar">
            <span className="file-viewer-path">{selectedFile}</span>
            <div className="file-viewer-actions">
              {isEditing ? (
                <>
                  <button className="file-viewer-action file-viewer-action--save" onClick={handleSaveEdit}>Save</button>
                  <button className="file-viewer-action" onClick={() => { setIsEditing(false); setEditContent(fileContent ?? '') }}>Cancel</button>
                </>
              ) : (
                <button className="file-viewer-action" onClick={() => { setIsEditing(true); setEditContent(fileContent ?? '') }}>Edit</button>
              )}
            </div>
          </div>
          <div className="files-code-viewer">
            {fileLoading ? (
              <div className="p-3 text-xs text-muted-foreground">Loading...</div>
            ) : isEditing ? (
              <CodeMirrorEditor
                content={editContent}
                language={language}
                readOnly={false}
                onChange={setEditContent}
                onSave={handleSaveEdit}
              />
            ) : language === 'markdown' ? (
              <div className="files-markdown-viewer message-content">
                <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
                  {fileContent ?? ''}
                </ReactMarkdown>
              </div>
            ) : (
              <SyntaxHighlighter
                language={language}
                style={codeTheme}
                PreTag="div"
                showLineNumbers
                lineNumberStyle={{
                  minWidth: '3em',
                  paddingRight: '1em',
                  textAlign: 'right',
                  userSelect: 'none',
                  color: '#555',
                }}
                customStyle={{
                  margin: 0,
                  borderRadius: 0,
                  minHeight: '100%',
                }}
              >
                {fileContent ?? ''}
              </SyntaxHighlighter>
            )}
          </div>
        </div>
      )}

      {/* Context menu */}
      {ctxMenu && (
        <>
          <div className="file-ctx-backdrop" onClick={closeCtxMenu} />
          <div className="file-ctx-menu" style={{ position: 'fixed', left: ctxMenu.x, top: ctxMenu.y }}>
            {onAddToChat && !ctxMenu.entry.is_dir && (
              <button className="file-ctx-item" onClick={() => { onAddToChat(ctxMenu.entry.path); closeCtxMenu() }}>
                Add to chat
              </button>
            )}
            {!ctxMenu.entry.is_dir && (
              <button className="file-ctx-item" onClick={() => handleDuplicate(ctxMenu.entry)}>Duplicate</button>
            )}
            <button className="file-ctx-item" onClick={() => handleRename(ctxMenu.entry)}>Rename</button>
            <button className="file-ctx-item" onClick={() => handleMove(ctxMenu.entry)}>Move</button>
            <button className="file-ctx-item file-ctx-item--danger" onClick={() => handleDelete(ctxMenu.entry)}>Delete</button>
          </div>
        </>
      )}
      {ConfirmDialogElement}
    </div>
  )
})

const GIT_STATUS_COLORS: Record<string, string> = {
  M: '#e5c07b',   // modified — yellow
  A: '#4ade80',   // added — green
  D: '#f87171',   // deleted — red
  R: '#60a5fa',   // renamed — blue
  '?': '#737373', // untracked — gray
  '??': '#737373',
}

function GitStatusBadge({ status }: { status: string }) {
  const label = status === '??' ? '?' : status.charAt(0)
  const color = GIT_STATUS_COLORS[status] ?? GIT_STATUS_COLORS[label] ?? '#737373'
  return (
    <span
      className="files-git-badge"
      style={{ color }}
      title={status === 'M' ? 'Modified' : status === 'A' ? 'Added' : status === 'D' ? 'Deleted' : status === 'R' ? 'Renamed' : 'Untracked'}
    >
      {label}
    </span>
  )
}
