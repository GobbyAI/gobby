import type { MouseEvent, Ref } from 'react'

import {
  FOLDER_ICON_COLOR_VAR,
  getGitStatusColorVar,
  getLanguageColorVar,
} from '../../lib/languageColors'
import { cn } from '../../lib/utils'
import { Button } from '../ui/Button'
import { coarseHitAreaCls } from '../ui/controlStyles'
import { Input } from '../ui/Input'
import type { ContextMenuState, FileEntry, RenamingState } from './FilesTab.types'

interface FilesTabTreeProps {
  entries: FileEntry[]
  expandedPaths: Set<string>
  selectedFile: string | null
  childrenMap: Map<string, FileEntry[]>
  renaming: RenamingState | null
  renameInputRef: Ref<HTMLInputElement>
  contextMenu: ContextMenuState | null
  gitStatus: Record<string, string>
  onToggleDirectory: (path: string) => void
  onOpenFile: (path: string) => void
  onContextMenu: (event: MouseEvent, entry: FileEntry) => void
  onActionsMenu: (event: MouseEvent<HTMLButtonElement>, entry: FileEntry) => void
  onSubmitRename: () => void
  onCancelRename: () => void
}

interface FileTreeRenameInputProps {
  name: string
  inputRef: Ref<HTMLInputElement>
  onSubmit: () => void
  onCancel: () => void
}

// The base-layer focus ring sits 2px outside the row, so the tree pane's
// overflow clips it to a stray full-width line between rows (#20046); an
// inset ring stays fully visible inside the scroller.
const treeRowFocusCls =
  'focus-visible:outline-2 focus-visible:outline-accent focus-visible:outline-offset-[-2px]'

function FileTreeRenameInput({
  name,
  inputRef,
  onSubmit,
  onCancel,
}: FileTreeRenameInputProps) {
  return (
    <span className="min-w-0 flex-1" onClick={(event) => event.stopPropagation()}>
      <Input
        ref={inputRef}
        aria-label={`Rename ${name}`}
        wrapperClassName="min-w-0 flex-1"
        className="h-auto flex-1 rounded border-[var(--accent)] bg-[var(--bg-tertiary)] px-1.5 py-0.5 text-[length:inherit] text-[var(--text-primary)] outline-none"
        defaultValue={name}
        onKeyDown={(event) => {
          if (event.key === 'Enter') onSubmit()
          if (event.key === 'Escape') onCancel()
        }}
        onBlur={onSubmit}
        onClick={(event) => event.stopPropagation()}
      />
    </span>
  )
}

function FolderIcon({ open }: { open: boolean }) {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke={FOLDER_ICON_COLOR_VAR}
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      style={{ flexShrink: 0 }}
    >
      <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
      {open && <line x1="9" y1="14" x2="15" y2="14" />}
    </svg>
  )
}

function FileIcon({ extension }: { extension: string }) {
  const color = getLanguageColorVar(extension)
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke={color}
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      style={{ flexShrink: 0 }}
    >
      <path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z" />
      <polyline points="13 2 13 9 20 9" />
    </svg>
  )
}

function GitStatusBadge({ status }: { status: string }) {
  const label = status === '??' ? '?' : status.charAt(0)
  const color = getGitStatusColorVar(label)
  return (
    <span
      className="ml-1.5 shrink-0 pr-1 text-[length:var(--text-2xs)] font-bold [font-family:inherit]"
      style={{ color }}
      title={
        status === 'M'
          ? 'Modified'
          : status === 'A'
            ? 'Added'
            : status === 'D'
              ? 'Deleted'
              : status === 'R'
                ? 'Renamed'
                : 'Untracked'
      }
    >
      {label}
    </span>
  )
}

