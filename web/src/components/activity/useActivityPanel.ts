import { useCallback, useEffect, useRef, useState } from 'react'
import { useDirtyGuardController } from './dirtyGuard'
import { ACTIVITY_PANEL_TABS, type ActivityTab } from './ActivityPanelTabs'

const STORAGE_KEY_LAYOUT = 'gobby-activity-panel-layout'
const STORAGE_KEY_WIDTH = 'gobby-activity-panel-width'
const STORAGE_KEY_TAB = 'gobby-activity-panel-tab-v2'

const VALID_TABS = ACTIVITY_PANEL_TABS.map(({ id }) => id)

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
export type ViewOverride = 'panel' | null

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

function normalizeStoredTab(value: string | null): ActivityTab | null {
  if (value && VALID_TABS.includes(value as ActivityTab)) return value as ActivityTab
  return null
}

function loadActiveTab(): ActivityTab {
  try {
    const stored = normalizeStoredTab(localStorage.getItem(STORAGE_KEY_TAB))
    if (stored) return stored
  } catch {
    /* ignore */
  }
  return 'sessions'
}

export function loadLayoutMode(): LayoutMode {
  try {
    const stored = localStorage.getItem(STORAGE_KEY_LAYOUT)
    if (stored && LAYOUT_MODES.includes(stored as LayoutMode)) {
      return stored as LayoutMode
    }
  } catch {
    /* ignore */
  }
  // New users default to split (chat + panel both visible), matching the
  // previous `isPinned: true` desktop default.
  return 'split'
}

export function useActivityPanel(isMobile: boolean) {
  const dirtyGuard = useDirtyGuardController()
  const [mode, setMode] = useState<LayoutMode>(loadLayoutMode)
  // Initial render always starts at chat (desktop and mobile). The desktop
  // `panel` preference only carries onto mobile via the crossing effect
  // below, never on a fresh mobile mount.
  const [mobileView, setMobileView] = useState<MobileView>('chat')
  const [viewOverride, setViewOverride] = useState<ViewOverride>(null)

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
  const [terminalSessionRequest, setTerminalSessionRequest] = useState<string | null>(null)

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
    void dirtyGuard.guardedRun(() => {
      autoOpenedRef.current = false
      setViewOverride(null)
      if (isMobile) {
        setMobileView(reduceMobileToggle)
        return
      }
      setMode(reduceToggleFromChat)
    })
  }, [dirtyGuard, isMobile])

  const toggleFromPanel = useCallback(() => {
    void dirtyGuard.guardedRun(() => {
      autoOpenedRef.current = false
      setViewOverride(null)
      if (isMobile) {
        setMobileView(reduceMobileToggle)
        return
      }
      setMode(reduceToggleFromPanel)
    })
  }, [dirtyGuard, isMobile])

  const showTab = useCallback(
    (tab: ActivityTab) => {
      void dirtyGuard.guardedRun(() => {
        setViewOverride(null)
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
      })
    },
    [dirtyGuard, isMobile],
  )

  // Cross-tab escape hatch (`gobby:open-command-palette` precedent): deep
  // components dispatch `gobby:show-activity-tab` with `{tab}` instead of
  // threading `showTab` through every panel surface.
  useEffect(() => {
    const handler = (event: Event) => {
      const detail = (event as CustomEvent<{ tab?: string; sessionId?: unknown }>).detail
      const normalized = normalizeStoredTab(detail?.tab ?? null)
      if (!normalized) return
      if (normalized === 'terminal' && typeof detail?.sessionId === 'string') {
        setTerminalSessionRequest(detail.sessionId)
      }
      showTab(normalized)
    }
    window.addEventListener('gobby:show-activity-tab', handler)
    return () => window.removeEventListener('gobby:show-activity-tab', handler)
  }, [showTab])

  const clearTerminalSessionRequest = useCallback(() => {
    setTerminalSessionRequest(null)
  }, [])

  const closeIfAutoOpened = useCallback(() => {
    void dirtyGuard.guardedRun(() => {
      setViewOverride(null)
      if (!autoOpenedRef.current) return
      autoOpenedRef.current = false
      if (isMobile) {
        setMobileView('chat')
        return
      }
      setMode((current) => (current === 'split' ? 'chat' : current))
    })
  }, [dirtyGuard, isMobile])

  // Used by mobile-only action handlers (plan approve, session swap) to return
  // to the chat after acting. No-op on desktop, where the chosen layout stays.
  const dismissOnMobile = useCallback(() => {
    void dirtyGuard.guardedRun(() => {
      if (isMobile) {
        setMobileView('chat')
      }
    })
  }, [dirtyGuard, isMobile])

  const handleTabChange = useCallback((tab: ActivityTab) => {
    void dirtyGuard.guardedRun(() => {
      autoOpenedRef.current = false
      setViewOverride(null)
      setActiveTab(tab)
    })
  }, [dirtyGuard])

  const baseEffectiveMode: LayoutMode = isMobile
    ? mobileView === 'panel'
      ? 'panel'
      : 'chat'
    : mode
  const effectiveViewOverride: ViewOverride = isMobile ? null : viewOverride
  const effectiveMode: LayoutMode =
    effectiveViewOverride === 'panel' ? 'panel' : baseEffectiveMode

  const requestPanelOverride = useCallback(() => {
    if (isMobile) {
      setViewOverride(null)
      return
    }
    setViewOverride('panel')
  }, [isMobile])

  const releasePanelOverride = useCallback(() => {
    setViewOverride(null)
  }, [])

  return {
    mode,
    effectiveMode,
    viewOverride: effectiveViewOverride,
    requestPanelOverride,
    releasePanelOverride,
    panelWidth,
    setPanelWidth,
    activeTab,
    terminalSessionRequest,
    clearTerminalSessionRequest,
    setActiveTab: handleTabChange,
    showTab,
    closeIfAutoOpened,
    toggleFromChat,
    toggleFromPanel,
    dismissOnMobile,
    dirtyGuard,
  }
}
