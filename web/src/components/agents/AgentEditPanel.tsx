import { useId, useLayoutEffect, useRef, type ReactNode } from 'react'
import { useDialogFocus } from '../../hooks/useDialogFocus'
import { cn } from '../../lib/utils'
import { Button } from '../ui/Button'
import { coarseHitAreaCls } from '../ui/controlStyles'

interface AgentEditPanelProps {
  isOpen: boolean
  onClose: () => void
  title: string
  headerContent?: ReactNode
  footer?: ReactNode
  children: ReactNode
}

export function AgentEditPanel({
  isOpen,
  onClose,
  title,
  headerContent,
  footer,
  children,
}: AgentEditPanelProps) {
  const panelRef = useRef<HTMLDivElement>(null)
  const titleId = useId()
  useDialogFocus({ ref: panelRef, isOpen, onClose })
  useLayoutEffect(() => {
    panelRef.current?.toggleAttribute('inert', !isOpen)
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
        role={isOpen ? 'dialog' : undefined}
        aria-modal={isOpen ? true : undefined}
        aria-labelledby={isOpen ? titleId : undefined}
        aria-hidden={isOpen ? undefined : true}
        className={cn(
          'fixed right-0 top-0 z-[100] flex h-full w-[480px] max-w-[90vw] flex-col border-l border-[var(--border)] bg-[var(--bg-secondary)] outline-none transition-transform duration-[250ms] ease-out',
          'max-md:w-screen max-md:max-w-full',
          isOpen ? 'translate-x-0' : 'translate-x-full',
        )}
        onClick={event => event.stopPropagation()}
      >
        <div className="flex-shrink-0 border-b border-[var(--border)] px-5 py-4">
          <div className="flex items-center justify-between">
            <h2
              id={titleId}
              className="text-[length:var(--text-sm)] font-semibold uppercase tracking-[0.05em] text-[var(--text-muted)]"
            >
              {title}
            </h2>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              dense
              onClick={onClose}
              aria-label="Close panel"
              className={coarseHitAreaCls}
            >
              <svg
                aria-hidden="true"
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
            </Button>
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
