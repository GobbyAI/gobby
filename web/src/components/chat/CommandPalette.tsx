import { useState, useCallback, useEffect, useRef, useMemo } from 'react'
import type { GobbySession } from '../../hooks/useSessions'
import { useNow } from '../../hooks/useNow'
import { formatRelativeTime } from '../../utils/formatTime'

export interface CommandPaletteAction {
  id: string
  label: string
  icon?: string
  category: 'action' | 'navigate'
  onSelect: () => void
}

interface CommandPaletteProps {
  isOpen: boolean
  onClose: () => void
  sessions: GobbySession[]
  activeSessionId: string | null
  onSelectSession: (session: GobbySession) => void
  onDeleteSession?: (session: GobbySession) => void
  onRenameSession?: (id: string, title: string) => void  // future: inline rename
  actions: CommandPaletteAction[]
}

export function CommandPalette({
  isOpen,
  onClose,
  sessions,
  activeSessionId,
  onSelectSession,
  onDeleteSession,
  onRenameSession: _onRenameSession,
  actions,
}: CommandPaletteProps) {
  void _onRenameSession // future: inline rename
  const [query, setQuery] = useState('')
  const [selectedIndex, setSelectedIndex] = useState(0)
  const inputRef = useRef<HTMLInputElement>(null)
  const listRef = useRef<HTMLDivElement>(null)
  const now = useNow()

  // Reset on open
  useEffect(() => {
    if (isOpen) {
      setQuery('')
      setSelectedIndex(0)
      // Focus input after animation frame
      requestAnimationFrame(() => inputRef.current?.focus())
    }
  }, [isOpen])

  // Filter items based on query
  const { filteredSessions, filteredActions, allItems } = useMemo(() => {
    const q = query.toLowerCase().trim()
    const showOnlySessions = q.startsWith('#')
    const showOnlyActions = q.startsWith('>')
    const searchTerm = showOnlySessions ? q.slice(1) : showOnlyActions ? q.slice(1).trim() : q

    const filteredSessions = showOnlyActions
      ? []
      : sessions.filter((s) => {
          if (!searchTerm) return true
          const title = (s.title || '').toLowerCase()
          const ref = s.ref.toLowerCase()
          const seq = s.seq_num != null ? `#${s.seq_num}` : ''
          return title.includes(searchTerm) || ref.includes(searchTerm) || seq.includes(searchTerm)
        })

    const filteredActions = showOnlySessions
      ? []
      : actions.filter((a) => {
          if (!searchTerm) return true
          return a.label.toLowerCase().includes(searchTerm)
        })

    const actionItems = filteredActions.filter((a) => a.category === 'action')
    const navItems = filteredActions.filter((a) => a.category === 'navigate')

    const allItems: Array<{ type: 'session'; session: GobbySession } | { type: 'action'; action: CommandPaletteAction }> = []
    for (const s of filteredSessions) allItems.push({ type: 'session', session: s })
    for (const a of actionItems) allItems.push({ type: 'action', action: a })
    for (const a of navItems) allItems.push({ type: 'action', action: a })

    return { filteredSessions, filteredActions, allItems }
  }, [query, sessions, actions])

  // Clamp selection
  useEffect(() => {
    setSelectedIndex((prev) => Math.min(prev, Math.max(0, allItems.length - 1)))
  }, [allItems.length])

  // Scroll selected into view
  useEffect(() => {
    const list = listRef.current
    if (!list) return
    const selected = list.querySelector('[data-selected="true"]') as HTMLElement | null
    if (selected) {
      selected.scrollIntoView({ block: 'nearest' })
    }
  }, [selectedIndex])

  const handleSelect = useCallback(
    (index: number) => {
      const item = allItems[index]
      if (!item) return
      if (item.type === 'session') {
        onSelectSession(item.session)
      } else {
        item.action.onSelect()
      }
      onClose()
    },
    [allItems, onSelectSession, onClose],
  )

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === 'ArrowDown') {
        e.preventDefault()
        setSelectedIndex((i) => Math.min(i + 1, allItems.length - 1))
      } else if (e.key === 'ArrowUp') {
        e.preventDefault()
        setSelectedIndex((i) => Math.max(i - 1, 0))
      } else if (e.key === 'Enter') {
        e.preventDefault()
        handleSelect(selectedIndex)
      } else if (e.key === 'Escape') {
        e.preventDefault()
        onClose()
      } else if (e.key === 'Backspace' && query === '' && allItems[selectedIndex]?.type === 'session') {
        // Delete session with backspace when query is empty
        const session = allItems[selectedIndex].session
        if (onDeleteSession) {
          e.preventDefault()
          onDeleteSession(session)
        }
      }
    },
    [allItems, selectedIndex, handleSelect, onClose, query, onDeleteSession],
  )

  // Group sessions by recency + precompute index maps (must be above early return to avoid hook ordering issues)
  const { sessionIndexMap, actionIndexMap, todaySessions, weekSessions, olderSessions } = useMemo(() => {
    const todaySessions: GobbySession[] = []
    const weekSessions: GobbySession[] = []
    const olderSessions: GobbySession[] = []
    for (const s of filteredSessions) {
      const age = now - new Date(s.updated_at).getTime()
      if (age < 86400000) todaySessions.push(s)
      else if (age < 604800000) weekSessions.push(s)
      else olderSessions.push(s)
    }

    const sessionIndexMap = new Map<string, number>()
    const actionIndexMap = new Map<string, number>()
    let idx = 0
    for (const s of todaySessions) sessionIndexMap.set(s.id, idx++)
    for (const s of weekSessions) sessionIndexMap.set(s.id, idx++)
    for (const s of olderSessions) sessionIndexMap.set(s.id, idx++)
    for (const a of filteredActions.filter((a) => a.category === 'action')) actionIndexMap.set(a.id, idx++)
    for (const a of filteredActions.filter((a) => a.category === 'navigate')) actionIndexMap.set(a.id, idx++)
    return { sessionIndexMap, actionIndexMap, todaySessions, weekSessions, olderSessions }
  }, [filteredSessions, filteredActions, now])

  if (!isOpen) return null

  return (
    <>
      <div className="command-palette-overlay" onClick={onClose} />
      <div className="command-palette-container" role="dialog" aria-label="Command palette">
        <div className="command-palette-input-wrap">
          <SearchIcon />
          <input
            ref={inputRef}
            className="command-palette-input"
            placeholder="Search sessions, actions, pages..."
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={handleKeyDown}
            autoComplete="off"
            spellCheck={false}
          />
          <kbd className="command-palette-kbd">Esc</kbd>
        </div>

        <div className="command-palette-list" ref={listRef}>
          {allItems.length === 0 && (
            <div className="command-palette-empty">No results</div>
          )}

          {/* Sessions */}
          {todaySessions.length > 0 && (
            <>
              <div className="command-palette-group-label">Today</div>
              {todaySessions.map((s) => {
                const idx = sessionIndexMap.get(s.id)!
                return (
                  <SessionItem
                    key={s.id}
                    session={s}
                    isActive={s.id === activeSessionId}
                    isSelected={idx === selectedIndex}
                    onSelect={() => handleSelect(idx)}
                    onHover={() => setSelectedIndex(idx)}
                  />
                )
              })}
            </>
          )}
          {weekSessions.length > 0 && (
            <>
              <div className="command-palette-group-label">This Week</div>
              {weekSessions.map((s) => {
                const idx = sessionIndexMap.get(s.id)!
                return (
                  <SessionItem
                    key={s.id}
                    session={s}
                    isActive={s.id === activeSessionId}
                    isSelected={idx === selectedIndex}
                    onSelect={() => handleSelect(idx)}
                    onHover={() => setSelectedIndex(idx)}
                  />
                )
              })}
            </>
          )}
          {olderSessions.length > 0 && (
            <>
              <div className="command-palette-group-label">Older</div>
              {olderSessions.map((s) => {
                const idx = sessionIndexMap.get(s.id)!
                return (
                  <SessionItem
                    key={s.id}
                    session={s}
                    isActive={s.id === activeSessionId}
                    isSelected={idx === selectedIndex}
                    onSelect={() => handleSelect(idx)}
                    onHover={() => setSelectedIndex(idx)}
                  />
                )
              })}
            </>
          )}

          {/* Actions */}
          {filteredActions.filter((a) => a.category === 'action').length > 0 && (
            <>
              <div className="command-palette-group-label">Actions</div>
              {filteredActions
                .filter((a) => a.category === 'action')
                .map((a) => {
                  const idx = actionIndexMap.get(a.id)!
                  return (
                    <ActionItem
                      key={a.id}
                      action={a}
                      isSelected={idx === selectedIndex}
                      onSelect={() => handleSelect(idx)}
                      onHover={() => setSelectedIndex(idx)}
                    />
                  )
                })}
            </>
          )}

          {/* Navigate */}
          {filteredActions.filter((a) => a.category === 'navigate').length > 0 && (
            <>
              <div className="command-palette-group-label">Navigate</div>
              {filteredActions
                .filter((a) => a.category === 'navigate')
                .map((a) => {
                  const idx = actionIndexMap.get(a.id)!
                  return (
                    <ActionItem
                      key={a.id}
                      action={a}
                      isSelected={idx === selectedIndex}
                      onSelect={() => handleSelect(idx)}
                      onHover={() => setSelectedIndex(idx)}
                    />
                  )
                })}
            </>
          )}
        </div>

        <div className="command-palette-footer">
          <span><kbd>&uarr;&darr;</kbd> navigate</span>
          <span><kbd>&crarr;</kbd> select</span>
          <span><kbd>#</kbd> sessions</span>
          <span><kbd>&gt;</kbd> actions</span>
        </div>
      </div>
    </>
  )
}

