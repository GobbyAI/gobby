import { createContext, useContext } from 'react'
import type { SettingsSectionId } from '../sections'
import type { UseSettingsReturn } from '../../../hooks/useSettings'

/** Predicate reporting whether a section currently holds unsaved edits. */
export type SectionDirtyGuard = () => boolean

/**
 * The app-owned default-provider selection, threaded so the Providers & Models
 * section can drive the same `selectedProvider` state (persisted by App) that
 * the chat ProviderPicker uses. The model half lives in `clientSettings`
 * (`ui_settings.model`); provider is App-local state, not a `useSettings` field.
 */
export interface ProviderSelectionContextValue {
  selectedProvider: string | null
  onSelectProvider: (provider: string) => void
}

/**
 * The app-owned active-project selection, threaded so the Projects & Sessions
 * section can drive the same `selectedProjectId` state (persisted by App to
 * `ui_settings.selectedProjectId`) that the chrome's ProjectSelector uses.
 * Project selection is App-local state, not a `useSettings` field.
 */
export interface ProjectSelectionContextValue {
  selectedProjectId: string | null
  onSelectProject: (projectId: string) => void
}

export interface SaveConfigResult {
  ok: boolean
  errors?: string[]
}

/**
 * Everything a settings section needs from the overlay: the loaded config
 * surface (shared so it loads once per overlay open) plus the dirty-guard
 * registry from `useSettingsOverlay`. Sections read this via
 * `useSettingsSectionContext`; the provider lives in SettingsSectionProvider
 * and tests inject a fake value through the context provider directly.
 */
export interface SettingsSectionContextValue {
  schema: Record<string, unknown> | null
  configValues: Record<string, unknown>
  secretKeys: string[]
  isLoading: boolean
  saveConfig: (values: Record<string, unknown>) => Promise<SaveConfigResult>
  registerDirtyGuard: (
    section: SettingsSectionId,
    isDirty: SectionDirtyGuard,
  ) => () => void
  /**
   * The single app-wide `useSettings` instance, shared so client-side sections
   * (Appearance, Chat & Voice, Providers, Projects) read and mutate the same
   * ui_settings state the app chrome uses. Absent only when no provider is
   * mounted (the fallback below), which sections must tolerate.
   */
  clientSettings?: UseSettingsReturn
  /**
   * App-owned default-provider selection. Absent when no provider is mounted
   * (the fallback below) or the host does not supply it; sections must tolerate
   * its absence.
   */
  providerSelection?: ProviderSelectionContextValue
  /**
   * App-owned active-project selection. Absent when no provider is mounted
   * (the fallback below) or the host does not supply it; sections must tolerate
   * its absence.
   */
  projectSelection?: ProjectSelectionContextValue
  /**
   * Live rules-engine enforcement flag. Unlike the draft-backed config rows,
   * this is a standalone surface (`/api/rules`) the provider reads/writes
   * directly via `useConfiguration`; the Automation & Workflows section renders
   * it as an immediate toggle. Absent when no provider is mounted (the fallback
   * below); sections must tolerate its absence.
   */
  rulesEnforcement?: boolean
  /** Persist the rules-enforcement flag; resolves true on a successful write. */
  setRulesEnforcement?: (enabled: boolean) => Promise<boolean>
}

export const noopRegister: SettingsSectionContextValue['registerDirtyGuard'] =
  () => () => {}

const FALLBACK_CONTEXT: SettingsSectionContextValue = {
  schema: null,
  configValues: {},
  secretKeys: [],
  isLoading: false,
  saveConfig: async () => ({ ok: false, errors: ['Settings context unavailable'] }),
  registerDirtyGuard: noopRegister,
}

export const SettingsSectionContext =
  createContext<SettingsSectionContextValue>(FALLBACK_CONTEXT)

export function useSettingsSectionContext(): SettingsSectionContextValue {
  return useContext(SettingsSectionContext)
}
