import { useState, useCallback, useEffect, useId, useRef, useMemo } from 'react'
import type { GobbySession } from '../../types/sessions'
import { useNow } from '../../hooks/useNow'
import { formatRelativeTime } from '../../utils/formatTime'
import { getSessionTitleText } from '../../lib/sessionTitle'

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
  const invokerRef = useRef<HTMLElement | null>(null)
  const listboxId = useId()
  const optionIdPrefix = useId()
  const now = useNow()

  // Reset on open
  useEffect(() => {
    if (isOpen) {
      invokerRef.current = document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null
      setQuery('')
      setSelectedIndex(0)
      // Focus input after animation frame
      const frame = requestAnimationFrame(() => inputRef.current?.focus())
      return () => {
        cancelAnimationFrame(frame)
        invokerRef.current?.focus()
        invokerRef.current = null
      }
    }
  }, [isOpen])

  // Filter items based on query
  const {
    filteredActions,
    allItems,
    sessionIndexMap,
    actionIndexMap,
    todaySessions,
    weekSessions,
    olderSessions,
  } = useMemo(() => {
    const q = query.toLowerCase().trim()
    const showOnlySessions = q.startsWith('#')
    const showOnlyActions = q.startsWith('>')
    const searchTerm = showOnlySessions ? q.slice(1) : showOnlyActions ? q.slice(1).trim() : q

    const filteredSessions = showOnlyActions
      ? []
      : sessions
          .filter((s) => {
            if (!searchTerm) return true
            const title = (s.title || '').toLowerCase()
            const ref = (s.ref ?? '').toLowerCase()
            const seq = s.seq_num != null ? `#${s.seq_num}` : ''
            return title.includes(searchTerm) || ref.includes(searchTerm) || seq.includes(searchTerm)
          })
          // Order by ref (#N) descending so each recency bucket lists newest first.
          .sort((a, b) => (b.seq_num ?? 0) - (a.seq_num ?? 0))

    const filteredActions = showOnlySessions
      ? []
      : actions.filter((a) => {
          if (!searchTerm) return true
          return a.label.toLowerCase().includes(searchTerm)
        })

    const actionItems = filteredActions.filter((a) => a.category === 'action')
    const navItems = filteredActions.filter((a) => a.category === 'navigate')

    const todaySessions: GobbySession[] = []
    const weekSessions: GobbySession[] = []
    const olderSessions: GobbySession[] = []
    for (const session of filteredSessions) {
      const age = now - new Date(session.updated_at).getTime()
      if (age < 86400000) todaySessions.push(session)
      else if (age < 604800000) weekSessions.push(session)
      else olderSessions.push(session)
    }

    const allItems: Array<{ type: 'session'; session: GobbySession } | { type: 'action'; action: CommandPaletteAction }> = []
    for (const session of todaySessions) allItems.push({ type: 'session', session })
    for (const session of weekSessions) allItems.push({ type: 'session', session })
    for (const session of olderSessions) allItems.push({ type: 'session', session })
    for (const a of actionItems) allItems.push({ type: 'action', action: a })
    for (const a of navItems) allItems.push({ type: 'action', action: a })

    const sessionIndexMap = new Map<string, number>()
    const actionIndexMap = new Map<string, number>()
    allItems.forEach((item, index) => {
      if (item.type === 'session') sessionIndexMap.set(item.session.id, index)
      else actionIndexMap.set(item.action.id, index)
    })

    return {
      filteredActions,
      allItems,
      sessionIndexMap,
      actionIndexMap,
      todaySessions,
      weekSessions,
      olderSessions,
    }
  }, [query, sessions, actions, now])

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
      } else if (e.key === 'Tab') {
        e.preventDefault()
        inputRef.current?.focus()
      } else if (e.key === 'Backspace' && query === '' && allItems[selectedIndex]?.type === 'session') {
        // Delete session with backspace when query is empty
        const session = allItems[selectedIndex].session
        if (onDeleteSession && session.session_type === 'web_chat') {
          e.preventDefault()
          onDeleteSession(session)
        }
      }
    },
    [allItems, selectedIndex, handleSelect, onClose, query, onDeleteSession],
  )

  if (!isOpen) return null

  return (
    <>
      <div className="command-palette-overlay" onClick={onClose} />
      <div
        className="command-palette-container"
        role="dialog"
        aria-modal="true"
        aria-label="Command palette"
      >
        <div className="command-palette-input-wrap">
          <SearchIcon />
          <input
            ref={inputRef}
            className="command-palette-input"
            placeholder="Search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={handleKeyDown}
            role="combobox"
            aria-expanded="true"
            aria-controls={listboxId}
            aria-activedescendant={
              allItems.length > 0 ? `${optionIdPrefix}-${selectedIndex}` : undefined
            }
            aria-autocomplete="list"
            autoComplete="off"
            spellCheck={false}
          />
          <kbd className="command-palette-kbd">Esc</kbd>
        </div>

        <div
          className="command-palette-list"
          ref={listRef}
          id={listboxId}
          role="listbox"
          aria-label="Command palette results"
        >
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
                    id={`${optionIdPrefix}-${idx}`}
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
                    id={`${optionIdPrefix}-${idx}`}
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
                    id={`${optionIdPrefix}-${idx}`}
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
                      id={`${optionIdPrefix}-${idx}`}
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
                      id={`${optionIdPrefix}-${idx}`}
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
  id,
  session,
  isActive,
  isSelected,
  onSelect,
  onHover,
}: {
  id: string
  session: GobbySession
  isActive: boolean
  isSelected: boolean
  onSelect: () => void
  onHover: () => void
}) {
  const seqLabel = session.seq_num != null ? `#${session.seq_num}` : null
  const titleText = getSessionTitleText(session.title)

  return (
    <div
      id={id}
      className={`command-palette-item${isSelected ? ' selected' : ''}${isActive ? ' active' : ''}`}
      onClick={onSelect}
      onMouseEnter={onHover}
      data-selected={isSelected}
      role="option"
      aria-selected={isSelected}
    >
      <span className="command-palette-item-dot" />
      <span className="command-palette-item-ref" data-testid="session-ref">{seqLabel}</span>
      <span className="command-palette-item-title">{titleText}</span>
      <span className="command-palette-item-time">{formatRelativeTime(session.updated_at)}</span>
    </div>
  )
}

function ActionItem({
  id,
  action,
  isSelected,
  onSelect,
  onHover,
}: {
  id: string
  action: CommandPaletteAction
  isSelected: boolean
  onSelect: () => void
  onHover: () => void
}) {
  return (
    <div
      id={id}
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
