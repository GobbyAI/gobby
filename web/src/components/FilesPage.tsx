import { useState, useCallback, useRef, useEffect } from 'react'
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter'
import { CodeMirrorEditor } from './shared/CodeMirrorEditor'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { markdownComponents } from './shared/MarkdownComponents'
import { codeTheme, CODE_CHROME_VARS } from './shared/codeTheme'
import { getLanguageColorVar, FOLDER_ICON_COLOR_VAR } from '../lib/languageColors'
import { undo, redo } from '@codemirror/commands'
import type { EditorView } from '@codemirror/view'
import type { FileEntry, OpenFile, Project, GitStatus } from '../hooks/useFiles'
import { cn } from '../lib/utils'

const PAGE_CLS = 'flex flex-1 overflow-hidden'

const SIDEBAR_CLS =
  'flex w-[var(--sidebar-width)] min-w-[var(--sidebar-width)] flex-col border-r border-[var(--border)] bg-[var(--bg-secondary)] max-md:w-[200px] max-md:min-w-[160px]'
const SIDEBAR_HEADER_CLS = 'flex items-center justify-between border-b border-[var(--border)] px-3 py-2.5'
const SIDEBAR_TITLE_CLS =
  'text-[length:var(--text-sm)] font-semibold uppercase tracking-[0.05em] text-[var(--text-muted)]'

const TREE_CLS = 'flex-1 overflow-x-hidden overflow-y-auto py-1'
const EMPTY_TREE_CLS = 'px-4 py-6 text-center text-[length:var(--text-md)] text-[var(--text-muted)]'

const PROJECT_NODE_CLS = 'mb-0.5'
const PROJECT_HEADER_CLS =
  'flex cursor-pointer select-none items-center gap-1.5 px-2 py-1.5 text-[length:var(--text-md)] font-medium text-[var(--text-primary)] transition-colors duration-100 hover:bg-[var(--bg-tertiary)] pointer-coarse:min-h-11'
const PROJECT_NAME_CLS = 'overflow-hidden text-ellipsis whitespace-nowrap'

const TREE_ARROW_CLS = 'w-2.5 shrink-0 text-center text-[length:var(--text-2xs)] text-[var(--text-muted)]'
const TREE_ITEM_CLS =
  'flex cursor-pointer select-none items-center gap-1.5 px-2 py-0.5 text-[length:var(--text-md)] text-[var(--text-secondary)] transition-colors duration-100 hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)] pointer-coarse:min-h-11'
const TREE_NAME_CLS = 'overflow-hidden text-ellipsis whitespace-nowrap'
const TREE_GIT_BADGE_CLS = 'ml-auto shrink-0 font-mono text-[length:var(--text-2xs)] font-semibold opacity-85'
const TREE_LOADING_CLS = 'px-2 py-1 text-[length:var(--text-sm)] italic text-[var(--text-muted)]'

const MAIN_CLS = 'relative flex flex-1 flex-col overflow-hidden bg-[var(--bg-primary)]'

const TABS_CLS =
  'flex overflow-x-auto border-b border-[var(--border)] bg-[var(--bg-secondary)] [scrollbar-width:thin]'
const TAB_CLS =
  'group flex min-w-0 cursor-pointer select-none items-center gap-1.5 whitespace-nowrap border-r border-[var(--border)] px-3 py-2 text-[length:var(--text-md)] text-[var(--text-muted)] transition-colors duration-100 hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-secondary)] pointer-coarse:min-h-11'
const TAB_ACTIVE_CLS = '-mb-px border-b-2 border-b-[var(--accent)] bg-[var(--bg-primary)] text-[var(--text-primary)]'
const TAB_NAME_CLS = 'overflow-hidden text-ellipsis'
const TAB_CLOSE_CLS =
  'flex h-4 w-4 shrink-0 cursor-pointer items-center justify-center rounded-sm border-0 bg-transparent p-0 text-[length:var(--text-base)] leading-none text-[var(--text-muted)] opacity-0 transition-opacity duration-100 group-hover:opacity-100 hover:bg-[rgba(255,255,255,0.1)] hover:text-[var(--text-primary)] pointer-coarse:h-11 pointer-coarse:w-11'

