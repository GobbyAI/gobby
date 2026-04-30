import { useState, useCallback, useRef, useEffect } from 'react'
import './FilesPage.css'
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter'
import { CodeMirrorEditor } from './shared/CodeMirrorEditor'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { markdownComponents } from './shared/MarkdownComponents'
import { codeTheme } from './shared/codeTheme'
import { undo, redo } from '@codemirror/commands'
import type { EditorView } from '@codemirror/view'
import type { FileEntry, OpenFile, Project, GitStatus } from '../hooks/useFiles'

interface FilesPageProps {
  projects: Project[]
  expandedDirs: Map<string, FileEntry[]>
  expandedProjects: Set<string>
  openFiles: OpenFile[]
  activeFileIndex: number
  loadingDirs: Set<string>
  onExpandProject: (projectId: string) => void
  onExpandDir: (projectId: string, path: string) => void
  onOpenFile: (projectId: string, path: string, name: string) => void
  onCloseFile: (index: number) => void
  onSetActiveFile: (index: number) => void
  getImageUrl: (projectId: string, path: string) => string
  onToggleEditing: (index: number) => void
  onCancelEditing: (index: number) => void
  onUpdateEditContent: (index: number, content: string) => void
  onSaveFile: (index: number) => void
  gitStatuses: Map<string, GitStatus>
  onFetchDiff: (projectId: string, path: string) => Promise<string>
}

