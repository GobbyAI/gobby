import { useState, useCallback, useRef } from 'react'
import { CodeBlock } from './shared/CodeBlock'
import { Button } from './ui/Button'
import { Card } from './ui/Card'
import { ConfirmDialog } from './ui/ConfirmDialog'
import { TabBar } from './ui/TabBar'
import { coarseHitAreaCls } from './ui/controlStyles'
import { CodeMirrorEditor } from './shared/CodeMirrorEditor'
import { MarkdownBody, markdownBodyClassName } from './shared/MarkdownBody'
import { getLanguageColorVar, FOLDER_ICON_COLOR_VAR } from '../lib/languageColors'
import { undo, redo } from '@codemirror/commands'
import type { EditorView } from '@codemirror/view'
import type { FileEntry, OpenFile, Project, GitStatus } from '../hooks/useFiles'
import { cn } from '../lib/utils'
import { activateOnKeyboard } from '../lib/keyboard'

const GIT_STATUS_CLASS_BY_CODE: Readonly<Record<string, string>> = {
  M: 'text-[var(--color-warning-foreground)]',
  MM: 'text-[var(--color-warning-foreground)]',
  AM: 'text-[var(--color-warning-foreground)]',
  '??': 'text-[var(--color-success-foreground)]',
  A: 'text-[var(--color-success-foreground)]',
  D: 'text-[var(--color-error)]',
  R: 'text-[var(--color-info)]',
}

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
  onClearSaveError: (index: number) => void
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
  onClearSaveError,
  onSaveFile,
  gitStatuses,
  onFetchDiff,
}: FilesPageProps) {
  const activeFile = activeFileIndex >= 0 ? openFiles[activeFileIndex] : null
  const activeFileKey = activeFile ? `${activeFile.projectId}\0${activeFile.path}` : null
  const [diffState, setDiffState] = useState<{
    activeFileKey: string | null
    requestId: number
    content: string | null
    visible: boolean
  }>({ activeFileKey, requestId: 0, content: null, visible: false })
  const [pendingDiscard, setPendingDiscard] = useState<
    { action: 'cancel' | 'close'; index: number } | null
  >(null)
  const editorViewRef = useRef<EditorView | null>(null)
  const showDiff = diffState.activeFileKey === activeFileKey && diffState.visible
  const diffContent = showDiff ? diffState.content : null

  if (diffState.activeFileKey !== activeFileKey) {
    setDiffState({
      activeFileKey,
      requestId: diffState.requestId + 1,
      content: null,
      visible: false,
    })
  }

  const hideDiff = useCallback(() => {
    setDiffState(current => ({
      ...current,
      requestId: current.requestId + 1,
      content: null,
      visible: false,
    }))
  }, [])

  const showCancelConfirm = pendingDiscard !== null

  const handleCancel = useCallback(() => {
    if (activeFile?.dirty) {
      setPendingDiscard({ action: 'cancel', index: activeFileIndex })
    } else {
      onCancelEditing(activeFileIndex)
      hideDiff()
    }
  }, [activeFile, activeFileIndex, hideDiff, onCancelEditing])

  const confirmCancel = useCallback(() => {
    if (!pendingDiscard) return
    setPendingDiscard(null)
    if (pendingDiscard.action === 'close') {
      onCloseFile(pendingDiscard.index)
    } else {
      onCancelEditing(pendingDiscard.index)
      hideDiff()
    }
  }, [hideDiff, onCancelEditing, onCloseFile, pendingDiscard])

  const handleUndo = useCallback(() => {
    if (editorViewRef.current) undo(editorViewRef.current)
  }, [])

  const handleRedo = useCallback(() => {
    if (editorViewRef.current) redo(editorViewRef.current)
  }, [])

  const handleShowDiff = useCallback(async () => {
    if (!activeFile) return
    if (showDiff) {
      hideDiff()
      return
    }
    const requestId = diffState.requestId + 1
    setDiffState({ activeFileKey, requestId, content: null, visible: false })
    const diff = await onFetchDiff(activeFile.projectId, activeFile.path)
    setDiffState(current => {
      if (current.activeFileKey !== activeFileKey || current.requestId !== requestId) return current
      return { ...current, content: diff, visible: true }
    })
  }, [activeFile, activeFileKey, diffState.requestId, hideDiff, showDiff, onFetchDiff])

  const activeGitStatus = activeFile ? gitStatuses.get(activeFile.projectId) : undefined
  const activeFileGitStatus = activeFile && activeGitStatus ? activeGitStatus.files[activeFile.path] : undefined

  return (
    <div className="flex flex-1 overflow-hidden">
      <Card className="flex w-[var(--sidebar-width)] min-w-[var(--sidebar-width)] flex-col rounded-none border-0 border-r border-[var(--border)] bg-[var(--bg-secondary)] max-md:w-[200px] max-md:min-w-[160px]">
        <div className="flex items-center justify-between border-b border-[var(--border)] px-3 py-2.5">
          <span className="text-[length:var(--text-sm)] font-semibold uppercase tracking-[0.05em] text-[var(--text-muted)]">
            Explorer
          </span>
        </div>
        <div className="flex-1 overflow-x-hidden overflow-y-auto py-1">
          {projects.length === 0 ? (
            <div className="px-4 py-6 text-center text-[length:var(--text-md)] text-[var(--text-muted)]">
              No projects registered
            </div>
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
      </Card>

      <div className="relative flex flex-1 flex-col overflow-hidden bg-[var(--bg-primary)]">
        {openFiles.length > 0 && (
          <TabBar
            tabs={openFiles.map((file, index) => ({
              id: String(index),
              label: file.dirty ? `${file.name} ●` : file.name,
              closeLabel: file.name,
              icon: <FileIcon extension={file.name.split('.').pop() || ''} size={14} />,
            }))}
            activeTab={String(activeFileIndex)}
            onTabChange={(tabId) => onSetActiveFile(Number(tabId))}
            onTabClose={(tabId) => {
              const index = Number(tabId)
              const file = openFiles[index]
              if (!file) return

              if (file.dirty) {
                setPendingDiscard({ action: 'close', index })
              } else {
                onCloseFile(index)
              }
            }}
            ariaLabel="Open files"
            className="mb-0 bg-[var(--bg-secondary)]"
          />
        )}

        {activeFile && !activeFile.image && !activeFile.binary && !activeFile.loading && !activeFile.error && activeFile.content !== null && (
          <div className="flex min-h-8 items-center justify-between border-b border-[var(--border)] bg-[var(--bg-secondary)] px-3 py-1 text-[length:var(--text-sm)] [container-name:files-viewer] [container-type:inline-size]">
            <span className="overflow-hidden text-ellipsis whitespace-nowrap text-[var(--text-muted)]">
              {activeFile.path}
            </span>
            <div className="flex shrink-0 items-center gap-1.5">
              {activeFile.truncated && (
                <span className="text-[var(--color-warning-foreground)]" role="status">
                  File is too large to edit safely.
                </span>
              )}
              {activeFileGitStatus && (
                <Button
                  variant="accent"
                  size="sm"
                  dense
                  className={cn(coarseHitAreaCls, 'file-viewer-btn')}
                  onClick={handleShowDiff}
                  aria-pressed={showDiff}
                  aria-label="Diff"
                  title="Diff"
                >
                  <DiffIcon />
                  <span className="file-viewer-btn__label">Diff</span>
                </Button>
              )}
              {activeFile.editing ? (
                <>
                  <Button variant="accent" size="icon" dense className={cn(coarseHitAreaCls, 'file-viewer-btn')} onClick={handleUndo} title="Undo (Cmd+Z)" aria-label="Undo">
                    <UndoIcon />
                  </Button>
                  <Button variant="accent" size="icon" dense className={cn(coarseHitAreaCls, 'file-viewer-btn')} onClick={handleRedo} title="Redo (Cmd+Shift+Z)" aria-label="Redo">
                    <RedoIcon />
                  </Button>
                  <Button
                    variant="accent"
                    size="sm"
                    dense
                    className={cn(coarseHitAreaCls, 'file-viewer-btn')}
                    onClick={handleCancel}
                    aria-label="Cancel"
                    title="Cancel"
                  >
                    <XIcon />
                    <span className="file-viewer-btn__label">Cancel</span>
                  </Button>
                  <Button
                    variant="accent"
                    size="sm"
                    dense
                    className={cn(coarseHitAreaCls, 'file-viewer-btn')}
                    onClick={() => onSaveFile(activeFileIndex)}
                    disabled={activeFile.truncated || activeFile.saving || !activeFile.dirty}
                    aria-label={activeFile.saving ? 'Saving' : 'Save'}
                    title={activeFile.saving ? 'Saving...' : 'Save'}
                  >
                    <CheckIcon />
                    <span className="file-viewer-btn__label">{activeFile.saving ? 'Saving...' : 'Save'}</span>
                  </Button>
                </>
              ) : (
                <Button
                  variant="accent"
                  size="sm"
                  dense
                  className={cn(coarseHitAreaCls, 'file-viewer-btn')}
                  onClick={() => {
                    onToggleEditing(activeFileIndex)
                    hideDiff()
                  }}
                  disabled={activeFile.truncated}
                  aria-label="Edit"
                  title={activeFile.truncated ? 'File is too large to edit safely' : 'Edit'}
                >
                  <EditIcon />
                  <span className="file-viewer-btn__label">Edit</span>
                </Button>
              )}
            </div>
          </div>
        )}

        <div className="flex flex-1 flex-col overflow-hidden">
          {showDiff && diffContent !== null ? (
            <div className="min-h-0 flex-1 overflow-auto [&>div]:min-h-full">
              <CodeBlock
                language="diff"
                lineNumberMinWidth="3em"
                customStyle={{
                  margin: 0,
                  borderRadius: 0,
                  minHeight: '100%',
                }}
              >
                {diffContent || '(no changes)'}
              </CodeBlock>
            </div>
          ) : activeFile ? (
            <FileContent
              file={activeFile}
              getImageUrl={getImageUrl}
              onContentChange={(content) => onUpdateEditContent(activeFileIndex, content)}
              onDismissSaveError={() => onClearSaveError(activeFileIndex)}
              onSave={() => onSaveFile(activeFileIndex)}
              editorViewRef={editorViewRef}
            />
          ) : (
            <div className="flex flex-1 flex-col items-center justify-center gap-3 text-[length:var(--text-base)] text-[var(--text-muted)]">
              <FilesPlaceholderIcon />
              <p>Select a file to view</p>
            </div>
          )}
        </div>

        <ConfirmDialog
          open={showCancelConfirm}
          onConfirm={confirmCancel}
          onCancel={() => setPendingDiscard(null)}
          title="Discard unsaved changes?"
          description="Your changes to this file will be lost."
          confirmLabel="Discard"
          cancelLabel="Keep Editing"
          destructive
        />
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
    <div className="mb-0.5">
      <Button
        type="button"
        variant="ghost"
        size="sm"
        dense
        className={cn(
          coarseHitAreaCls,
          'h-auto w-full justify-start gap-1.5 rounded-none border-0 bg-transparent px-2 py-1.5 text-left text-[length:var(--text-md)] font-medium text-[var(--text-primary)] hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)]',
        )}
        onClick={onToggle}
        aria-expanded={isExpanded}
        aria-label={`${isExpanded ? 'Collapse' : 'Expand'} ${project.name}`}
      >
        <span className="w-2.5 shrink-0 text-center text-[length:var(--text-2xs)] text-[var(--text-muted)]">
          {isExpanded ? '▾' : '▸'}
        </span>
        <ProjectIcon />
        <span className="overflow-hidden text-ellipsis whitespace-nowrap">{project.name}</span>
        {gitStatus?.branch && (
          <span className="ml-auto rounded-sm bg-[var(--bg-tertiary)] px-1.5 py-px text-[length:var(--text-xs)] font-normal text-[var(--text-muted)]">
            {gitStatus.branch}
          </span>
        )}
      </Button>
      {isExpanded && (
        <div>
          {isLoading ? (
            <div className="px-2 py-1 text-[length:var(--text-sm)] italic text-[var(--text-muted)]">
              Loading...
            </div>
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
  return GIT_STATUS_CLASS_BY_CODE[status] ?? GIT_STATUS_CLASS_BY_CODE.M
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
          className="flex cursor-pointer select-none items-center gap-1.5 px-2 py-0.5 text-[length:var(--text-md)] text-[var(--text-secondary)] transition-colors duration-100 hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)] pointer-coarse:min-h-11"
          style={{ paddingLeft: `${depth * 16 + 4}px` }}
          role="button"
          tabIndex={0}
          aria-expanded={isExpanded}
          onClick={() => onExpandDir(projectId, entry.path)}
          onKeyDown={(event) =>
            activateOnKeyboard(event, () => onExpandDir(projectId, entry.path))
          }
        >
          <span className="w-2.5 shrink-0 text-center text-[length:var(--text-2xs)] text-[var(--text-muted)]">
            {isExpanded ? '▾' : '▸'}
          </span>
          <FolderIcon open={isExpanded} />
          <span className="overflow-hidden text-ellipsis whitespace-nowrap">{entry.name}</span>
        </div>
        {isExpanded && (
          <div>
            {isLoading ? (
              <div className="px-2 py-1 text-[length:var(--text-sm)] italic text-[var(--text-muted)]" style={{ paddingLeft: `${(depth + 1) * 16 + 4}px` }}>
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
      className="flex cursor-pointer select-none items-center gap-1.5 px-2 py-0.5 text-[length:var(--text-md)] text-[var(--text-secondary)] transition-colors duration-100 hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)] pointer-coarse:min-h-11"
      style={{ paddingLeft: `${depth * 16 + 20}px` }}
      role="button"
      tabIndex={0}
      onClick={() => onOpenFile(projectId, entry.path, entry.name)}
      onKeyDown={(event) =>
        activateOnKeyboard(event, () => onOpenFile(projectId, entry.path, entry.name))
      }
    >
      <FileIcon extension={entry.extension?.replace('.', '') || ''} size={14} />
      <span className={cn('overflow-hidden text-ellipsis whitespace-nowrap', gitClass)}>
        {entry.name}
      </span>
      {gitStatus && (
        <span className={cn('ml-auto shrink-0 font-mono text-[length:var(--text-2xs)] font-semibold opacity-85', gitClass)}>
          {gitStatus === '??' ? '?' : gitStatus.charAt(0)}
        </span>
      )}
    </div>
  )
}

function FileContent({
  file,
  getImageUrl,
  onContentChange,
  onDismissSaveError,
  onSave,
  editorViewRef,
}: {
  file: OpenFile
  getImageUrl: (projectId: string, path: string) => string
  onContentChange: (content: string) => void
  onDismissSaveError: () => void
  onSave: () => void
  editorViewRef?: React.MutableRefObject<EditorView | null>
}) {
  if (file.loading) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-2 text-[length:var(--text-base)] text-[var(--text-muted)]">
        Loading...
      </div>
    )
  }

  if (file.error) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-2 text-[length:var(--text-base)] text-[var(--color-error)]">
        Error: {file.error}
      </div>
    )
  }

  if (file.image) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-4 overflow-auto p-8">
        <img
          src={getImageUrl(file.projectId, file.path)}
          alt={file.name}
          loading="lazy"
          decoding="async"
          className="max-h-[calc(100%-3rem)] max-w-full rounded-lg object-contain [background:repeating-conic-gradient(var(--bg-primary)_0%_25%,var(--bg-secondary)_0%_50%)_50%/20px_20px]"
        />
        <div className="text-[length:var(--text-sm)] text-[var(--text-muted)]">
          {file.name} &middot; {formatSize(file.size)} &middot; {file.mime_type}
        </div>
      </div>
    )
  }

  if (file.binary) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-2 text-[length:var(--text-base)] text-[var(--text-muted)]">
        <BinaryIcon />
        <p>Binary file &middot; {formatSize(file.size)}</p>
        <p className="text-[length:var(--text-sm)] text-[var(--text-muted)]">{file.mime_type}</p>
      </div>
    )
  }

  if (file.content === null) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-2 text-[length:var(--text-base)] text-[var(--text-muted)]">
        No content
      </div>
    )
  }

  if (file.editing) {
    return (
      <div className="flex min-h-0 flex-1 flex-col">
        {file.saveError && (
          <div className="flex items-center justify-between gap-3 border-b border-[var(--color-error)] bg-[var(--bg-secondary)] px-3 py-2 text-[length:var(--text-sm)] text-[var(--color-error)]" role="alert">
            <span>Save failed: {file.saveError}</span>
            <Button
              variant="accent"
              size="icon"
              dense
              className={cn(coarseHitAreaCls, 'file-viewer-btn')}
              onClick={onDismissSaveError}
              aria-label="Dismiss save error"
              title="Dismiss"
            >
              <XIcon />
            </Button>
          </div>
        )}
        <div className="min-h-0 flex-1 overflow-auto [&>div]:min-h-full">
          <CodeMirrorEditor
            content={file.editContent ?? file.content}
            language={file.language}
            readOnly={false}
            onChange={onContentChange}
            onSave={onSave}
            editorViewRef={editorViewRef}
          />
        </div>
      </div>
    )
  }

  if (file.language === 'markdown') {
    return (
      <div className="min-h-0 flex-1 overflow-auto [&>div]:min-h-full">
        <div
          className={cn(
            'message-content overflow-wrap-break-word px-6 py-4 text-[length:var(--text-base)] leading-[1.7] text-[var(--text-primary)]',
            markdownBodyClassName,
          )}
        >
          <MarkdownBody
            content={file.content}
            id={`files-page-md-${file.projectId}:${file.path}`}
          />
        </div>
      </div>
    )
  }

  return (
    <div className="min-h-0 flex-1 overflow-auto [&>div]:min-h-full">
      <CodeBlock
        language={file.language}
        lineNumberMinWidth="3em"
        customStyle={{
          margin: 0,
          borderRadius: 0,
          minHeight: '100%',
        }}
      >
        {file.content}
      </CodeBlock>
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

function EditIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M12 20h9" />
      <path d="M16.5 3.5a2.121 2.121 0 1 1 3 3L7 19l-4 1 1-4 12.5-12.5z" />
    </svg>
  )
}

function CheckIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <polyline points="20 6 9 17 4 12" />
    </svg>
  )
}

function XIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <line x1="18" y1="6" x2="6" y2="18" />
      <line x1="6" y1="6" x2="18" y2="18" />
    </svg>
  )
}

function DiffIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <line x1="12" y1="3" x2="12" y2="21" />
      <polyline points="8 7 4 11 8 15" />
      <polyline points="16 9 20 13 16 17" />
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
