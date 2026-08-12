import { useCallback, useRef, useState } from "react";
import { DEFAULT_SETTINGS_SECTION, type SettingsSectionId } from "./sections";

/** Predicate a section registers so the overlay can guard navigation away. */
export type DirtyGuard = () => boolean;

export interface SettingsOverlayController {
  /** Whether the overlay is mounted/visible. */
  readonly isOpen: boolean;
  /** The currently selected section. */
  readonly activeSection: SettingsSectionId;
  /** Open the overlay, optionally jumping straight to a section. */
  open: (section?: SettingsSectionId) => void;
  /** Close the overlay, routed through the active section's dirty guard. */
  close: () => void;
  /** Switch sections, routed through the leaving section's dirty guard. */
  selectSection: (section: SettingsSectionId) => void;
  /**
   * Register a predicate reporting whether `section` has unsaved edits.
   * Returns an unregister fn. Used by section components (#17046) so leaving a
   * dirty section prompts once before discarding.
   */
  registerDirtyGuard: (
    section: SettingsSectionId,
    isDirty: DirtyGuard,
  ) => () => void;
}

export interface UseSettingsOverlayOptions {
  /**
   * Confirm prompt shown when leaving a dirty section. Injectable for tests;
   * defaults to `window.confirm`.
   */
  confirmDiscard?: (message: string) => boolean;
}

const DISCARD_MESSAGE = "Discard unsaved changes in this section?";

/**
 * Owns the settings overlay's open/close state, active-section routing, and the
 * dirty-section guard registry. A single confirm path gates both section
 * changes and close so a section with unsaved edits can never be left silently.
 */
export function useSettingsOverlay(
  options: UseSettingsOverlayOptions = {},
): SettingsOverlayController {
  const { confirmDiscard } = options;
  const [isOpen, setIsOpen] = useState(false);
  const [activeSection, setActiveSection] = useState<SettingsSectionId>(
    DEFAULT_SETTINGS_SECTION,
  );
  // Each section can hold more than one dirty guard (e.g. the section shell
  // plus a test harness), so guards are stored per-section as a Set and the
  // section counts as dirty if any guard reports dirty.
  const dirtyGuards = useRef(new Map<SettingsSectionId, Set<DirtyGuard>>());

  const guardAllows = useCallback(
    (section: SettingsSectionId): boolean => {
      const guards = dirtyGuards.current.get(section);
      const anyDirty = guards
        ? Array.from(guards).some((isDirty) => isDirty())
        : false;
      if (!anyDirty) return true;
      const ask =
        confirmDiscard ?? ((message: string) => window.confirm(message));
      return ask(DISCARD_MESSAGE);
    },
    [confirmDiscard],
  );

  const open = useCallback((section?: SettingsSectionId) => {
    if (section) setActiveSection(section);
    setIsOpen(true);
  }, []);

  const close = useCallback(() => {
    if (!guardAllows(activeSection)) return;
    setIsOpen(false);
  }, [activeSection, guardAllows]);

  const selectSection = useCallback(
    (section: SettingsSectionId) => {
      if (section === activeSection) return;
      if (!guardAllows(activeSection)) return;
      setActiveSection(section);
    },
    [activeSection, guardAllows],
  );

  const registerDirtyGuard = useCallback(
    (section: SettingsSectionId, isDirty: DirtyGuard) => {
      let guards = dirtyGuards.current.get(section);
      if (!guards) {
        guards = new Set();
        dirtyGuards.current.set(section, guards);
      }
      guards.add(isDirty);
      return () => {
        const current = dirtyGuards.current.get(section);
        if (!current) return;
        current.delete(isDirty);
        if (current.size === 0) dirtyGuards.current.delete(section);
      };
    },
    [],
  );

  return {
    isOpen,
    activeSection,
    open,
    close,
    selectSection,
    registerDirtyGuard,
  };
}