export function FilesPage({
  projects,
  expandedDirs,
  expandedProjects,
  openFiles,
  activeFileIndex,
  loadingDirs,
  onExpandProject,
  onExpandDir,
  onOpenFile,
  onCloseFile,
  onSetActiveFile,
  getImageUrl,
  onToggleEditing,
  onCancelEditing,
  onUpdateEditContent,
  onSaveFile,
  gitStatuses,
  onFetchDiff,
}: FilesPageProps) {
  const activeFile = activeFileIndex >= 0 ? openFiles[activeFileIndex] : null
  const [diffContent, setDiffContent] = useState<string | null>(null)
  const [showDiff, setShowDiff] = useState(false)
  const [showCancelConfirm, setShowCancelConfirm] = useState(false)
  const editorViewRef = useRef<EditorView | null>(null)

  const cancelIndexRef = useRef(activeFileIndex)

  const handleCancel = useCallback(() => {
    if (activeFile?.dirty) {
      cancelIndexRef.current = activeFileIndex
      setShowCancelConfirm(true)
    } else {
      onCancelEditing(activeFileIndex)
      setShowDiff(false)
    }
  }, [activeFile, activeFileIndex, onCancelEditing])

  const confirmCancel = useCallback(() => {
    setShowCancelConfirm(false)
    onCancelEditing(cancelIndexRef.current)
    setShowDiff(false)
  }, [onCancelEditing])

  // Keyboard accessibility for cancel confirmation dialog
  const previousFocusRef = useRef<Element | null>(null)
  useEffect(() => {
    if (!showCancelConfirm) return
    previousFocusRef.current = document.activeElement
    const dialog = document.querySelector('.files-confirm-dialog') as HTMLElement | null
    dialog?.focus()
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setShowCancelConfirm(false)
      } else if (e.key === 'Tab' && dialog) {
        const focusable = dialog.querySelectorAll<HTMLElement>('button, [tabindex]')
        if (focusable.length === 0) return
        const first = focusable[0]
        const last = focusable[focusable.length - 1]
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault()
          last.focus()
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault()
          first.focus()
        }
      }
    }
    document.addEventListener('keydown', handleKeyDown)
    return () => {
      document.removeEventListener('keydown', handleKeyDown)
      if (previousFocusRef.current instanceof HTMLElement) {
        previousFocusRef.current.focus()
      }
    }
  }, [showCancelConfirm])

  const handleUndo = useCallback(() => {
    if (editorViewRef.current) undo(editorViewRef.current)
  }, [])

  const handleRedo = useCallback(() => {
    if (editorViewRef.current) redo(editorViewRef.current)
  }, [])

  const handleShowDiff = useCallback(async () => {
    if (!activeFile) return
    if (showDiff) {
      setShowDiff(false)
      setDiffContent(null)
      return
    }
    const diff = await onFetchDiff(activeFile.projectId, activeFile.path)
    setDiffContent(diff)
    setShowDiff(true)
  }, [activeFile, showDiff, onFetchDiff])

  // Get git status for active file's project
  const activeGitStatus = activeFile ? gitStatuses.get(activeFile.projectId) : undefined
  const activeFileGitStatus = activeFile && activeGitStatus ? activeGitStatus.files[activeFile.path] : undefined

  return (
    <div className="files-page">
      <div className="files-sidebar">
        <div className="files-sidebar-header">
          <span className="files-sidebar-title">Explorer</span>
        </div>
        <div className="files-tree">
          {projects.length === 0 ? (
            <div className="files-empty-tree">No projects registered</div>
          ) : (
            projects.map(project => (
              <ProjectNode
                key={project.id}
                project={project}
                isExpanded={expandedProjects.has(project.id)}
                expandedDirs={expandedDirs}
                loadingDirs={loadingDirs}
                gitStatus={gitStatuses.get(project.id)}
                onToggle={() => onExpandProject(project.id)}
                onExpandDir={onExpandDir}
                onOpenFile={onOpenFile}
              />
            ))
          )}
        </div>
      </div>

      <div className="files-main">
        {openFiles.length > 0 && (
          <div className="files-tabs">
            {openFiles.map((file, i) => (
              <div
                key={`${file.projectId}:${file.path}`}
                className={`files-tab ${i === activeFileIndex ? 'active' : ''}`}
                onClick={() => onSetActiveFile(i)}
              >
                <FileIcon extension={file.name.split('.').pop() || ''} size={14} />
                <span className="files-tab-name">{file.dirty ? `${file.name} \u25CF` : file.name}</span>
                <button
                  className="files-tab-close"
                  onClick={(e) => {
                    e.stopPropagation()
                    onCloseFile(i)
                  }}
                >
                  &times;
                </button>
              </div>
            ))}
          </div>
        )}

        {activeFile && !activeFile.image && !activeFile.binary && !activeFile.loading && !activeFile.error && activeFile.content !== null && (
          <div className="files-toolbar">
            <span className="files-toolbar-path">{activeFile.path}</span>
            <div className="files-toolbar-actions">
              {activeFileGitStatus && (
                <button
                  className={`files-diff-btn ${showDiff ? 'active' : ''}`}
                  onClick={handleShowDiff}
                >
                  Diff
                </button>
              )}
              {activeFile.editing ? (
                <>
                  <button className="files-undo-btn" onClick={handleUndo} title="Undo (Cmd+Z)">
                    <UndoIcon />
                  </button>
                  <button className="files-redo-btn" onClick={handleRedo} title="Redo (Cmd+Shift+Z)">
                    <RedoIcon />
                  </button>
                  <button
                    className="files-cancel-btn"
                    onClick={handleCancel}
                  >
                    Cancel
                  </button>
                  <button
                    className="files-save-btn"
                    onClick={() => onSaveFile(activeFileIndex)}
                    disabled={activeFile.saving || !activeFile.dirty}
                  >
                    {activeFile.saving ? 'Saving...' : 'Save'}
                  </button>
                </>
              ) : (
                <button
                  className="files-edit-toggle"
                  onClick={() => {
                    onToggleEditing(activeFileIndex)
                    setShowDiff(false)
                  }}
                >
                  Edit
                </button>
              )}
            </div>
          </div>
        )}

        <div className="files-viewer">
          {showDiff && diffContent !== null ? (
            <div className="files-code-viewer">
              <SyntaxHighlighter
                style={codeTheme}
                language="diff"
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
                {diffContent || '(no changes)'}
              </SyntaxHighlighter>
            </div>
          ) : activeFile ? (
            <FileContent
              file={activeFile}
              getImageUrl={getImageUrl}
              onContentChange={(content) => onUpdateEditContent(activeFileIndex, content)}
              onSave={() => onSaveFile(activeFileIndex)}
              editorViewRef={editorViewRef}
            />
          ) : (
            <div className="files-empty-viewer">
              <FilesPlaceholderIcon />
              <p>Select a file to view</p>
            </div>
          )}
        </div>

        {showCancelConfirm && (
          <div className="files-confirm-overlay" onClick={() => setShowCancelConfirm(false)}>
            <div className="files-confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="cancel-dialog-title" aria-describedby="cancel-dialog-desc" tabIndex={-1} onClick={e => e.stopPropagation()}>
              <p className="files-confirm-title" id="cancel-dialog-title">Discard unsaved changes?</p>
              <p className="files-confirm-message" id="cancel-dialog-desc">Your changes to this file will be lost.</p>
              <div className="files-confirm-actions">
                <button className="files-confirm-keep" onClick={() => setShowCancelConfirm(false)}>
                  Keep Editing
                </button>
                <button className="files-confirm-discard" onClick={confirmCancel}>
                  Discard
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

// -- File Tree Components --

interface ProjectNodeProps {
  project: Project
  isExpanded: boolean
  expandedDirs: Map<string, FileEntry[]>
  loadingDirs: Set<string>
  gitStatus?: GitStatus
  onToggle: () => void
  onExpandDir: (projectId: string, path: string) => void
  onOpenFile: (projectId: string, path: string, name: string) => void
}

function ProjectNode({ project, isExpanded, expandedDirs, loadingDirs, gitStatus, onToggle, onExpandDir, onOpenFile }: ProjectNodeProps) {
  const rootKey = `${project.id}:`
  const rootEntries = expandedDirs.get(rootKey) || []
  const isLoading = loadingDirs.has(rootKey)

  return (
    <div className="files-project-node">
      <div className="files-project-header" onClick={onToggle}>
        <span className="files-tree-arrow">{isExpanded ? '\u25BE' : '\u25B8'}</span>
        <ProjectIcon />
        <span className="files-project-name">{project.name}</span>
        {gitStatus?.branch && (
          <span className="files-branch-badge">{gitStatus.branch}</span>
        )}
      </div>
      {isExpanded && (
        <div className="files-project-children">
          {isLoading ? (
            <div className="files-tree-loading">Loading...</div>
          ) : (
            rootEntries.map(entry => (
              <TreeEntry
                key={entry.path}
                entry={entry}
                projectId={project.id}
                depth={1}
                expandedDirs={expandedDirs}
                loadingDirs={loadingDirs}
                gitFiles={gitStatus?.files}
                onExpandDir={onExpandDir}
                onOpenFile={onOpenFile}
              />
            ))
          )}
        </div>
      )}
    </div>
  )
}

interface TreeEntryProps {
  entry: FileEntry
  projectId: string
  depth: number
  expandedDirs: Map<string, FileEntry[]>
  loadingDirs: Set<string>
  gitFiles?: Record<string, string>
  onExpandDir: (projectId: string, path: string) => void
  onOpenFile: (projectId: string, path: string, name: string) => void
}

function getGitStatusColor(status: string | undefined): string | undefined {
  if (!status) return undefined
  if (status === 'M' || status === 'MM' || status === 'AM') return 'var(--color-warning-foreground)'
  if (status === '??' || status === 'A') return 'var(--color-success-foreground)'
  if (status === 'D') return 'var(--color-error)'
  if (status === 'R') return 'var(--color-info)'
  return 'var(--color-warning-foreground)'
}

function TreeEntry({ entry, projectId, depth, expandedDirs, loadingDirs, gitFiles, onExpandDir, onOpenFile }: TreeEntryProps) {
  const key = `${projectId}:${entry.path}`
  const isExpanded = expandedDirs.has(key)
  const isLoading = loadingDirs.has(key)
  const children = expandedDirs.get(key) || []
  const gitStatus = gitFiles?.[entry.path]
  const gitColor = getGitStatusColor(gitStatus)

  if (entry.is_dir) {
    return (
      <div className="files-tree-dir">
        <div
          className="files-tree-item"
          style={{ paddingLeft: `${depth * 16 + 4}px` }}
          onClick={() => onExpandDir(projectId, entry.path)}
        >
          <span className="files-tree-arrow">{isExpanded ? '\u25BE' : '\u25B8'}</span>
          <FolderIcon open={isExpanded} />
          <span className="files-tree-name">{entry.name}</span>
        </div>
        {isExpanded && (
          <div className="files-tree-children">
            {isLoading ? (
              <div className="files-tree-loading" style={{ paddingLeft: `${(depth + 1) * 16 + 4}px` }}>
                Loading...
              </div>
            ) : (
              children.map(child => (
                <TreeEntry
                  key={child.path}
                  entry={child}
                  projectId={projectId}
                  depth={depth + 1}
                  expandedDirs={expandedDirs}
                  loadingDirs={loadingDirs}
                  gitFiles={gitFiles}
                  onExpandDir={onExpandDir}
                  onOpenFile={onOpenFile}
                />
              ))
            )}
          </div>
        )}
      </div>
    )
  }

  return (
    <div
      className="files-tree-item files-tree-file"
      style={{ paddingLeft: `${depth * 16 + 20}px` }}
      onClick={() => onOpenFile(projectId, entry.path, entry.name)}
    >
      <FileIcon extension={entry.extension?.replace('.', '') || ''} size={14} />
      <span className="files-tree-name" style={gitColor ? { color: gitColor } : undefined}>{entry.name}</span>
      {gitStatus && (
        <span className="files-tree-git-badge" style={{ color: gitColor }}>
          {gitStatus === '??' ? '?' : gitStatus.charAt(0)}
        </span>
      )}
    </div>
  )
}

// -- File Viewer --

function FileContent({ file, getImageUrl, onContentChange, onSave, editorViewRef }: {
  file: OpenFile
  getImageUrl: (projectId: string, path: string) => string
  onContentChange: (content: string) => void
  onSave: () => void
  editorViewRef?: React.MutableRefObject<EditorView | null>
}) {
  if (file.loading) {
    return <div className="files-viewer-status">Loading...</div>
  }

  if (file.error) {
    return <div className="files-viewer-status files-viewer-error">Error: {file.error}</div>
  }

  if (file.image) {
    return (
      <div className="files-image-viewer">
        <img
          src={getImageUrl(file.projectId, file.path)}
          alt={file.name}
          className="files-image-preview"
        />
        <div className="files-image-info">
          {file.name} &middot; {formatSize(file.size)} &middot; {file.mime_type}
        </div>
      </div>
    )
  }

  if (file.binary) {
    return (
      <div className="files-viewer-status">
        <BinaryIcon />
        <p>Binary file &middot; {formatSize(file.size)}</p>
        <p className="files-viewer-muted">{file.mime_type}</p>
      </div>
    )
  }

  if (file.content === null) {
    return <div className="files-viewer-status">No content</div>
  }

  if (file.editing) {
    return (
      <div className="files-code-viewer">
        <CodeMirrorEditor
          content={file.editContent ?? file.content}
          language={file.language}
          readOnly={false}
          onChange={onContentChange}
          onSave={onSave}
          editorViewRef={editorViewRef}
        />
      </div>
    )
  }

  if (file.language === 'markdown') {
    return (
      <div className="files-code-viewer">
        <div className="files-markdown-viewer message-content">
          <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
            {file.content}
          </ReactMarkdown>
        </div>
      </div>
    )
  }

  return (
    <div className="files-code-viewer">
      <SyntaxHighlighter
        style={codeTheme}
        language={file.language}
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
        {file.content}
      </SyntaxHighlighter>
    </div>
  )
}

// -- Undo/Redo Icons --

function UndoIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="1 4 1 10 7 10" />
      <path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10" />
    </svg>
  )
}

function RedoIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="23 4 23 10 17 10" />
      <path d="M20.49 15a9 9 0 1 1-2.13-9.36L23 10" />
    </svg>
  )
}

