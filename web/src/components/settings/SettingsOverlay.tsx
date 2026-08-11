import { createElement, useEffect, useId, useRef, useState } from 'react'
import type { KeyboardEvent as ReactKeyboardEvent } from 'react'
import { ChevronDownIcon, CloseIcon, LogoutIcon } from '../icons'
import { cn } from '../../lib/utils'
import { Button } from '../ui/Button'
import { coarseHitAreaCls } from '../ui/controlStyles'
import { useIsMobile } from '../../hooks/useIsMobile'
import { ActivityFilterDropdown } from '../activity/ActivityFilterDropdown'
import {
  getSettingsSection,
  SETTINGS_SECTIONS,
  type SettingsSectionId,
} from './sections'
import { getSettingsSectionComponent } from './sections/index'
import { SettingsSectionProvider } from './sections/SettingsSectionProvider'
import type {
  ProjectSelectionContextValue,
  ProviderSelectionContextValue,
  SettingsSectionContextValue,
} from './sections/SettingsSectionContext'
import type { UseSettingsReturn } from '../../hooks/useSettings'

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
  /**
   * The app-wide `useSettings` instance from App. Forwarded to the section
   * provider so client-side sections (Appearance, etc.) drive the same state
   * the chrome reads — changes stay live without a reload. Optional so tests
   * can mount the shell standalone.
   */
  clientSettings?: UseSettingsReturn
  /**
   * App-owned default-provider selection, forwarded to the section provider so
   * the Providers & Models section drives the same `selectedProvider` state as
   * the chat picker. Optional so tests can mount the shell standalone.
   */
  providerSelection?: ProviderSelectionContextValue
  /**
   * App-owned active-project selection, forwarded to the section provider so
   * the Projects & Sessions section drives the same `selectedProjectId` state
   * as the chrome's ProjectSelector. Optional so tests can mount the shell
   * standalone.
   */
  projectSelection?: ProjectSelectionContextValue
  /**
   * Log-out handler for the mobile-tier header entry (the mobile app-header
   * pattern collapses the header's logout into this surface). Undefined when
   * auth is off or the visitor is not authenticated; the entry only renders
   * at the mobile tier.
   */
  onLogout?: () => void
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
  clientSettings,
  providerSelection,
  projectSelection,
  onLogout,
}: SettingsOverlayProps) {
  const dialogRef = useRef<HTMLDivElement>(null)
  const restoreFocusRef = useRef<HTMLElement | null>(null)
  const [sectionMenuOpen, setSectionMenuOpen] = useState(false)
  const headingId = useId()
  const isMobile = useIsMobile()

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
    <div className="fixed inset-0 z-[200] flex items-center justify-center p-8 [--control-row-height:var(--status-bar-control-height)] mobile:p-0">
      <Button
        type="button"
        variant="ghost"
        dense
        className="absolute inset-0 m-0 cursor-pointer bg-[var(--surface-scrim)] hover:bg-[var(--surface-scrim)] animate-settings-overlay-fade"
        data-testid="settings-overlay-backdrop"
        aria-label="Close settings"
        tabIndex={-1}
        onClick={onClose}
      />
      <div
        ref={dialogRef}
        className="relative flex h-[min(640px,100%)] max-h-full w-[min(720px,100%)] flex-col overflow-hidden rounded-xl border border-border bg-surface-secondary shadow-lg outline-none animate-settings-overlay-rise mobile:h-full mobile:w-full mobile:rounded-none mobile:border-0"
        role="dialog"
        aria-modal="true"
        aria-labelledby={headingId}
        tabIndex={-1}
        onKeyDown={handleKeyDown}
      >
        <header className="flex shrink-0 items-center justify-between gap-3 border-b border-border px-5 py-4">
          <h2 id={headingId} className="text-lg font-semibold leading-[1.2] text-foreground">
            Settings
          </h2>
          <div className="flex shrink-0 items-center gap-1">
            {isMobile && onLogout ? (
              <Button
                type="button"
                variant="ghost"
                size="icon"
                className="shrink-0"
                onClick={onLogout}
                aria-label="Log out"
                title="Log out"
              >
                <LogoutIcon />
              </Button>
            ) : null}
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className="shrink-0"
              onClick={onClose}
              aria-label="Close settings"
            >
              <CloseIcon />
            </Button>
          </div>
        </header>
        <div className="flex shrink-0 items-center border-b border-border px-5 py-3">
          <div className="relative inline-block mobile:w-full">
            <Button
              type="button"
              variant="ghost"
              size="sm"
              dense
              className={cn(
                'min-w-56 cursor-pointer py-1.5 [font-family:inherit] [&_svg]:shrink-0 [&_svg]:text-[var(--text-muted)] aria-expanded:bg-[var(--accent-tint)] aria-expanded:text-accent aria-expanded:[&_svg]:text-accent mobile:w-full',
                coarseHitAreaCls,
              )}
              aria-haspopup="listbox"
              aria-expanded={sectionMenuOpen}
              onClick={() => setSectionMenuOpen((open) => !open)}
            >
              <span className="overflow-hidden text-left text-ellipsis whitespace-nowrap">
                {section.label}
              </span>
              <ChevronDownIcon />
            </Button>
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
        <SettingsSectionProvider
          registerDirtyGuard={registerDirtyGuard}
          clientSettings={clientSettings}
          providerSelection={providerSelection}
          projectSelection={projectSelection}
        >
          <div className="flex min-h-0 flex-1 flex-col">
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
