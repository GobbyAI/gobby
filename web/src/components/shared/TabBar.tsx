import type { KeyboardEvent } from 'react'

import { cn } from '../../lib/utils'

interface Tab {
  id: string
  label: string
  badge?: number
}

interface TabBarProps {
  tabs: Tab[]
  activeTab: string
  onTabChange: (tabId: string) => void
  className?: string
}

export function TabBar({ tabs, activeTab, onTabChange, className }: TabBarProps) {
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
    const tabElements = event.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>(
      '[role="tab"]',
    )
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
      <div role="tablist" className="flex min-w-max border-b border-[var(--border)]">
        {tabs.map((tab, index) => {
          const isActive = activeTab === tab.id
          return (
            <button
              key={tab.id}
              type="button"
              role="tab"
              aria-selected={isActive}
              tabIndex={isActive ? 0 : -1}
              className={cn(
                '-mb-px shrink-0 cursor-pointer whitespace-nowrap border-0 border-b-2 border-b-transparent bg-transparent px-4 py-2 text-[length:var(--text-sm)] font-medium text-[var(--text-secondary)] transition-colors duration-150',
                'pointer-coarse:min-h-11',
                'hover:text-[var(--text-primary)]',
                'focus-visible:outline-2 focus-visible:outline-[var(--accent)] focus-visible:[outline-offset:-2px]',
                isActive && 'border-b-[var(--accent)] text-[var(--accent)]',
              )}
              onClick={() => onTabChange(tab.id)}
              onKeyDown={(event) => handleKeyDown(event, index)}
            >
              {tab.label}
              {tab.badge !== undefined && tab.badge > 0 && (
                <span
                  className={cn(
                    'ml-1.5 inline-flex h-[18px] min-w-[18px] items-center justify-center rounded-full px-[5px] text-[length:var(--text-2xs)] font-semibold',
                    isActive
                      ? 'bg-[var(--accent)] text-[var(--accent-foreground)]'
                      : 'bg-[var(--bg-tertiary)] text-[var(--text-secondary)]',
                  )}
                >
                  {tab.badge}
                </span>
              )}
            </button>
          )
        })}
      </div>
    </div>
  )
}
