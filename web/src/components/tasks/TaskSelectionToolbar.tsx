import { useState } from 'react'
import { BatchLaunchAgentDialog } from './LaunchAgentDialog'
import { cn } from '../../lib/utils'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface SelectedTask {
  id: string
  title: string
  category?: string | null
}

interface TaskSelectionToolbarProps {
  selectedTasks: SelectedTask[]
  projectId?: string | null
  onClearSelection: () => void
  onBatchSpawned?: (succeeded: number, failed: number) => void
}

// ---------------------------------------------------------------------------
// Icons
// ---------------------------------------------------------------------------

function RocketIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91-.09z" />
      <path d="m12 15-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 0 1-4 2z" />
      <path d="M9 12H4s.55-3.03 2-4c1.62-1.08 5 0 5 0" />
      <path d="M12 15v5s3.03-.55 4-2c1.08-1.62 0-5 0-5" />
    </svg>
  )
}

// ---------------------------------------------------------------------------
// Toolbar
// ---------------------------------------------------------------------------

const TOOLBAR_CLS =
  'fixed bottom-6 left-1/2 z-[100] flex -translate-x-1/2 items-center gap-3 rounded-xl border border-[var(--border)] bg-[var(--bg-secondary)] px-4 py-2.5 shadow-[var(--shadow-lg)] max-md:bottom-0 max-md:left-0 max-md:right-0 max-md:translate-x-0 max-md:justify-center max-md:rounded-none max-md:border-x-0 max-md:border-b-0 max-md:py-3'

const COUNT_CLS =
  'text-[length:calc(var(--font-size-base)*0.85)] font-medium whitespace-nowrap text-[var(--text-primary)]'

const BTN_BASE_CLS =
  'flex cursor-pointer items-center gap-[0.3rem] min-h-11 rounded-md border border-[var(--border)] px-3 py-[0.4rem] font-[inherit] text-[length:calc(var(--font-size-base)*0.8)] font-medium transition-[background-color,color,border-color] duration-150'

const BTN_PRIMARY_CLS =
  'border-[var(--accent)] bg-[var(--accent)] text-[var(--accent-foreground)] hover:bg-[var(--accent-hover)]'

const BTN_DEFAULT_CLS =
  'bg-[var(--bg-tertiary)] text-[var(--text-primary)] hover:bg-[var(--bg-primary)]'

export function TaskSelectionToolbar({
  selectedTasks,
  projectId,
  onClearSelection,
  onBatchSpawned,
}: TaskSelectionToolbarProps) {
  const [showBatchDialog, setShowBatchDialog] = useState(false)

  if (selectedTasks.length === 0) return null

  return (
    <>
      <div className={TOOLBAR_CLS}>
        <span className={COUNT_CLS}>
          {selectedTasks.length} task{selectedTasks.length !== 1 ? 's' : ''} selected
        </span>
        <button
          className={cn(BTN_BASE_CLS, BTN_PRIMARY_CLS)}
          onClick={() => setShowBatchDialog(true)}
        >
          <RocketIcon /> Launch Agents
        </button>
        <button
          className={cn(BTN_BASE_CLS, BTN_DEFAULT_CLS)}
          onClick={onClearSelection}
        >
          Clear
        </button>
      </div>

      <BatchLaunchAgentDialog
        isOpen={showBatchDialog}
        tasks={selectedTasks}
        projectId={projectId}
        onClose={() => setShowBatchDialog(false)}
        onSpawned={(succeeded, failed) => {
          setShowBatchDialog(false)
          onClearSelection()
          onBatchSpawned?.(succeeded, failed)
        }}
      />
    </>
  )
}
