import { useEffect } from 'react'
import {
  cleanup,
  createEvent,
  fireEvent,
  render,
  screen,
  within,
} from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { SettingsOverlay } from '../SettingsOverlay'
import { useSettingsOverlay } from '../useSettingsOverlay'
import {
  DEFAULT_SETTINGS_SECTION,
  SETTINGS_SECTIONS,
  type SettingsSectionId,
} from '../sections'

interface HarnessProps {
  confirmDiscard?: (message: string) => boolean
  dirtySections?: readonly SettingsSectionId[]
}

const NO_DIRTY: readonly SettingsSectionId[] = []

/**
 * Mirrors the App.tsx wiring: a trigger button + the lazily-mounted overlay
 * driven by useSettingsOverlay. A sibling "chat" input with local state stands
 * in for the chat surface so we can assert it survives open/close (13.1.1).
 */
function Harness({ confirmDiscard, dirtySections = NO_DIRTY }: HarnessProps) {
  const overlay = useSettingsOverlay(confirmDiscard ? { confirmDiscard } : {})

  useEffect(() => {
    const unregister = dirtySections.map((section) =>
      overlay.registerDirtyGuard(section, () => true),
    )
    return () => unregister.forEach((fn) => fn())
  }, [overlay, dirtySections])

  return (
    <div>
      <input aria-label="chat draft" defaultValue="" />
      <button type="button" onClick={() => overlay.open()}>
        Open settings
      </button>
      {overlay.isOpen ? (
        <SettingsOverlay
          isOpen={overlay.isOpen}
          activeSection={overlay.activeSection}
          onClose={overlay.close}
          onSelectSection={overlay.selectSection}
        />
      ) : null}
    </div>
  )
}

function openOverlay(): HTMLElement {
  fireEvent.click(screen.getByRole('button', { name: 'Open settings' }))
  return screen.getByRole('dialog')
}

function openSectionMenu(dialog: HTMLElement): void {
  const trigger = dialog.querySelector<HTMLElement>('[aria-haspopup="listbox"]')
  if (!trigger) throw new Error('section trigger not found')
  fireEvent.click(trigger)
}

function selectSection(dialog: HTMLElement, label: string): void {
  openSectionMenu(dialog)
  fireEvent.click(within(dialog).getByRole('option', { name: label }))
}

afterEach(() => {
  cleanup()
})

describe('SettingsOverlay', () => {
  it('does not render dialog content while closed', () => {
    render(<Harness />)
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('opens as a labelled modal dialog and moves focus inside it', () => {
    render(<Harness />)
    const dialog = openOverlay()

    expect(dialog).toHaveAttribute('aria-modal', 'true')
    expect(dialog).toHaveAccessibleName('Settings')
    expect(dialog.contains(document.activeElement)).toBe(true)
  })

  it('traps Tab focus within the dialog', () => {
    render(<Harness />)
    const dialog = openOverlay()
    const focusables = Array.from(
      dialog.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      ),
    )
    const first = focusables[0]
    const last = focusables[focusables.length - 1]
    expect(first).toBeDefined()
    expect(last).toBeDefined()

    last.focus()
    fireEvent.keyDown(last, { key: 'Tab' })
    expect(document.activeElement).toBe(first)

    first.focus()
    fireEvent.keyDown(first, { key: 'Tab', shiftKey: true })
    expect(document.activeElement).toBe(last)
  })

  it('closes on Escape and restores focus to the trigger', () => {
    render(<Harness />)
    const trigger = screen.getByRole('button', { name: 'Open settings' })
    trigger.focus()

    const dialog = openOverlay()
    fireEvent.keyDown(dialog, { key: 'Escape' })

    expect(screen.queryByRole('dialog')).toBeNull()
    expect(document.activeElement).toBe(trigger)
  })

  it('ignores Escape already consumed by an inner control (defaultPrevented)', () => {
    render(<Harness />)
    const dialog = openOverlay()

    const consumed = createEvent.keyDown(dialog, { key: 'Escape' })
    consumed.preventDefault()
    fireEvent(dialog, consumed)
    expect(screen.queryByRole('dialog')).not.toBeNull()

    fireEvent.keyDown(dialog, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('closes when the backdrop is clicked', () => {
    const { container } = render(<Harness />)
    openOverlay()
    const backdrop = container.querySelector('.settings-overlay-shell__backdrop')
    expect(backdrop).not.toBeNull()
    fireEvent.click(backdrop as Element)
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('preserves sibling chat state across open and close', () => {
    render(<Harness />)
    const chat = screen.getByLabelText('chat draft') as HTMLInputElement
    fireEvent.change(chat, { target: { value: 'unsent message' } })

    const dialog = openOverlay()
    fireEvent.keyDown(dialog, { key: 'Escape' })

    expect((screen.getByLabelText('chat draft') as HTMLInputElement).value).toBe(
      'unsent message',
    )
  })
})

describe('SettingsOverlay section dropdown', () => {
  it('lists every audit-IA section in the dropdown with the active one selected', () => {
    render(<Harness />)
    const dialog = openOverlay()
    const trigger = dialog.querySelector<HTMLElement>('[aria-haspopup="listbox"]')
    expect(trigger).not.toBeNull()
    expect(trigger).toHaveAttribute('aria-expanded', 'false')

    const defaultSection = SETTINGS_SECTIONS.find(
      (section) => section.id === DEFAULT_SETTINGS_SECTION,
    )
    expect(trigger?.textContent).toContain(defaultSection?.label)

    openSectionMenu(dialog)
    expect(trigger).toHaveAttribute('aria-expanded', 'true')

    const listbox = within(dialog).getByRole('listbox', { name: 'Settings sections' })
    const options = within(listbox).getAllByRole('option')
    expect(options).toHaveLength(SETTINGS_SECTIONS.length)

    const selected = options.find(
      (option) => option.getAttribute('aria-selected') === 'true',
    )
    expect(selected?.textContent).toBe(defaultSection?.label)
  })

  it('switches the content pane when a section is selected', () => {
    render(<Harness />)
    const dialog = openOverlay()
    selectSection(dialog, 'Observability')
    expect(
      within(dialog).getByRole('heading', { name: 'Observability' }),
    ).not.toBeNull()
  })
})

describe('useSettingsOverlay dirty-section guard', () => {
  it('blocks leaving a dirty section when discard is declined', () => {
    const confirmDiscard = vi.fn(() => false)
    render(
      <Harness confirmDiscard={confirmDiscard} dirtySections={[DEFAULT_SETTINGS_SECTION]} />,
    )
    const dialog = openOverlay()
    selectSection(dialog, 'Observability')

    expect(confirmDiscard).toHaveBeenCalledTimes(1)
    const defaultSection = SETTINGS_SECTIONS.find(
      (section) => section.id === DEFAULT_SETTINGS_SECTION,
    )
    expect(
      within(dialog).getByRole('heading', { name: defaultSection!.label }),
    ).not.toBeNull()
  })

  it('allows leaving a dirty section when discard is confirmed', () => {
    const confirmDiscard = vi.fn(() => true)
    render(
      <Harness confirmDiscard={confirmDiscard} dirtySections={[DEFAULT_SETTINGS_SECTION]} />,
    )
    const dialog = openOverlay()
    selectSection(dialog, 'Observability')

    expect(confirmDiscard).toHaveBeenCalledTimes(1)
    expect(
      within(dialog).getByRole('heading', { name: 'Observability' }),
    ).not.toBeNull()
  })
})
