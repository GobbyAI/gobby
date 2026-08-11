import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { SettingsSectionProvider } from '../SettingsSectionProvider'
import { useSettingsSectionContext } from '../SettingsSectionContext'

const config = vi.hoisted(() => ({
  schema: null,
  configValues: {},
  activeConfigValues: {},
  secretKeys: [],
  pendingRestartKeys: [],
  failedLiveKeys: {},
  mutationError: null,
  isLoading: false,
  fetchConfig: vi.fn(async () => undefined),
  saveConfig: vi.fn(async () => ({ ok: true })),
  fetchSecrets: vi.fn(async () => undefined),
  fetchPrompts: vi.fn(async () => undefined),
  fetchTemplate: vi.fn(async () => undefined),
  importConfig: vi.fn(async () => ({ success: true })),
}))

vi.mock('../../../../hooks/useConfiguration', () => ({
  useConfiguration: () => config,
}))

function SaveHarness() {
  const { saveConfig } = useSettingsSectionContext()
  return (
    <button type="button" onClick={() => void saveConfig({ server: { port: 60887 } })}>
      Save harness
    </button>
  )
}

describe('SettingsSectionProvider', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('does_not_issue_a_second_config_fetch_after_a_successful_save', async () => {
    render(
      <SettingsSectionProvider>
        <SaveHarness />
      </SettingsSectionProvider>,
    )
    await waitFor(() => expect(config.fetchConfig).toHaveBeenCalledTimes(1))
    config.fetchConfig.mockClear()

    fireEvent.click(screen.getByRole('button', { name: 'Save harness' }))

    await waitFor(() =>
      expect(config.saveConfig).toHaveBeenCalledWith({ server: { port: 60887 } }),
    )
    expect(config.fetchConfig).not.toHaveBeenCalled()
  })
})
