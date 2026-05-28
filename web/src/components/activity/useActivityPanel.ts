import { useCallback, useEffect, useRef, useState } from 'react'
import type { ActivityTab } from './ActivityPanelTabs'

const STORAGE_KEY_LAYOUT = 'gobby-activity-panel-layout'
const LEGACY_STORAGE_KEY_PINNED = 'gobby-activity-panel-pinned'
const STORAGE_KEY_WIDTH = 'gobby-activity-panel-width'
const LEGACY_STORAGE_KEY_TAB = 'gobby-activity-panel-tab'
const STORAGE_KEY_TAB = 'gobby-activity-panel-tab-v2'

const VALID_TABS: ActivityTab[] = [
  'sessions',
  'mcp',
  'tasks',
  'plans',
  'artifacts',
  'changes',
  'files',
  'canvas',
  'pipelines',
  'cron',
  'traces',
]

/**
 * Pane visibility is one enum, not two booleans, so "both panes collapsed"
 * is unrepresentable:
 *   - `chat`  — chat only, activity panel hidden
 *   - `split` — chat + activity panel side by side (the old `isPinned: true`)
 *   - `panel` — activity panel full-width, chat hidden (desktop only)
 *
 * `mode` is the persisted *desktop* preference. Mobile derives a binary
 * `mobileView` ('chat' | 'panel') and never writes the desktop key.
 */
export type LayoutMode = 'chat' | 'split' | 'panel'
export type MobileView = 'chat' | 'panel'

const LAYOUT_MODES: LayoutMode[] = ['chat', 'split', 'panel']

/** Chat-header button / Cmd+` / palette `toggle_panel` / `/panel`. */
export function reduceToggleFromChat(mode: LayoutMode): LayoutMode {
  if (mode === 'chat') return 'split'
  if (mode === 'split') return 'chat'
  return 'split' // panel -> split (chat returns next to the panel)
}

/** Panel-header button / `/chat`. */
export function reduceToggleFromPanel(mode: LayoutMode): LayoutMode {
  if (mode === 'split') return 'panel'
  if (mode === 'panel') return 'split'
  return 'split' // chat -> split (panel joins the chat)
}

function reduceMobileToggle(view: MobileView): MobileView {
  return view === 'panel' ? 'chat' : 'panel'
}

function normalizeStoredTab(value: string | null, legacy = false): ActivityTab | null {
  if (legacy && value === 'artifacts') return 'changes'
  if (value && VALID_TABS.includes(value as ActivityTab)) return value as ActivityTab
  return null
}

function loadActiveTab(): ActivityTab {
  try {
    const stored = normalizeStoredTab(localStorage.getItem(STORAGE_KEY_TAB))
    if (stored) return stored

    const legacy = normalizeStoredTab(localStorage.getItem(LEGACY_STORAGE_KEY_TAB), true)
    if (legacy) {
      localStorage.removeItem(LEGACY_STORAGE_KEY_TAB)
      return legacy
    }
  } catch {
    /* ignore */
  }
  return 'sessions'
}

/**
 * Resolve the persisted desktop layout mode, migrating the legacy
 * `gobby-activity-panel-pinned` boolean on first read:
 * `true` -> `split`, `false` -> `chat`. Idempotent: once the new key is
 * written the legacy key is removed and never consulted again.
 */
export function loadLayoutMode(): LayoutMode {
  try {
    const stored = localStorage.getItem(STORAGE_KEY_LAYOUT)
    if (stored && LAYOUT_MODES.includes(stored as LayoutMode)) {
      return stored as LayoutMode
    }

    const legacy = localStorage.getItem(LEGACY_STORAGE_KEY_PINNED)
    if (legacy !== null) {
      const migrated: LayoutMode = legacy === 'true' ? 'split' : 'chat'
      try {
        localStorage.setItem(STORAGE_KEY_LAYOUT, migrated)
      } catch {
        /* ignore */
      } finally {
        try {
          localStorage.removeItem(LEGACY_STORAGE_KEY_PINNED)
        } catch {
          /* ignore */
        }
      }
      return migrated
    }
  } catch {
    /* ignore */
  }
  // New users default to split (chat + panel both visible), matching the
  // previous `isPinned: true` desktop default.
  return 'split'
}

