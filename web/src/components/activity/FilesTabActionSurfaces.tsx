import type { Ref } from 'react'

import { cn } from '../../lib/utils'
import { Button } from '../ui/Button'
import { Card } from '../ui/Card'
import { coarseHitAreaCls } from '../ui/controlStyles'
import { Input } from '../ui/Input'
import type { ContextMenuState, FileEntry } from './FilesTab.types'

interface FilesTabContextMenuProps {
  contextMenu: ContextMenuState | null
  menuRef: Ref<HTMLDivElement>
  onAddToChat?: (filePath: string) => void
  onClose: () => void
  onDuplicate: (entry: FileEntry) => void
  onRename: (entry: FileEntry) => void
  onMove: (entry: FileEntry) => void
  onDelete: (entry: FileEntry) => void
}

const contextMenuItemClassName = cn(
  coarseHitAreaCls,
  'block w-full rounded bg-transparent px-2.5 py-1.5 text-left text-[length:var(--text-md)] text-[var(--text-primary)] transition-colors duration-100 hover:bg-[var(--bg-tertiary)]',
)

export function FilesTabContextMenu({
  contextMenu,
  menuRef,
  onAddToChat,
  onClose,
  onDuplicate,
  onRename,
  onMove,
  onDelete,
}: FilesTabContextMenuProps) {
  if (!contextMenu) return null

  const { entry } = contextMenu
  return (
    <>
      <div className="fixed inset-0 z-[90]" onClick={onClose} />
      <div
        ref={menuRef}
        className="z-[91] min-w-[140px] rounded-md border border-[var(--border)] bg-[var(--bg-secondary)] p-1 shadow-[var(--shadow-md)]"
        role="menu"
        aria-label={`Actions for ${entry.name}`}
        style={{ position: 'fixed', left: contextMenu.x, top: contextMenu.y }}
        onKeyDown={(event) => {
          if (event.key === 'Escape') onClose()
        }}
      >
        {onAddToChat && !entry.is_dir && (
          <Button
            type="button"
            role="menuitem"
            variant="ghost"
            size="sm"
            dense
            className={contextMenuItemClassName}
            onClick={() => {
              onAddToChat(entry.path)
              onClose()
            }}
          >
            Add to chat
          </Button>
        )}
        {!entry.is_dir && (
          <Button
            type="button"
            role="menuitem"
            variant="ghost"
            size="sm"
            dense
            className={contextMenuItemClassName}
            onClick={() => onDuplicate(entry)}
          >
            Duplicate
          </Button>
        )}
        <Button
          type="button"
          role="menuitem"
          variant="ghost"
          size="sm"
          dense
          className={contextMenuItemClassName}
          onClick={() => onRename(entry)}
        >
          Rename
        </Button>
        <Button
          type="button"
          role="menuitem"
          variant="ghost"
          size="sm"
          dense
          className={contextMenuItemClassName}
          onClick={() => onMove(entry)}
        >
          Move
        </Button>
        <Button
          type="button"
          role="menuitem"
          variant="ghost"
          size="sm"
          dense
          className={cn(
            coarseHitAreaCls,
            'block w-full rounded bg-transparent px-2.5 py-1.5 text-left text-[length:var(--text-md)] text-[var(--color-error)] transition-colors duration-100 hover:bg-[color-mix(in_srgb,var(--color-error)_10%,transparent)]',
          )}
          onClick={() => onDelete(entry)}
        >
          Delete
        </Button>
      </div>
    </>
  )
}

interface FilesTabMoveDialogProps {
  moving: FileEntry | null
  formRef: Ref<HTMLFormElement>
  movePath: string
  error: string | null
  onMovePathChange: (path: string) => void
  onClose: () => void
  onSubmit: () => void
}

export function FilesTabMoveDialog({
  moving,
  formRef,
  movePath,
  error,
  onMovePathChange,
  onClose,
  onSubmit,
}: FilesTabMoveDialogProps) {
  if (!moving) return null

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-[var(--surface-scrim)] p-4"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose()
      }}
    >
      <Card asChild padding="md" className="w-full max-w-md shadow-xl">
        <form
          ref={formRef}
          role="dialog"
          aria-modal="true"
          aria-labelledby="files-move-title"
          onSubmit={(event) => {
            event.preventDefault()
            onSubmit()
          }}
        >
          <h2 id="files-move-title" className="mb-3 text-base font-semibold text-foreground">
            Move {moving.name}
          </h2>
          <div className="grid gap-1.5 text-sm text-foreground">
            <span id="files-move-path-label">Move to path</span>
            <Input
              autoFocus
              aria-labelledby="files-move-path-label"
              value={movePath}
              onChange={(event) => onMovePathChange(event.target.value)}
              className="min-h-10 rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
            />
          </div>
          {error && (
            <p className="mt-2 text-sm text-destructive-foreground" role="alert">
              {error}
            </p>
          )}
          <div className="mt-4 flex justify-end gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              dense
              className={coarseHitAreaCls}
              onClick={onClose}
            >
              Cancel
            </Button>
            <Button
              type="submit"
              variant="accent"
              size="sm"
              dense
              className={coarseHitAreaCls}
              disabled={!movePath.trim() || movePath.trim() === moving.path}
            >
              Move file
            </Button>
          </div>
        </form>
      </Card>
    </div>
  )
}
