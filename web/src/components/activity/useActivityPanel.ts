import { useCallback, useEffect, useRef, useState } from 'react'
import type { ActivityTab } from './ActivityPanel'

const STORAGE_KEY_PINNED = 'gobby-activity-panel-pinned'
const STORAGE_KEY_WIDTH = 'gobby-activity-panel-width'
const STORAGE_KEY_TAB = 'gobby-activity-panel-tab'
const VALID_TABS: ActivityTab[] = [
  'sessions',
  'pipelines',
  'tasks',
  'files',
  'plans',
  'artifacts',
  'canvas',
]

export function useActivityPanel() {
  const [isPinned, setIsPinned] = useState(() => {
    try {
      const stored = localStorage.getItem(STORAGE_KEY_PINNED)
      if (stored !== null) return stored === 'true'
    } catch {
      /* ignore */
    }
    return window.innerWidth >= 1100
  })

  const [panelWidth, setPanelWidth] = useState(() => {
    try {
      const stored = localStorage.getItem(STORAGE_KEY_WIDTH)
      if (stored) {
        const w = parseInt(stored, 10)
        if (w >= 280 && w <= 1200) return w
      }
    } catch {
      /* ignore */
    }
    return 360
  })

  const [activeTab, setActiveTab] = useState<ActivityTab>(() => {
    try {
      const stored = localStorage.getItem(STORAGE_KEY_TAB) as ActivityTab | null
      if (stored && VALID_TABS.includes(stored)) return stored
    } catch {
      /* ignore */
    }
    return 'tasks'
  })

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY_PINNED, String(isPinned))
    } catch {
      /* ignore */
    }
  }, [isPinned])

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

  const autoOpenedRef = useRef(false)

  const showTab = useCallback(
    (tab: ActivityTab) => {
      setActiveTab(tab)
      if (!isPinned) {
        setIsPinned(true)
        autoOpenedRef.current = true
      }
    },
    [isPinned],
  )

  const closeIfAutoOpened = useCallback(() => {
    if (autoOpenedRef.current) {
      setIsPinned(false)
      autoOpenedRef.current = false
    }
  }, [])

  const togglePanel = useCallback(() => {
    autoOpenedRef.current = false
    setIsPinned((prev) => !prev)
  }, [])

  const handleTabChange = useCallback((tab: ActivityTab) => {
    autoOpenedRef.current = false
    setActiveTab(tab)
  }, [])

  return {
    isPinned,
    setIsPinned,
    panelWidth,
    setPanelWidth,
    activeTab,
    setActiveTab: handleTabChange,
    showTab,
    closeIfAutoOpened,
    togglePanel,
  }
}
