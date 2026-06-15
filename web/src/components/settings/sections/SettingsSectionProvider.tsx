import { useCallback, useEffect, useMemo } from 'react'
import type { ReactNode } from 'react'
import { useConfiguration } from '../../../hooks/useConfiguration'
import type { UseSettingsReturn } from '../../../hooks/useSettings'
import {
  noopRegister,
  SettingsSectionContext,
  type SaveConfigResult,
  type SettingsSectionContextValue,
} from './SettingsSectionContext'

export interface SettingsSectionProviderProps {
  /** The overlay controller's dirty-guard registrar (see useSettingsOverlay). */
  registerDirtyGuard?: SettingsSectionContextValue['registerDirtyGuard']
  /**
   * The app-wide `useSettings` instance, supplied by the overlay so client-side
   * sections share the same ui_settings state as the chrome. Optional so tests
   * can mount the provider without it.
   */
  clientSettings?: UseSettingsReturn
  children: ReactNode
}

/**
 * Loads the daemon config once for the lifetime of an open overlay and exposes
 * it (plus the overlay dirty-guard registry) to the active section. Wrapping
 * `saveConfig` refetches on success so a re-entered section reflects what was
 * just written.
 */
export function SettingsSectionProvider({
  registerDirtyGuard = noopRegister,
  clientSettings,
  children,
}: SettingsSectionProviderProps) {
  const config = useConfiguration()
  const { fetchConfig, saveConfig: persistConfig } = config

  useEffect(() => {
    void fetchConfig()
  }, [fetchConfig])

  const saveConfig = useCallback(
    async (values: Record<string, unknown>): Promise<SaveConfigResult> => {
      const result = await persistConfig(values)
      if (result.ok) await fetchConfig()
      return result
    },
    [persistConfig, fetchConfig],
  )

  const value = useMemo<SettingsSectionContextValue>(
    () => ({
      schema: config.schema,
      configValues: config.configValues,
      secretKeys: config.secretKeys,
      isLoading: config.isLoading,
      saveConfig,
      registerDirtyGuard,
      clientSettings,
    }),
    [
      config.schema,
      config.configValues,
      config.secretKeys,
      config.isLoading,
      saveConfig,
      registerDirtyGuard,
      clientSettings,
    ],
  )

  return (
    <SettingsSectionContext.Provider value={value}>
      {children}
    </SettingsSectionContext.Provider>
  )
}
