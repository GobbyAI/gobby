import { useEffect, useRef } from 'react'
import './SidebarPanel.css'

interface SidebarPanelProps {
  isOpen: boolean
  onClose: () => void
  title: string | React.ReactNode
  width?: number
  headerContent?: React.ReactNode
  footer?: React.ReactNode
  children: React.ReactNode
}

export function SidebarPanel({ isOpen, onClose, title, width = 480, headerContent, footer, children }: SidebarPanelProps) {
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
      {isOpen && <div className="sidebar-backdrop" onClick={onClose} />}
      <div
        ref={panelRef}
        tabIndex={-1}
        className={`sidebar-panel ${isOpen ? 'sidebar-panel--open' : ''}`}
        style={{ width, outline: 'none' }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="sidebar-header">
          <div className="sidebar-header-top">
            <span className="sidebar-header-label">
              {title}
            </span>
            <button className="sidebar-close" onClick={onClose} type="button">
              <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
                <path d="M4 4l8 8M12 4l-8 8" />
              </svg>
            </button>
          </div>
          {headerContent}
        </div>
        <div className="sidebar-content">
          {children}
        </div>
        {footer && (
          <div className="sidebar-footer">
            {footer}
          </div>
        )}
      </div>
    </>
  )
}
