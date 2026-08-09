import type { KeyboardEvent, ReactNode } from 'react'

import { cn } from '../../lib/utils'
import { coarseHitAreaCls } from './controlStyles'

interface Tab {
  id: string
  label: string
  closeLabel?: string
  icon?: ReactNode
  badge?: number
}

interface TabBarProps {
  tabs: Tab[]
  activeTab: string
  onTabChange: (tabId: string) => void
  onTabClose?: (tabId: string) => void
  ariaLabel?: string
  className?: string
}

export function TabBar({
  tabs,
  activeTab,
  onTabChange,
  onTabClose,
  ariaLabel,
  className,
}: TabBarProps) {
  const handleKeyDown = (event: KeyboardEvent<HTMLButtonElement>, currentIndex: number) => {
    let nextIndex: number

    switch (event.key) {
      case 'ArrowLeft':
        nextIndex = (currentIndex - 1 + tabs.length) % tabs.length
        break
      case 'ArrowRight':
        nextIndex = (currentIndex + 1) % tabs.length
        break
      case 'Home':
        nextIndex = 0
        break
      case 'End':
        nextIndex = tabs.length - 1
        break
      default:
        return
    }

    event.preventDefault()
    const nextTab = tabs[nextIndex]
    const tabElements = event.currentTarget
      .closest('[role="tablist"]')
      ?.querySelectorAll<HTMLButtonElement>('[role="tab"]')
    tabElements?.[nextIndex]?.focus()
    onTabChange(nextTab.id)
  }

  return (
    <div
      className={cn(
        'mb-3 overflow-x-auto overflow-y-hidden [scrollbar-width:none] [&::-webkit-scrollbar]:hidden',
        className,
      )}
    >
      <div
        role="tablist"
        aria-label={ariaLabel}
        className="flex min-w-max border-b border-[var(--border)]"
      >
        {tabs.map((tab, index) => {
          const isActive = activeTab === tab.id
          return (
            <div
              key={tab.id}
              role="presentation"
              className={cn(
                'group -mb-px flex shrink-0 items-center border-b-2 border-b-transparent',
                isActive && 'border-b-[var(--accent)]',
              )}
            >
              <button
                type="button"
                role="tab"
                aria-selected={isActive}
                tabIndex={isActive ? 0 : -1}
                className={cn(
                  coarseHitAreaCls,
                  'inline-flex shrink-0 cursor-pointer items-center gap-1.5 whitespace-nowrap border-0 bg-transparent px-4 py-2 text-[length:var(--text-sm)] font-medium text-[var(--text-secondary)] transition-colors duration-150',
                  'hover:text-[var(--text-primary)]',
                  'focus-visible:outline-2 focus-visible:outline-[var(--accent)] focus-visible:[outline-offset:-2px]',
                  onTabClose && 'pr-2',
                  isActive && 'text-[var(--accent)]',
                )}
                onClick={() => onTabChange(tab.id)}
                onKeyDown={(event) => handleKeyDown(event, index)}
              >
                {tab.icon}
                <span className="overflow-hidden text-ellipsis">{tab.label}</span>
                {tab.badge !== undefined && tab.badge > 0 && (
                  <span
                    className={cn(
                      'inline-flex h-[18px] min-w-[18px] items-center justify-center rounded-full px-[5px] text-[length:var(--text-2xs)] font-semibold',
                      isActive
                        ? 'bg-[var(--accent)] text-[var(--accent-foreground)]'
                        : 'bg-[var(--bg-tertiary)] text-[var(--text-secondary)]',
                    )}
                  >
                    {tab.badge}
                  </span>
                )}
              </button>
              {onTabClose && (
                <button
                  type="button"
                  className={cn(
                    coarseHitAreaCls,
                    'mr-1 flex size-4 shrink-0 cursor-pointer items-center justify-center rounded-sm border-0 bg-transparent p-0 text-[var(--text-muted)] opacity-0 transition-opacity duration-100',
                    'group-hover:opacity-100 hover:bg-surface-tint-strong hover:text-[var(--text-primary)]',
                    'focus-visible:opacity-100 focus-visible:outline-2 focus-visible:outline-[var(--accent)]',
                    isActive && 'opacity-100',
                  )}
                  aria-label={`Close ${tab.closeLabel ?? tab.label}`}
                  onClick={() => onTabClose(tab.id)}
                >
                  <svg viewBox="0 0 16 16" className="size-3" aria-hidden="true">
                    <path d="M4 4l8 8m0-8-8 8" fill="none" stroke="currentColor" strokeWidth="1.5" />
                  </svg>
                </button>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