// -- Utilities --

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1048576) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1048576).toFixed(1)} MB`
}

// -- Icons --

function ProjectIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#3b82f6" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="2" y="3" width="20" height="14" rx="2" ry="2" />
      <line x1="8" y1="21" x2="16" y2="21" />
      <line x1="12" y1="17" x2="12" y2="21" />
    </svg>
  )
}

function FolderIcon({ open }: { open: boolean }) {
  if (open) {
    return (
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#facc15" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
        <line x1="9" y1="14" x2="15" y2="14" />
      </svg>
    )
  }
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#facc15" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
    </svg>
  )
}

function FileIcon({ extension, size = 14 }: { extension: string; size?: number }) {
  // Color-code by file type
  let color = '#a3a3a3'
  const ext = extension.toLowerCase()
  if (['ts', 'tsx'].includes(ext)) color = '#3178c6'
  else if (['js', 'jsx'].includes(ext)) color = '#f7df1e'
  else if (ext === 'py') color = '#3776ab'
  else if (ext === 'rs') color = '#ce422b'
  else if (ext === 'go') color = '#00add8'
  else if (['json', 'yaml', 'yml', 'toml'].includes(ext)) color = '#cb8742'
  else if (['md', 'txt', 'rst'].includes(ext)) color = '#737373'
  else if (['css', 'scss', 'less'].includes(ext)) color = '#563d7c'
  else if (['html', 'htm'].includes(ext)) color = '#e34c26'
  else if (['sh', 'bash', 'zsh'].includes(ext)) color = '#4eaa25'
  else if (['sql'].includes(ext)) color = '#e38c00'
  else if (['rb'].includes(ext)) color = '#cc342d'

  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z" />
      <polyline points="13 2 13 9 20 9" />
    </svg>
  )
}

function BinaryIcon() {
  return (
    <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="#737373" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z" />
      <polyline points="13 2 13 9 20 9" />
      <line x1="9" y1="13" x2="15" y2="13" />
      <line x1="9" y1="17" x2="15" y2="17" />
    </svg>
  )
}

function FilesPlaceholderIcon() {
  return (
    <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="#737373" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
    </svg>
  )
}
