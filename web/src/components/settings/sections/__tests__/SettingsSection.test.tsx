import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { SettingsSection } from '../SettingsSection'
import { SettingsSectionContext } from '../SettingsSectionContext'
import { Subsection, TextConfigField } from '../configFields'

afterEach(cleanup)

describe('SettingsSection renderers', () => {
  it('renders the section and subsection shells with a config field row', () => {
    const saveConfig = vi.fn(async () => ({ ok: true }))
    const registerDirtyGuard = vi.fn(() => () => {})
    const { container } = render(
      <SettingsSectionContext.Provider
        value={{
          schema: null,
          configValues: { server: { host: '127.0.0.1' } },
          secretKeys: [],
          isLoading: false,
          saveConfig,
          registerDirtyGuard,
        }}
      >
        <SettingsSection
          sectionId="runtime-infrastructure"
          ownedPaths={['server.host']}
        >
          {(fields) => (
            <Subsection title="Server" hint="Local daemon binding.">
              <TextConfigField
                fields={fields}
                path="server.host"
                label="Host"
                ariaLabel="Server host"
              />
            </Subsection>
          )}
        </SettingsSection>
      </SettingsSectionContext.Provider>,
    )

    const sectionShell = container.firstElementChild
    expect(sectionShell).toHaveClass('flex', 'min-h-0', 'flex-1', 'flex-col')
    expect(screen.getByRole('heading', { level: 3 })).toHaveClass(
      'text-lg',
      'font-semibold',
      'leading-[1.2]',
    )

    const subsection = screen.getByRole('heading', { name: 'Server' }).closest('section')
    expect(subsection).toHaveClass('flex', 'flex-col', 'gap-4')
    expect(screen.getByText('Local daemon binding.')).toHaveClass(
      'max-w-[48ch]',
      'leading-[1.4]',
    )

    const input = screen.getByRole('textbox', { name: 'Server host' })
    expect(input).toHaveValue('127.0.0.1')
    expect(input).toHaveClass('h-9', 'w-full', 'rounded-md')
    expect(input.parentElement).toHaveClass('inline-flex', 'w-full')

    fireEvent.change(input, { target: { value: '0.0.0.0' } })
    expect(input).toHaveValue('0.0.0.0')
    expect(screen.getByRole('contentinfo')).toHaveClass(
      'shrink-0',
      'border-t',
      'bg-surface-secondary',
    )
  })

  it('fails_closed_when_schema_activation_is_unavailable', async () => {
    const saveConfig = vi.fn(async () => ({ ok: true }))
    render(
      <SettingsSectionContext.Provider
        value={{
          schema: null,
          configValues: { server: { host: '127.0.0.1' } },
          secretKeys: [],
          isLoading: false,
          saveConfig,
          registerDirtyGuard: () => () => {},
        }}
      >
        <SettingsSection sectionId="runtime-infrastructure" ownedPaths={['server.host']}>
          {(fields) => (
            <TextConfigField
              fields={fields}
              path="server.host"
              label="Host"
              ariaLabel="Server host"
            />
          )}
        </SettingsSection>
      </SettingsSectionContext.Provider>,
    )

    fireEvent.change(screen.getByRole('textbox', { name: 'Server host' }), {
      target: { value: '0.0.0.0' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(saveConfig).toHaveBeenCalledWith({}))
  })
})
