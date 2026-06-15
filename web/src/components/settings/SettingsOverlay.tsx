import { createElement, useEffect, useId, useRef, useState } from 'react'
import type { KeyboardEvent as ReactKeyboardEvent } from 'react'
import { ChevronDownIcon, CloseIcon } from '../icons'
import { ActivityFilterDropdown } from '../activity/ActivityFilterDropdown'
import {
  getSettingsSection,
  SETTINGS_SECTIONS,
  type SettingsSectionId,
} from './sections'
import { getSettingsSectionComponent } from './sections/index'
import { SettingsSectionProvider } from './sections/SettingsSectionProvider'
import type { SettingsSectionContextValue } from './sections/SettingsSectionContext'

interface SettingsOverlayProps {
  isOpen: boolean
  activeSection: SettingsSectionId
  onClose: () => void
  onSelectSection: (section: SettingsSectionId) => void
  /**
   * The overlay controller's dirty-guard registrar (useSettingsOverlay). Passed
   * down so the active section can gate switching/closing while it has unsaved
   * edits. Optional so tests can mount the shell without the controller.
   */
  registerDirtyGuard?: SettingsSectionContextValue['registerDirtyGuard']
}

const SECTION_OPTIONS = SETTINGS_SECTIONS.map((section) => ({
  value: section.id,
  label: section.label,
}))

const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(', ')

function getFocusable(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR))
}

/**
 * Full-screen settings dialog rendered above the app shell. The chat surface
 * stays mounted underneath (no route change), so opening and closing preserves
 * chat state. Sections are chosen from a dropdown (the activity-panel
 * `ActivityFilterDropdown` style); the chosen section's content fills and
 * scrolls the body.
 *
 * Owns its own focus management: focus moves inside on open, is trapped while
 * open, and restores to the trigger (the header cog) on close. Escape closes
 * the section dropdown first if it is open, then the overlay — and only when an
 * inner editor or dropdown has not already consumed it (`defaultPrevented`).
 */
export function SettingsOverlay({
  isOpen,
  activeSection,
  onClose,
  onSelectSection,
  registerDirtyGuard,
}: SettingsOverlayProps) {
  const dialogRef = useRef<HTMLDivElement>(null)
  const restoreFocusRef = useRef<HTMLElement | null>(null)
  const [sectionMenuOpen, setSectionMenuOpen] = useState(false)
  const headingId = useId()

  useEffect(() => {
    if (!isOpen) return
    restoreFocusRef.current = document.activeElement as HTMLElement | null
    const dialog = dialogRef.current
    const focusables = dialog ? getFocusable(dialog) : []
    ;(focusables[0] ?? dialog)?.focus()
    return () => {
      restoreFocusRef.current?.focus?.()
    }
  }, [isOpen])

  if (!isOpen) return null

  const section = getSettingsSection(activeSection)

  const handleKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Escape') {
      // Inner CodeMirror instances and open dropdowns call preventDefault on
      // their own Escape handling; let them win.
      if (event.defaultPrevented) return
      // The section dropdown closes first so one Escape doesn't tear down the
      // whole overlay while a menu is open.
      if (sectionMenuOpen) {
        event.stopPropagation()
        setSectionMenuOpen(false)
        return
      }
      event.stopPropagation()
      onClose()
      return
    }
    if (event.key !== 'Tab') return
    const dialog = dialogRef.current
    if (!dialog) return
    const focusables = getFocusable(dialog)
    if (focusables.length === 0) return
    const first = focusables[0]
    const last = focusables[focusables.length - 1]
    const active = document.activeElement
    if (event.shiftKey && active === first) {
      event.preventDefault()
      last.focus()
    } else if (!event.shiftKey && active === last) {
      event.preventDefault()
      first.focus()
    }
  }

  return (
    <div className="settings-overlay-shell">
      <button
        type="button"
        className="settings-overlay-shell__backdrop"
        aria-label="Close settings"
        tabIndex={-1}
        onClick={onClose}
      />
      <div
        ref={dialogRef}
        className="settings-overlay-shell__dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={headingId}
        tabIndex={-1}
        onKeyDown={handleKeyDown}
      >
        <header className="settings-overlay-shell__header">
          <h2 id={headingId} className="settings-overlay-shell__title">
            Settings
          </h2>
          <button
            type="button"
            className="btn btn-ghost btn-icon btn-sm settings-overlay-shell__close"
            onClick={onClose}
            aria-label="Close settings"
          >
            <CloseIcon />
          </button>
        </header>
        <div className="settings-overlay-shell__toolbar">
          <div className="settings-overlay-shell__section-select">
            <button
              type="button"
              className="settings-overlay-shell__section-trigger"
              aria-haspopup="listbox"
              aria-expanded={sectionMenuOpen}
              onClick={() => setSectionMenuOpen((open) => !open)}
            >
              <span className="settings-overlay-shell__section-trigger-label">
                {section.label}
              </span>
              <ChevronDownIcon />
            </button>
            {sectionMenuOpen ? (
              <ActivityFilterDropdown<SettingsSectionId>
                value={activeSection}
                options={SECTION_OPTIONS}
                onChange={onSelectSection}
                onClose={() => setSectionMenuOpen(false)}
                ariaLabel="Settings sections"
              />
            ) : null}
          </div>
        </div>
        <SettingsSectionProvider registerDirtyGuard={registerDirtyGuard}>
          <div className="settings-overlay-shell__body">
            {/* Remount on section change so each section starts from a fresh
                draft of its owned config paths. */}
            {createElement(getSettingsSectionComponent(activeSection), {
              key: activeSection,
            })}
          </div>
        </SettingsSectionProvider>
      </div>
    </div>
  )
}

export default SettingsOverlay