const TOOLBAR_CLS =
  'flex min-h-8 items-center justify-between border-b border-[var(--border)] bg-[var(--bg-secondary)] px-3 py-1 text-[length:var(--text-sm)]'
const TOOLBAR_PATH_CLS = 'overflow-hidden text-ellipsis whitespace-nowrap text-[var(--text-muted)]'
const TOOLBAR_ACTIONS_CLS = 'flex shrink-0 items-center gap-1.5'

const TOOLBAR_BTN_BASE_CLS =
  'cursor-pointer rounded border border-[var(--border)] bg-transparent px-2 py-0.5 text-[length:var(--text-sm)] text-[var(--text-secondary)] transition-colors duration-150 hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)] pointer-coarse:min-h-11'
const ICON_BTN_CLS =
  'flex cursor-pointer items-center justify-center rounded border border-[var(--border)] bg-transparent px-1.5 py-0.5 text-[var(--text-secondary)] transition-colors duration-150 hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)] pointer-coarse:h-11 pointer-coarse:w-11'
const SAVE_BTN_CLS =
  'cursor-pointer rounded border-0 bg-[var(--color-success-foreground)] px-2.5 py-0.5 text-[length:var(--text-sm)] font-medium text-[var(--accent-foreground)] transition-colors duration-150 hover:bg-[color-mix(in_srgb,var(--color-success-foreground)_85%,var(--text-primary))] disabled:cursor-not-allowed disabled:opacity-60 pointer-coarse:min-h-11'
const DIFF_BTN_ACTIVE_CLS =
  'border-[var(--color-warning-foreground)] bg-[var(--color-warning-soft)] text-[var(--color-warning-foreground)]'

const VIEWER_CLS = 'flex flex-1 flex-col overflow-hidden'
const CODE_VIEWER_CLS = 'min-h-0 flex-1 overflow-auto [&>div]:min-h-full'

const MARKDOWN_VIEWER_CLS =
  'overflow-wrap-break-word px-6 py-4 text-[length:var(--text-base)] leading-[1.7] text-[var(--text-primary)]'

const EMPTY_VIEWER_CLS =
  'flex flex-1 flex-col items-center justify-center gap-3 text-[length:var(--text-base)] text-[var(--text-muted)]'

const VIEWER_STATUS_CLS =
  'flex flex-1 flex-col items-center justify-center gap-2 text-[length:var(--text-base)] text-[var(--text-muted)]'
const VIEWER_ERROR_CLS = 'text-[var(--color-error)]'
const VIEWER_MUTED_CLS = 'text-[length:var(--text-sm)] text-[var(--text-muted)]'

const IMAGE_VIEWER_CLS = 'flex flex-1 flex-col items-center justify-center gap-4 overflow-auto p-8'
const IMAGE_PREVIEW_CLS =
  'max-h-[calc(100%-3rem)] max-w-full rounded-lg object-contain [background:repeating-conic-gradient(var(--bg-primary)_0%_25%,var(--bg-secondary)_0%_50%)_50%/20px_20px]'
const IMAGE_INFO_CLS = 'text-[length:var(--text-sm)] text-[var(--text-muted)]'

const BRANCH_BADGE_CLS =
  'ml-auto rounded-sm bg-[var(--bg-tertiary)] px-1.5 py-px text-[length:var(--text-xs)] font-normal text-[var(--text-muted)]'

const CONFIRM_OVERLAY_CLS = 'absolute inset-0 z-[100] flex items-center justify-center bg-[var(--surface-scrim)]'
const CONFIRM_DIALOG_CLS =
  'w-[90%] max-w-[340px] rounded-lg border border-[var(--border)] bg-[var(--bg-secondary)] p-5'
