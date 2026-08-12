import { useCallback, useLayoutEffect, useRef, useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent } from "react";

/**
 * A single visible row in a flattened ARIA tree, in DOM order. `depth` is
 * 0-based; `isExpandable` marks parent rows (tasks: has children; MCP: a server
 * row); `isExpanded` is only meaningful when `isExpandable` is true.
 */
export interface TreeNavItem {
  id: string;
  depth: number;
  isExpandable: boolean;
  isExpanded: boolean;
}

type NavKey =
  "ArrowDown" | "ArrowUp" | "ArrowLeft" | "ArrowRight" | "Home" | "End";

interface UseTreeKeyboardNavigationOptions {
  /** Flattened visible rows, in DOM order. */
  items: TreeNavItem[];
  /** The host-owned selection, or null when nothing is selected. */
  selectedId: string | null;
  onSelect: (id: string) => void;
  onToggle: (id: string) => void;
  /** Ensure a virtualized row is mounted before DOM focus is applied. */
  onFocusRequest?: (id: string) => void;
  /**
   * When true (default), arrow navigation also selects the row it lands on
   * (selection-follows-focus, used by the Tasks tree). Set false when selecting
   * a row is expensive (e.g. the MCP tree fires a schema fetch on select) so
   * arrows move focus only and Enter/Space activates.
   */
  selectionFollowsFocus?: boolean;
}

export interface TreeKeyboardNavigation {
  setRowRef: (id: string, node: HTMLElement | null) => void;
  handleKeyDown: (id: string, event: ReactKeyboardEvent) => void;
  getTabIndex: (id: string) => 0 | -1;
}

/**
 * Shared keyboard + roving-focus behavior for the activity-panel ARIA trees
 * (Tasks and MCP). Owns the roving-tabindex anchor and DOM focus management;
 * the caller owns selection and the flattened row model.
 *
 * Keyboard model: ArrowUp/Down move between visible rows; ArrowRight expands a
 * collapsed parent or steps into its first child; ArrowLeft collapses an open
 * parent or steps to the parent row; Home/End jump to the first/last visible
 * row; Enter/Space activate (select) the row.
 */
export function useTreeKeyboardNavigation({
  items,
  selectedId,
  onSelect,
  onToggle,
  onFocusRequest,
  selectionFollowsFocus = true,
}: UseTreeKeyboardNavigationOptions): TreeKeyboardNavigation {
  const rowRefs = useRef<Map<string, HTMLElement>>(new Map());
  const pendingFocusRef = useRef<string | null>(null);
  // Roving anchor. Kept as state so tabIndex re-resolves when focus moves via
  // arrows without a selection change (the selectionFollowsFocus=false case).
  const [focusedId, setFocusedId] = useState<string | null>(selectedId);

  // Keep the anchor aligned with external selection (mouse click, host reset)
  // via the "adjust state during render" pattern — no effect, so there's no
  // extra commit/paint just to sync focus.
  const [prevSelectedId, setPrevSelectedId] = useState<string | null>(
    selectedId,
  );
  if (selectedId !== prevSelectedId) {
    setPrevSelectedId(selectedId);
    setFocusedId(selectedId);
  }

  const setRowRef = useCallback((id: string, node: HTMLElement | null) => {
    if (node) {
      rowRefs.current.set(id, node);
      if (pendingFocusRef.current === id) {
        pendingFocusRef.current = null;
        node.focus();
      }
    } else {
      rowRefs.current.delete(id);
    }
  }, []);

  // Keyboard-initiated focus moves land in a layout effect after the commit
  // (not requestAnimationFrame, which browsers suspend in occluded windows —
  // queued focus moves would then fire all at once on the next paint). Only
  // focusRow arms this; external selection sync never steals DOM focus.
  const [focusSeq, setFocusSeq] = useState(0);
  const focusRow = useCallback(
    (id: string) => {
      setFocusedId(id);
      pendingFocusRef.current = id;
      onFocusRequest?.(id);
      setFocusSeq((seq) => seq + 1);
    },
    [onFocusRequest],
  );

  useLayoutEffect(() => {
    if (focusSeq === 0) return;
    const id = pendingFocusRef.current;
    if (id === null) return;
    const node = rowRefs.current.get(id);
    if (!node) return;
    pendingFocusRef.current = null;
    node.focus();
  }, [focusSeq]);

  // Move focus to another row, also selecting it when selection follows focus.
  const moveTo = useCallback(
    (id: string) => {
      if (selectionFollowsFocus) onSelect(id);
      focusRow(id);
    },
    [focusRow, onSelect, selectionFollowsFocus],
  );

  const navigate = useCallback(
    (id: string, key: NavKey) => {
      const index = items.findIndex((item) => item.id === id);
      if (index === -1) return;
      const item = items[index];

      if (key === "Home") {
        const first = items[0];
        if (first && first.id !== id) moveTo(first.id);
        return;
      }

      if (key === "End") {
        const last = items[items.length - 1];
        if (last && last.id !== id) moveTo(last.id);
        return;
      }

      if (key === "ArrowDown") {
        const next = items[index + 1];
        if (next) moveTo(next.id);
        return;
      }

      if (key === "ArrowUp") {
        const previous = items[index - 1];
        if (previous) moveTo(previous.id);
        return;
      }

      if (key === "ArrowRight") {
        if (item.isExpandable && !item.isExpanded) {
          onToggle(id);
          focusRow(id);
          return;
        }
        const child = items[index + 1];
        if (
          item.isExpandable &&
          item.isExpanded &&
          child &&
          child.depth > item.depth
        ) {
          moveTo(child.id);
        }
        return;
      }

      // ArrowLeft
      if (item.isExpandable && item.isExpanded) {
        onToggle(id);
        focusRow(id);
        return;
      }
      const parent = items
        .slice(0, index)
        .reverse()
        .find((candidate) => candidate.depth === item.depth - 1);
      if (parent) moveTo(parent.id);
    },
    [focusRow, items, moveTo, onToggle],
  );

  const handleKeyDown = useCallback(
    (id: string, event: ReactKeyboardEvent) => {
      // Ignore events that bubbled up from nested controls (chevron/kebab):
      // otherwise the row handler swallows Enter/Space meant for those buttons.
      if (event.target !== event.currentTarget) return;

      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        setFocusedId(id);
        onSelect(id);
        return;
      }
      if (
        event.key === "ArrowDown" ||
        event.key === "ArrowUp" ||
        event.key === "ArrowLeft" ||
        event.key === "ArrowRight" ||
        event.key === "Home" ||
        event.key === "End"
      ) {
        event.preventDefault();
        navigate(id, event.key);
      }
    },
    [navigate, onSelect],
  );

  const getTabIndex = useCallback(
    (id: string): 0 | -1 => {
      const anchorVisible =
        focusedId !== null && items.some((item) => item.id === focusedId);
      if (anchorVisible) return id === focusedId ? 0 : -1;
      // No resolvable anchor: the first row is the tree's tab entry point so
      // keyboard users can always Tab in, even with nothing selected.
      return items.length > 0 && items[0]?.id === id ? 0 : -1;
    },
    [focusedId, items],
  );

  return { setRowRef, handleKeyDown, getTabIndex };
}
