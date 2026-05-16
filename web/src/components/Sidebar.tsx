import React from 'react'
import { LogoutIcon } from './icons'

interface NavItem {
  id: string
  label: string
  icon: React.ReactNode
  separator?: boolean
}

interface SidebarProps {
  items: NavItem[]
  activeItem: string
  isOpen: boolean
  onItemSelect: (itemId: string) => void
  onClose: () => void
  /** When provided (auth enabled + signed in), renders a Logout entry under Configuration. */
  onLogout?: () => void
}

export function Sidebar({ items, activeItem, isOpen, onItemSelect, onClose, onLogout }: SidebarProps) {
  return (
    <>
      {isOpen && <div className="sidebar-overlay" onClick={onClose} />}
      <nav className={`sidebar ${isOpen ? 'open' : ''}`}>
        <div className="sidebar-header">
          <button
            type="button"
            className="sidebar-collapse-btn"
            onClick={onClose}
            aria-label="Collapse menu"
          >
            <CollapseIcon />
          </button>
        </div>
        <div className="sidebar-nav">
          {items.map((item) => (
            <React.Fragment key={item.id}>
              {item.separator && <hr className="sidebar-separator" />}
              <button
                className={`sidebar-item ${activeItem === item.id ? 'active' : ''}`}
                onClick={() => {
                  onItemSelect(item.id)
                  onClose()
                }}
              >
                <span className="sidebar-item-icon">{item.icon}</span>
                <span className="sidebar-item-label">{item.label}</span>
              </button>
            </React.Fragment>
          ))}
          {onLogout && (
            <>
              <hr className="sidebar-separator" />
              <button
                type="button"
                className="sidebar-item"
                onClick={() => {
                  onLogout()
                  onClose()
                }}
                aria-label="Sign out"
              >
                <span className="sidebar-item-icon"><LogoutIcon /></span>
                <span className="sidebar-item-label">Logout</span>
              </button>
            </>
          )}
        </div>
      </nav>
    </>
  )
}

function CollapseIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <polyline points="11 17 6 12 11 7" />
      <polyline points="18 17 13 12 18 7" />
    </svg>
  )
}
