import { createContext, useContext } from 'react'
import type { SettingsSectionId } from '../sections'

/** Predicate reporting whether a section currently holds unsaved edits. */
export type SectionDirtyGuard = () => boolean

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