function SessionItem({
  session,
  isActive,
  isSelected,
  onSelect,
  onHover,
}: {
  session: GobbySession
  isActive: boolean
  isSelected: boolean
  onSelect: () => void
  onHover: () => void
}) {
  const seqLabel = session.seq_num != null ? `#${session.seq_num}` : null
  const titleText = session.title || `Chat ${session.ref}`

  return (
    <div
      className={`command-palette-item${isSelected ? ' selected' : ''}${isActive ? ' active' : ''}`}
      onClick={onSelect}
      onMouseEnter={onHover}
      data-selected={isSelected}
      role="option"
      aria-selected={isSelected}
    >
      <span className="command-palette-item-dot" />
      <span className="command-palette-item-ref">{seqLabel}</span>
      <span className="command-palette-item-title">{titleText}</span>
      <span className="command-palette-item-time">{formatRelativeTime(session.updated_at)}</span>
    </div>
  )
}

function ActionItem({
  action,
  isSelected,
  onSelect,
  onHover,
}: {
  action: CommandPaletteAction
  isSelected: boolean
  onSelect: () => void
  onHover: () => void
}) {
  return (
    <div
      className={`command-palette-item${isSelected ? ' selected' : ''}`}
      onClick={onSelect}
      onMouseEnter={onHover}
      data-selected={isSelected}
      role="option"
      aria-selected={isSelected}
    >
      <span className="command-palette-item-icon">{action.icon ?? (action.category === 'navigate' ? '\u2192' : '+')}</span>
      <span className="command-palette-item-title">{action.label}</span>
    </div>
  )
}

function SearchIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-muted-foreground shrink-0">
      <circle cx="11" cy="11" r="8" />
      <line x1="21" y1="21" x2="16.65" y2="16.65" />
    </svg>
  )
}
