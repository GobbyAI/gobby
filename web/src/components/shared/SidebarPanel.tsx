import { useEffect, useRef } from 'react'
import type { CSSProperties } from 'react'
import { cn } from '../../lib/utils'
import './SidebarPanel.css'

interface SidebarPanelProps {
  isOpen: boolean
  onClose: () => void
  title: string | React.ReactNode
  width?: number
  headerContent?: React.ReactNode
  footer?: React.ReactNode
  children: React.ReactNode
  className?: string
}

export function SidebarPanel({
  isOpen,
  onClose,
  title,
  width = 480,
  headerContent,
  footer,
  children,
  className,
}: SidebarPanelProps) {
  const panelRef = useRef<HTMLDivElement>(null)
  const onCloseRef = useRef(onClose)
  useEffect(() => {
    onCloseRef.current = onClose
  }, [onClose])

  useEffect(() => {
    if (!isOpen) return
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onCloseRef.current()
    }
    document.addEventListener('keydown', handleKeyDown)
    panelRef.current?.focus()
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [isOpen])

  return (
    <>
      {isOpen && (
        <div
          className="fixed inset-0 z-[90] bg-[var(--surface-scrim)]"
          onClick={onClose}
        />
      )}
      <div
        ref={panelRef}
        tabIndex={-1}
        className={cn(
          'fixed right-0 top-0 z-[100] flex h-full max-w-[90vw] flex-col border-l border-[var(--border)] bg-[var(--bg-secondary)] outline-none transition-transform duration-[250ms] ease-out',
          'w-[var(--sidebar-panel-width)] max-md:w-screen max-md:max-w-full',
          isOpen ? 'translate-x-0' : 'translate-x-full',
          className,
        )}
        style={{ '--sidebar-panel-width': `${width}px` } as CSSProperties}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex-shrink-0 border-b border-[var(--border)] px-5 py-4">
          <div className="flex items-center justify-between">
            <span className="text-[length:var(--text-sm)] font-semibold uppercase tracking-[0.05em] text-[var(--text-muted)]">
              {title}
            </span>
            <button
              type="button"
              onClick={onClose}
              aria-label="Close panel"
              className="flex h-8 w-8 cursor-pointer items-center justify-center rounded border-0 bg-transparent text-[var(--text-muted)] transition-colors duration-150 hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)] pointer-coarse:h-11 pointer-coarse:w-11"
            >
              <svg
                width="16"
                height="16"
                viewBox="0 0 16 16"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.5"
                strokeLinecap="round"
              >
                <path d="M4 4l8 8M12 4l-8 8" />
              </svg>
            </button>
          </div>
          {headerContent}
        </div>
        <div className="flex-1 overflow-y-auto">{children}</div>
        {footer && (
          <div className="flex flex-shrink-0 justify-end gap-2 border-t border-[var(--border)] px-5 py-3">
            {footer}
          </div>
        )}
      </div>
    </>
  )
}
