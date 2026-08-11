import { useState, useEffect, useCallback, useRef } from 'react'
import {
  DEFAULT_PLAN_PENDING_VARIANT,
  normalizePlanPendingVariant,
  type PlanPendingVariant,
} from '../components/chat/planPendingSurface'
import { normalizeChatMode, type ChatMode } from '../types/chat'
import { configurationClient } from '../api/config'

export type Theme = 'dark' | 'light' | 'system'
export type VoiceInputMode = 'ptt' | 'vad'
/**
 * Display density for the app chrome. Client-only: persisted to localStorage
 * and applied as `data-density` on the document root, but never sent to the
 * backend — the ui_settings API has no density field, so a write would be a
 * silent no-op. Kept out of PERSISTABLE_KEYS for exactly that reason.
 */
export type Density = 'comfortable' | 'compact'

export interface Settings {
  fontSize: number // Base font size in pixels (12-24)
  model: string // Selected LLM model short name
  chatMode: ChatMode // Active chat mode
  theme: Theme // UI theme
  defaultChatMode: ChatMode // Default mode for new conversations
  sttEnabled: boolean
  ttsEnabled: boolean
  voiceInputMode: VoiceInputMode
  planPendingVariant: PlanPendingVariant
  density: Density
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
  sttEnabled: false,
  ttsEnabled: false,
  voiceInputMode: 'ptt',
  planPendingVariant: DEFAULT_PLAN_PENDING_VARIANT,
  density: 'comfortable',
}

const STORAGE_KEY = 'gobby-settings'
const DARK_LOGO_PATH = '/logo.png'
const LIGHT_LOGO_PATH = '/logo-light.png'
const ICON_CACHE_PARAM = 'v=2'
const [ICON_CACHE_KEY = 'v', ICON_CACHE_VALUE = ''] = ICON_CACHE_PARAM.split('=')

/** Keys persisted to the backend (excludes per-conversation chatMode). */
type PersistableKey =
  | 'fontSize'
  | 'model'
  | 'theme'
  | 'defaultChatMode'
  | 'sttEnabled'
  | 'ttsEnabled'
  | 'voiceInputMode'
  | 'planPendingVariant'
const PERSISTABLE_KEYS: PersistableKey[] = [
  'fontSize',
  'model',
  'theme',
  'defaultChatMode',
  'sttEnabled',
  'ttsEnabled',
  'voiceInputMode',
  'planPendingVariant',
]

function loadFromLocalStorage(): unknown {
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

const DENSITY_VALUES: readonly Density[] = ['comfortable', 'compact']

function normalizeDensity(value: unknown): Density {
  return DENSITY_VALUES.includes(value as Density) ? (value as Density) : 'comfortable'
}

const FONT_SIZE_MIN = 12
const FONT_SIZE_MAX = 24

function normalizePersistedSettings(value: unknown): Partial<Settings> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return {}

  const settings = value as Partial<Settings>
  const normalized: Partial<Settings> = { ...settings }
  if (Object.prototype.hasOwnProperty.call(value, 'fontSize')) {
    normalized.fontSize =
      typeof settings.fontSize === 'number' && Number.isFinite(settings.fontSize)
        ? Math.min(FONT_SIZE_MAX, Math.max(FONT_SIZE_MIN, settings.fontSize))
        : DEFAULT_SETTINGS.fontSize
  }
  if (settings.chatMode) normalized.chatMode = normalizeChatMode(settings.chatMode)
  if (settings.defaultChatMode) {
    normalized.defaultChatMode = normalizeChatMode(settings.defaultChatMode)
  }
  if (settings.planPendingVariant !== undefined && settings.planPendingVariant !== null) {
    normalized.planPendingVariant = normalizePlanPendingVariant(settings.planPendingVariant)
  }
  if (settings.density !== undefined && settings.density !== null) {
    normalized.density = normalizeDensity(settings.density)
  }
  return normalized
}

async function fetchUISettings(): Promise<Partial<Settings> | null> {
  try {
    const snapshot = await configurationClient.fetchValues()
    const settings = snapshot?.desired.ui_settings
    if (settings && typeof settings === 'object' && !Array.isArray(settings)) {
      return settings as Partial<Settings>
    }
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
    // Last-write-wins: retry once after a revision conflict so a concurrent
    // config commit doesn't silently drop the preference write.
    await configurationClient.patchLastWriteWins({ ui_settings: body })
  } catch {
    // Best-effort; localStorage is the fast cache
  }
}

function logoPathForTheme(theme: 'dark' | 'light'): string {
  return theme === 'light' ? LIGHT_LOGO_PATH : DARK_LOGO_PATH
}

export function cacheBustedIconHref(path: string): string {
  const fragmentIndex = path.indexOf('#')
  const pathWithoutFragment = fragmentIndex >= 0 ? path.slice(0, fragmentIndex) : path
  const fragment = fragmentIndex >= 0 ? path.slice(fragmentIndex) : ''
  const queryIndex = pathWithoutFragment.indexOf('?')
  const basePath = queryIndex >= 0 ? pathWithoutFragment.slice(0, queryIndex) : pathWithoutFragment
  const query = queryIndex >= 0 ? pathWithoutFragment.slice(queryIndex + 1) : ''
  const params = new URLSearchParams(query)
  params.delete(ICON_CACHE_KEY)
  params.append(ICON_CACHE_KEY, ICON_CACHE_VALUE)
  const queryString = params.toString()
  return `${basePath}${queryString ? `?${queryString}` : ''}${fragment}`
}