export function useActivityPanel(isMobile: boolean) {
  const [mode, setMode] = useState<LayoutMode>(loadLayoutMode)
  // Initial render always starts at chat (desktop and mobile). The desktop
  // `panel` preference only carries onto mobile via the crossing effect
  // below, never on a fresh mobile mount.
  const [mobileView, setMobileView] = useState<MobileView>('chat')

  const [panelWidth, setPanelWidth] = useState(() => {
    try {
      const stored = localStorage.getItem(STORAGE_KEY_WIDTH)
      if (stored) {
        const w = parseInt(stored, 10)
        if (w >= 320 && w <= 4000) return w
      }
    } catch {
      /* ignore */
    }
    return 360
  })

  const [activeTab, setActiveTab] = useState<ActivityTab>(loadActiveTab)

  useEffect(() => {
    if (isMobile) return
    try {
      localStorage.setItem(STORAGE_KEY_LAYOUT, mode)
    } catch {
      /* ignore */
    }
  }, [isMobile, mode])

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY_WIDTH, String(panelWidth))
    } catch {
      /* ignore */
    }
  }, [panelWidth])

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY_TAB, activeTab)
    } catch {
      /* ignore */
    }
  }, [activeTab])

  // Crossing desktop -> mobile derives the mobile binary from the persisted
  // desktop mode. Crossing back leaves `mode` untouched (mobile never writes
  // it), so the desktop layout is restored verbatim.
  const prevIsMobileRef = useRef(isMobile)
  useEffect(() => {
    if (!prevIsMobileRef.current && isMobile) {
      setMobileView(mode === 'panel' ? 'panel' : 'chat')
    }
    prevIsMobileRef.current = isMobile
  }, [isMobile, mode])

  const autoOpenedRef = useRef(false)

  const toggleFromChat = useCallback(() => {
    autoOpenedRef.current = false
    if (isMobile) {
      setMobileView(reduceMobileToggle)
      return
    }
    setMode(reduceToggleFromChat)
  }, [isMobile])

  const toggleFromPanel = useCallback(() => {
    autoOpenedRef.current = false
    if (isMobile) {
      setMobileView(reduceMobileToggle)
      return
    }
    setMode(reduceToggleFromPanel)
  }, [isMobile])

  const showTab = useCallback(
    (tab: ActivityTab) => {
      setActiveTab(tab)
      if (isMobile) {
        setMobileView((view) => {
          if (view !== 'panel') {
            autoOpenedRef.current = true
            return 'panel'
          }
          return view
        })
        return
      }
      setMode((current) => {
        if (current === 'chat') {
          autoOpenedRef.current = true
          return 'split'
        }
        return current
      })
    },
    [isMobile],
  )

  const closeIfAutoOpened = useCallback(() => {
    if (!autoOpenedRef.current) return
    autoOpenedRef.current = false
    if (isMobile) {
      setMobileView('chat')
      return
    }
    setMode((current) => (current === 'split' ? 'chat' : current))
  }, [isMobile])

  // Used by mobile-only action handlers (plan approve, session swap) to return
  // to the chat after acting. No-op on desktop, where the chosen layout stays.
  const dismissOnMobile = useCallback(() => {
    if (isMobile) {
      setMobileView('chat')
    }
  }, [isMobile])

  const handleTabChange = useCallback((tab: ActivityTab) => {
    autoOpenedRef.current = false
    setActiveTab(tab)
  }, [])

  const effectiveMode: LayoutMode = isMobile
    ? mobileView === 'panel'
      ? 'panel'
      : 'chat'
    : mode

  return {
    mode,
    effectiveMode,
    panelWidth,
    setPanelWidth,
    activeTab,
    setActiveTab: handleTabChange,
    showTab,
    closeIfAutoOpened,
    toggleFromChat,
    toggleFromPanel,
    dismissOnMobile,
  }
}