const CONFIRM_TITLE_CLS = 'mb-1.5 mt-0 text-[length:var(--text-base)] font-semibold text-[var(--text-primary)]'
const CONFIRM_MESSAGE_CLS = 'mb-4 mt-0 text-[length:var(--text-base)] text-[var(--text-secondary)]'
const CONFIRM_ACTIONS_CLS = 'flex justify-end gap-2'
const CONFIRM_KEEP_CLS =
  'cursor-pointer rounded border border-[var(--border)] bg-transparent px-3 py-1 text-[length:var(--text-sm)] text-[var(--text-secondary)] transition-colors duration-150 hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)] pointer-coarse:min-h-11'
const CONFIRM_DISCARD_CLS =
  'cursor-pointer rounded border-0 bg-[var(--color-error)] px-3 py-1 text-[length:var(--text-sm)] font-medium text-[var(--accent-foreground)] transition-colors duration-150 hover:bg-[color-mix(in_srgb,var(--color-error)_85%,var(--text-primary))] pointer-coarse:min-h-11'

const CONFIRM_DIALOG_QUERY_CLS = 'files-confirm-dialog'

const GIT_M_CLS = 'text-[var(--color-warning-foreground)]'
const GIT_A_CLS = 'text-[var(--color-success-foreground)]'
const GIT_D_CLS = 'text-[var(--color-error)]'
const GIT_R_CLS = 'text-[var(--color-info)]'

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

  const previousFocusRef = useRef<Element | null>(null)
  useEffect(() => {
    if (!showCancelConfirm) return
    previousFocusRef.current = document.activeElement
    const dialog = document.querySelector(`.${CONFIRM_DIALOG_QUERY_CLS}`) as HTMLElement | null
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

  const activeGitStatus = activeFile ? gitStatuses.get(activeFile.projectId) : undefined
  const activeFileGitStatus = activeFile && activeGitStatus ? activeGitStatus.files[activeFile.path] : undefined

  return (
    <div className={PAGE_CLS}>
      <div className={SIDEBAR_CLS}>
        <div className={SIDEBAR_HEADER_CLS}>
          <span className={SIDEBAR_TITLE_CLS}>Explorer</span>
        </div>
        <div className={TREE_CLS}>
          {projects.length === 0 ? (
            <div className={EMPTY_TREE_CLS}>No projects registered</div>
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

      <div className={MAIN_CLS}>
        {openFiles.length > 0 && (
          <div className={TABS_CLS}>
            {openFiles.map((file, i) => {
              const isActive = i === activeFileIndex
              return (
                <div
                  key={`${file.projectId}:${file.path}`}
                  className={cn(TAB_CLS, isActive && TAB_ACTIVE_CLS)}
                  onClick={() => onSetActiveFile(i)}
                >
                  <FileIcon extension={file.name.split('.').pop() || ''} size={14} />
                  <span className={TAB_NAME_CLS}>{file.dirty ? `${file.name} ●` : file.name}</span>
                  <button
                    className={cn(TAB_CLOSE_CLS, isActive && 'opacity-100')}
                    onClick={(e) => {
                      e.stopPropagation()
                      onCloseFile(i)
                    }}
                  >
                    &times;
                  </button>
                </div>
              )
            })}
          </div>
        )}

        {activeFile && !activeFile.image && !activeFile.binary && !activeFile.loading && !activeFile.error && activeFile.content !== null && (
          <div className={TOOLBAR_CLS}>
            <span className={TOOLBAR_PATH_CLS}>{activeFile.path}</span>
            <div className={TOOLBAR_ACTIONS_CLS}>
              {activeFileGitStatus && (
                <button
                  className={cn(TOOLBAR_BTN_BASE_CLS, showDiff && DIFF_BTN_ACTIVE_CLS)}
                  onClick={handleShowDiff}
                >
                  Diff
                </button>
              )}
              {activeFile.editing ? (
                <>
                  <button className={ICON_BTN_CLS} onClick={handleUndo} title="Undo (Cmd+Z)">
                    <UndoIcon />
                  </button>
                  <button className={ICON_BTN_CLS} onClick={handleRedo} title="Redo (Cmd+Shift+Z)">
                    <RedoIcon />
                  </button>
                  <button
                    className={TOOLBAR_BTN_BASE_CLS}
                    onClick={handleCancel}
                  >
                    Cancel
                  </button>
                  <button
                    className={SAVE_BTN_CLS}
                    onClick={() => onSaveFile(activeFileIndex)}
                    disabled={activeFile.saving || !activeFile.dirty}
                  >
                    {activeFile.saving ? 'Saving...' : 'Save'}
                  </button>
                </>
              ) : (
                <button
                  className={TOOLBAR_BTN_BASE_CLS}
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

        <div className={VIEWER_CLS}>
          {showDiff && diffContent !== null ? (
            <div className={CODE_VIEWER_CLS}>
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
                  color: CODE_CHROME_VARS.gutterText,
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
            <div className={EMPTY_VIEWER_CLS}>
              <FilesPlaceholderIcon />
              <p>Select a file to view</p>
            </div>
          )}
        </div>

        {showCancelConfirm && (
          <div className={CONFIRM_OVERLAY_CLS} onClick={() => setShowCancelConfirm(false)}>
            <div
              className={cn(CONFIRM_DIALOG_CLS, CONFIRM_DIALOG_QUERY_CLS)}
              role="dialog"
              aria-modal="true"
              aria-labelledby="cancel-dialog-title"
              aria-describedby="cancel-dialog-desc"
              tabIndex={-1}
              onClick={e => e.stopPropagation()}
            >
              <p className={CONFIRM_TITLE_CLS} id="cancel-dialog-title">Discard unsaved changes?</p>
              <p className={CONFIRM_MESSAGE_CLS} id="cancel-dialog-desc">Your changes to this file will be lost.</p>
              <div className={CONFIRM_ACTIONS_CLS}>
                <button className={CONFIRM_KEEP_CLS} onClick={() => setShowCancelConfirm(false)}>
                  Keep Editing
                </button>
                <button className={CONFIRM_DISCARD_CLS} onClick={confirmCancel}>
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
    <div className={PROJECT_NODE_CLS}>
      <div className={PROJECT_HEADER_CLS} onClick={onToggle}>
        <span className={TREE_ARROW_CLS}>{isExpanded ? '▾' : '▸'}</span>
        <ProjectIcon />
        <span className={PROJECT_NAME_CLS}>{project.name}</span>
        {gitStatus?.branch && (
          <span className={BRANCH_BADGE_CLS}>{gitStatus.branch}</span>
        )}
      </div>
      {isExpanded && (
        <div>
          {isLoading ? (
            <div className={TREE_LOADING_CLS}>Loading...</div>
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

function getGitStatusClass(status: string | undefined): string | undefined {
  if (!status) return undefined
  if (status === 'M' || status === 'MM' || status === 'AM') return GIT_M_CLS
  if (status === '??' || status === 'A') return GIT_A_CLS
  if (status === 'D') return GIT_D_CLS
  if (status === 'R') return GIT_R_CLS
  return GIT_M_CLS
}

function TreeEntry({ entry, projectId, depth, expandedDirs, loadingDirs, gitFiles, onExpandDir, onOpenFile }: TreeEntryProps) {
  const key = `${projectId}:${entry.path}`
  const isExpanded = expandedDirs.has(key)
  const isLoading = loadingDirs.has(key)
  const children = expandedDirs.get(key) || []
  const gitStatus = gitFiles?.[entry.path]
  const gitClass = getGitStatusClass(gitStatus)

  if (entry.is_dir) {
    return (
      <div>
        <div
          className={TREE_ITEM_CLS}
          style={{ paddingLeft: `${depth * 16 + 4}px` }}
          onClick={() => onExpandDir(projectId, entry.path)}
        >
          <span className={TREE_ARROW_CLS}>{isExpanded ? '▾' : '▸'}</span>
          <FolderIcon open={isExpanded} />
          <span className={TREE_NAME_CLS}>{entry.name}</span>
        </div>
        {isExpanded && (
          <div>
            {isLoading ? (
              <div className={TREE_LOADING_CLS} style={{ paddingLeft: `${(depth + 1) * 16 + 4}px` }}>
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
      className={TREE_ITEM_CLS}
      style={{ paddingLeft: `${depth * 16 + 20}px` }}
      onClick={() => onOpenFile(projectId, entry.path, entry.name)}
    >
      <FileIcon extension={entry.extension?.replace('.', '') || ''} size={14} />
      <span className={cn(TREE_NAME_CLS, gitClass)}>{entry.name}</span>
      {gitStatus && (
        <span className={cn(TREE_GIT_BADGE_CLS, gitClass)}>
          {gitStatus === '??' ? '?' : gitStatus.charAt(0)}
        </span>
      )}
    </div>
  )
}

function FileContent({ file, getImageUrl, onContentChange, onSave, editorViewRef }: {
  file: OpenFile
  getImageUrl: (projectId: string, path: string) => string
  onContentChange: (content: string) => void
  onSave: () => void
  editorViewRef?: React.MutableRefObject<EditorView | null>
}) {
  if (file.loading) {
    return <div className={VIEWER_STATUS_CLS}>Loading...</div>
  }

  if (file.error) {
    return <div className={cn(VIEWER_STATUS_CLS, VIEWER_ERROR_CLS)}>Error: {file.error}</div>
  }

  if (file.image) {
    return (
      <div className={IMAGE_VIEWER_CLS}>
        <img
          src={getImageUrl(file.projectId, file.path)}
          alt={file.name}
          className={IMAGE_PREVIEW_CLS}
        />
        <div className={IMAGE_INFO_CLS}>
          {file.name} &middot; {formatSize(file.size)} &middot; {file.mime_type}
        </div>
      </div>
    )
  }

  if (file.binary) {
    return (
      <div className={VIEWER_STATUS_CLS}>
        <BinaryIcon />
        <p>Binary file &middot; {formatSize(file.size)}</p>
        <p className={VIEWER_MUTED_CLS}>{file.mime_type}</p>
      </div>
    )
  }

  if (file.content === null) {
    return <div className={VIEWER_STATUS_CLS}>No content</div>
  }

  if (file.editing) {
    return (
      <div className={CODE_VIEWER_CLS}>
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
      <div className={CODE_VIEWER_CLS}>
        <div className={cn(MARKDOWN_VIEWER_CLS, 'message-content')}>
          <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
            {file.content}
          </ReactMarkdown>
        </div>
      </div>
    )
  }

  return (
    <div className={CODE_VIEWER_CLS}>
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
          color: CODE_CHROME_VARS.gutterText,
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

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1048576) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1048576).toFixed(1)} MB`
}

function ProjectIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="2" y="3" width="20" height="14" rx="2" ry="2" />
      <line x1="8" y1="21" x2="16" y2="21" />
      <line x1="12" y1="17" x2="12" y2="21" />
    </svg>
  )
}

function FolderIcon({ open }: { open: boolean }) {
  if (open) {
    return (
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke={FOLDER_ICON_COLOR_VAR} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
        <line x1="9" y1="14" x2="15" y2="14" />
      </svg>
    )
  }
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke={FOLDER_ICON_COLOR_VAR} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
    </svg>
  )
}

function FileIcon({ extension, size = 14 }: { extension: string; size?: number }) {
  const color = getLanguageColorVar(extension)

  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z" />
      <polyline points="13 2 13 9 20 9" />
    </svg>
  )
}

function BinaryIcon() {
  return (
    <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="var(--text-muted)" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z" />
      <polyline points="13 2 13 9 20 9" />
      <line x1="9" y1="13" x2="15" y2="13" />
      <line x1="9" y1="17" x2="15" y2="17" />
    </svg>
  )
}

function FilesPlaceholderIcon() {
  return (
    <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="var(--text-muted)" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
    </svg>
  )
}