function upsertIconLinks(rel: string, href: string, type?: string): void {
  const links = Array.from(document.head.querySelectorAll<HTMLLinkElement>(`link[rel="${rel}"]`))
  if (links.length === 0) {
    const link = document.createElement('link')
    link.setAttribute('rel', rel)
    document.head.appendChild(link)
    links.push(link)
  }
  for (const link of links) {
    if (type) link.setAttribute('type', type)
    link.setAttribute('href', href)
  }
}

function updateDocumentIcons(theme: 'dark' | 'light'): void {
  const href = cacheBustedIconHref(logoPathForTheme(theme))
  upsertIconLinks('icon', href, 'image/png')
  upsertIconLinks('apple-touch-icon', href)
}

export function useSettings() {
  const [settings, setSettings] = useState<Settings>(() => {
    // Immediate render from localStorage; API fetch overwrites in useEffect
    return { ...DEFAULT_SETTINGS, ...normalizePersistedSettings(loadFromLocalStorage()) }
  })

  const initialized = useRef(false)
  const pendingChanges = useRef<Partial<Settings>>({})
  const skipNextPersistence = useRef(false)

  // On mount: fetch from API and merge (API wins over localStorage, while
  // explicit changes made during the fetch win over the API).
  useEffect(() => {
    let cancelled = false
    fetchUISettings().then((remote) => {
      if (cancelled) return
      initialized.current = true

      const changes = pendingChanges.current
      pendingChanges.current = {}
      if (!remote && Object.keys(changes).length === 0) return
      skipNextPersistence.current = Object.keys(changes).length === 0

      setSettings((prev) => {
        const merged = {
          ...prev,
          ...normalizePersistedSettings(remote),
          ...changes,
        }
        // Also update localStorage with the API values
        saveToLocalStorage(merged)
        return merged
      })
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
      updateDocumentIcons(resolved)
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

  // Apply display density to document (client-only; see Density type). Mirrors
  // the theme effect so the preference takes hold app-wide on mount and on any
  // change, regardless of which surface toggled it.
  useEffect(() => {
    document.documentElement.setAttribute('data-density', settings.density)
  }, [settings.density])

  // Persist settings on change (localStorage + API)
  useEffect(() => {
    if (!initialized.current) return
    if (skipNextPersistence.current) {
      skipNextPersistence.current = false
      return
    }
    saveToLocalStorage(settings)
    saveUISettings(settings)
  }, [settings])

  const updateSettings = useCallback((changes: Partial<Settings>) => {
    if (!initialized.current) {
      pendingChanges.current = { ...pendingChanges.current, ...changes }
    }
    setSettings((prev) => ({ ...prev, ...changes }))
  }, [])

  const updateDefaultChatMode = useCallback((defaultChatMode: ChatMode) => {
    updateSettings({ defaultChatMode })
  }, [updateSettings])

  const updateFontSize = useCallback((size: number) => {
    updateSettings({ fontSize: size })
  }, [updateSettings])

  const updateModel = useCallback((model: string) => {
    updateSettings({ model })
  }, [updateSettings])

  const updateChatMode = useCallback((chatMode: ChatMode) => {
    updateSettings({ chatMode })
  }, [updateSettings])

  const updateTheme = useCallback((theme: Theme) => {
    updateSettings({ theme })
  }, [updateSettings])

  const updateSttEnabled = useCallback((sttEnabled: boolean) => {
    updateSettings({ sttEnabled })
  }, [updateSettings])

  const updateTtsEnabled = useCallback((ttsEnabled: boolean) => {
    updateSettings({ ttsEnabled })
  }, [updateSettings])

  const updateVoiceInputMode = useCallback((voiceInputMode: VoiceInputMode) => {
    updateSettings({ voiceInputMode })
  }, [updateSettings])

  const updatePlanPendingVariant = useCallback((planPendingVariant: PlanPendingVariant) => {
    updateSettings({ planPendingVariant })
  }, [updateSettings])

  const updateDensity = useCallback((density: Density) => {
    updateSettings({ density })
  }, [updateSettings])

  const resetSettings = useCallback(() => {
    updateSettings(DEFAULT_SETTINGS)
  }, [updateSettings])

  return {
    settings,
    updateFontSize,
    updateModel,
    updateChatMode,
    updateTheme,
    updateDefaultChatMode,
    updateSttEnabled,
    updateTtsEnabled,
    updateVoiceInputMode,
    updatePlanPendingVariant,
    updateDensity,
    resetSettings,
  }
}

/**
 * The full return shape of {@link useSettings}. Shared with the settings
 * overlay so one instance drives both the app chrome and the overlay section,
 * keeping prop-threaded values (e.g. planPendingVariant) live without reload.
 */
export type UseSettingsReturn = ReturnType<typeof useSettings>
