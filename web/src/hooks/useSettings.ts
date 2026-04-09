import { useState, useEffect, useCallback, useRef } from 'react'
import type { ChatMode } from '../types/chat'

export type Theme = 'dark' | 'light' | 'system'

export interface Settings {
  fontSize: number // Base font size in pixels (12-24)
  model: string // Selected LLM model short name
  chatMode: ChatMode // Active chat mode
  theme: Theme // UI theme
  defaultChatMode: ChatMode // Default mode for new conversations
}

export const MODEL_OPTIONS = [
  { value: 'opus', label: 'Claude Opus' },
  { value: 'sonnet', label: 'Claude Sonnet' },
  { value: 'haiku', label: 'Claude Haiku' },
  { value: 'local', label: 'Local Model' },
] as const

const DEFAULT_SETTINGS: Settings = {
  fontSize: 16,
  model: 'opus',
  chatMode: 'plan',
  theme: 'dark',
  defaultChatMode: 'plan',
}

const STORAGE_KEY = 'gobby-settings'

/** Keys persisted to the backend (excludes per-conversation chatMode). */
type PersistableKey = 'fontSize' | 'model' | 'theme' | 'defaultChatMode'
const PERSISTABLE_KEYS: PersistableKey[] = ['fontSize', 'model', 'theme', 'defaultChatMode']

function loadFromLocalStorage(): Partial<Settings> {
  try {
    const stored = localStorage.getItem(STORAGE_KEY)
    if (stored) return JSON.parse(stored)
  } catch (e) {
    console.error('Failed to load settings from localStorage:', e)
  }
  return {}
}

function saveToLocalStorage(settings: Settings): void {
  try {
    const { chatMode: _, ...persistable } = settings
    localStorage.setItem(STORAGE_KEY, JSON.stringify(persistable))
  } catch (e) {
    console.error('Failed to save settings to localStorage:', e)
  }
}

async function fetchUISettings(): Promise<Partial<Settings> | null> {
  try {
    const res = await fetch('/api/config/ui-settings')
    if (res.ok) return await res.json()
  } catch {
    // API unavailable — fall back to localStorage only
  }
  return null
}

async function saveUISettings(settings: Settings): Promise<void> {
  try {
    const body: Record<string, unknown> = {}
    for (const key of PERSISTABLE_KEYS) {
      body[key] = settings[key]
    }
    await fetch('/api/config/ui-settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
  } catch {
    // Best-effort; localStorage is the fast cache
  }
}

export function useSettings() {
  const [settings, setSettings] = useState<Settings>(() => {
    // Immediate render from localStorage; API fetch overwrites in useEffect
    return { ...DEFAULT_SETTINGS, ...loadFromLocalStorage() }
  })

  const initialized = useRef(false)

  // On mount: fetch from API and merge (API wins over localStorage)
  useEffect(() => {
    let cancelled = false
    fetchUISettings().then((remote) => {
      if (cancelled || !remote) return
      setSettings((prev) => {
        const merged = { ...prev, ...remote }
        // Also update localStorage with the API values
        saveToLocalStorage(merged)
        return merged
      })
      initialized.current = true
    })
    return () => { cancelled = true }
  }, [])

  // Apply font size to document
  useEffect(() => {
    document.documentElement.style.setProperty(
      '--font-size-base',
      `${settings.fontSize}px`
    )
  }, [settings.fontSize])

  // Apply theme to document
  useEffect(() => {
    const applyTheme = (resolved: 'dark' | 'light') => {
      document.documentElement.setAttribute('data-theme', resolved)
    }

    if (settings.theme === 'system') {
      const mq = window.matchMedia('(prefers-color-scheme: dark)')
      applyTheme(mq.matches ? 'dark' : 'light')
      const handler = (e: MediaQueryListEvent) => applyTheme(e.matches ? 'dark' : 'light')
      mq.addEventListener('change', handler)
      return () => mq.removeEventListener('change', handler)
    } else {
      applyTheme(settings.theme)
    }
  }, [settings.theme])

  // Persist settings on change (localStorage + API)
  // Skip the initial render to avoid writing defaults before API fetch
  const isFirstRender = useRef(true)
  useEffect(() => {
    if (isFirstRender.current) {
      isFirstRender.current = false
      return
    }
    saveToLocalStorage(settings)
    saveUISettings(settings)
  }, [settings])

  const updateDefaultChatMode = useCallback((defaultChatMode: ChatMode) => {
    setSettings((prev) => ({ ...prev, defaultChatMode }))
  }, [])

  const updateFontSize = useCallback((size: number) => {
    setSettings((prev) => ({ ...prev, fontSize: size }))
  }, [])

  const updateModel = useCallback((model: string) => {
    setSettings((prev) => ({ ...prev, model }))
  }, [])

  const updateChatMode = useCallback((chatMode: ChatMode) => {
    setSettings((prev) => ({ ...prev, chatMode }))
  }, [])

  const updateTheme = useCallback((theme: Theme) => {
    setSettings((prev) => ({ ...prev, theme }))
  }, [])

  const resetSettings = useCallback(() => {
    setSettings(DEFAULT_SETTINGS)
  }, [])

  return {
    settings,
    updateFontSize,
    updateModel,
    updateChatMode,
    updateTheme,
    updateDefaultChatMode,
    resetSettings,
  }
}
