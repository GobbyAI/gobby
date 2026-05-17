import { useEffect, useState } from "react";
import type { TasksViewMode } from "./TasksTabToolbar";

const STORAGE_KEY_VIEW_MODE = "gobby-tasks-view-mode";

/**
 * Resolve the persisted *desktop* Tasks view mode. Only an explicit `"board"`
 * is honored; anything else (missing key, legacy junk, storage throwing)
 * falls back to `"list"`.
 */
export function loadTasksViewMode(): TasksViewMode {
  try {
    return localStorage.getItem(STORAGE_KEY_VIEW_MODE) === "board"
      ? "board"
      : "list";
  } catch {
    return "list";
  }
}

/**
 * The List/Board switcher is a desktop affordance. The Board is a wide,
 * pointer-first kanban (fixed-width stage columns, drag-between-stages); it
 * does not survive a phone viewport, so on mobile the List is the sole task
 * surface.
 *
 * Mirrors the `useActivityPanel(isMobile)` invariant: `viewMode` is the
 * persisted desktop preference; `effectiveViewMode` is forced to `"list"`
 * while mobile. Mobile never writes the desktop key, so a desktop `"board"`
 * preference is untouched by a mobile round-trip and returns verbatim when
 * the viewport widens back. The caller owns `useIsMobile()` and passes the
 * boolean in (same wiring as `ChatPage` → `useActivityPanel`), which keeps
 * this hook trivially testable without a `matchMedia` stub.
 */
export function useEffectiveTasksViewMode(isMobile: boolean) {
  const [viewMode, setViewMode] = useState<TasksViewMode>(loadTasksViewMode);

  useEffect(() => {
    // Mobile derives its view; it must never clobber the desktop preference.
    if (isMobile) return;
    try {
      localStorage.setItem(STORAGE_KEY_VIEW_MODE, viewMode);
    } catch {
      /* ignore */
    }
  }, [isMobile, viewMode]);

  const effectiveViewMode: TasksViewMode = isMobile ? "list" : viewMode;

  return { viewMode, setViewMode, effectiveViewMode };
}