export function FilesTabTree({
  entries,
  expandedPaths,
  selectedFile,
  childrenMap,
  renaming,
  renameInputRef,
  contextMenu,
  gitStatus,
  onToggleDirectory,
  onOpenFile,
  onContextMenu,
  onActionsMenu,
  onSubmitRename,
  onCancelRename,
}: FilesTabTreeProps) {
  const renderEntry = (entry: FileEntry, depth: number) => {
    const isDirectory = entry.is_dir
    const isExpanded = expandedPaths.has(entry.path)
    const isSelected = entry.path === selectedFile
    const children = childrenMap.get(entry.path)
    const isRenaming = renaming?.path === entry.path
    const extension = entry.name.split('.').pop() ?? ''

    if (isDirectory) {
      const prefix = entry.path ? `${entry.path}/` : ''
      const hasGitStatus = Object.keys(gitStatus).some((path) => path.startsWith(prefix))

      return (
        <div key={entry.path}>
          <div
            className={cn(
              'group/files-tree flex cursor-pointer select-none items-center gap-1.5 px-2 py-[0.1875rem] text-[length:var(--text-base)] font-[var(--font-weight-medium)] text-[var(--text-secondary)] transition-colors duration-100 hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)]',
              treeRowFocusCls,
              isSelected && 'bg-[color-mix(in_srgb,var(--accent)_8%,transparent)]',
            )}
            style={{ paddingLeft: `calc(0.5rem + ${depth} * 1rem)` }}
            role="treeitem"
            tabIndex={0}
            aria-level={depth + 1}
            aria-expanded={isExpanded}
            onClick={() => onToggleDirectory(entry.path)}
            onKeyDown={(event) => {
              if (
                event.target !== event.currentTarget ||
                (event.key !== 'Enter' && event.key !== ' ')
              )
                return
              event.preventDefault()
              onToggleDirectory(entry.path)
            }}
            onContextMenu={(event) => onContextMenu(event, entry)}
          >
            <FolderIcon open={isExpanded} />
            {isRenaming ? (
              <FileTreeRenameInput
                name={renaming.name}
                inputRef={renameInputRef}
                onSubmit={onSubmitRename}
                onCancel={onCancelRename}
              />
            ) : (
              <span
                className={cn(
                  'min-w-0 flex-1 truncate',
                  hasGitStatus && 'text-[var(--color-warning-foreground)]',
                )}
              >
                {entry.name}
              </span>
            )}
            {!isRenaming && (
              <Button
                type="button"
                variant="ghost"
                size="icon"
                dense
                className={cn(
                  coarseHitAreaCls,
                  'ml-auto size-6 shrink-0 rounded border-0 bg-transparent p-0 text-[var(--text-muted)] opacity-0 group-hover/files-tree:opacity-100 group-focus-within/files-tree:opacity-100 aria-expanded:opacity-100 hover:bg-[var(--bg-secondary)] hover:text-[var(--text-primary)] focus-visible:bg-[var(--bg-secondary)] focus-visible:text-[var(--text-primary)]',
                )}
                aria-label={`Actions for ${entry.name}`}
                aria-haspopup="menu"
                aria-expanded={contextMenu?.entry.path === entry.path}
                onClick={(event) => onActionsMenu(event, entry)}
              >
                <span aria-hidden="true">⋯</span>
              </Button>
            )}
          </div>
          {isExpanded && children?.map((child) => renderEntry(child, depth + 1))}
          {isExpanded && !children && (
            <div
              className="px-2 py-1 text-[length:var(--text-sm)] italic text-[var(--text-muted)]"
              style={{ paddingLeft: `calc(0.5rem + ${depth + 1} * 1rem)` }}
            >
              Loading...
            </div>
          )}
        </div>
      )
    }

    const fileStatus = gitStatus[entry.path] ?? null

    return (
      <div key={entry.path}>
        <div
          className={cn(
            'group/files-tree flex cursor-pointer select-none items-center gap-1.5 px-2 py-[0.1875rem] text-[length:var(--text-base)] font-[var(--font-weight-medium)] text-[var(--text-secondary)] transition-colors duration-100 hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)]',
            treeRowFocusCls,
            isSelected && 'bg-[color-mix(in_srgb,var(--accent)_8%,transparent)]',
          )}
          style={{ paddingLeft: `calc(0.5rem + ${depth} * 1rem)` }}
          role="treeitem"
          tabIndex={0}
          aria-level={depth + 1}
          aria-selected={isSelected}
          onClick={() => onOpenFile(entry.path)}
          onKeyDown={(event) => {
            if (
              event.target !== event.currentTarget ||
              (event.key !== 'Enter' && event.key !== ' ')
            )
              return
            event.preventDefault()
            onOpenFile(entry.path)
          }}
          onContextMenu={(event) => onContextMenu(event, entry)}
          draggable
          onDragStart={(event) => {
            event.dataTransfer.setData('application/x-gobby-file', entry.path)
            event.dataTransfer.effectAllowed = 'copy'
          }}
        >
          <FileIcon extension={extension} />
          {isRenaming ? (
            <FileTreeRenameInput
              name={renaming.name}
              inputRef={renameInputRef}
              onSubmit={onSubmitRename}
              onCancel={onCancelRename}
            />
          ) : (
            <span
              className={cn(
                'min-w-0 flex-1 truncate',
                fileStatus === 'M' && 'text-[var(--color-warning-foreground)]',
                fileStatus === 'A' && 'text-[var(--color-success-foreground)]',
                fileStatus === 'D' && 'text-[var(--color-error)] line-through',
                fileStatus === '??' && 'text-[var(--text-muted)]',
              )}
            >
              {entry.name}
            </span>
          )}
          {fileStatus && <GitStatusBadge status={fileStatus} />}
          {!isRenaming && (
            <Button
              type="button"
              variant="ghost"
              size="icon"
              dense
              className={cn(
                coarseHitAreaCls,
                'ml-auto size-6 shrink-0 rounded border-0 bg-transparent p-0 text-[var(--text-muted)] opacity-0 group-hover/files-tree:opacity-100 group-focus-within/files-tree:opacity-100 aria-expanded:opacity-100 hover:bg-[var(--bg-secondary)] hover:text-[var(--text-primary)] focus-visible:bg-[var(--bg-secondary)] focus-visible:text-[var(--text-primary)]',
              )}
              aria-label={`Actions for ${entry.name}`}
              aria-haspopup="menu"
              aria-expanded={contextMenu?.entry.path === entry.path}
              onClick={(event) => onActionsMenu(event, entry)}
            >
              <span aria-hidden="true">⋯</span>
            </Button>
          )}
        </div>
      </div>
    )
  }

  return (
    <div role="tree" aria-label="Project files">
      {entries.map((entry) => renderEntry(entry, 0))}
    </div>
  )
}
