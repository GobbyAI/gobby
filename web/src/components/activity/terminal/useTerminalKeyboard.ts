import {
  type RefObject,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";

import type { TerminalViewHandle } from "./TerminalView";

// With the keyboard up, a phone on its side has about one terminal row left.
const PHONE_LANDSCAPE_QUERY =
  "(orientation: landscape) and (max-height: 500px)";

function phoneLandscape(): MediaQueryList | null {
  return typeof window.matchMedia === "function"
    ? window.matchMedia(PHONE_LANDSCAPE_QUERY)
    : null;
}

interface TerminalKeyboardOptions {
  rootRef: RefObject<HTMLElement | null>;
  viewRef: RefObject<TerminalViewHandle | null>;
  expanded: boolean;
  onExpandedChange: (expanded: boolean) => void;
}

export interface TerminalKeyboard {
  open: boolean;
  /** The keyboard was asked for in phone landscape: ask for portrait instead. */
  rotatePrompt: boolean;
  /**
   * Tab height that ends where the on-screen keyboard begins, or null while
   * it is down. iOS lays the keyboard over the page instead of shrinking it,
   * so without this the last terminal rows and the keys sit underneath.
   */
  height: number | null;
  toggle: () => void;
  close: () => void;
}

/**
 * The on-demand soft keyboard for coarse pointers. Raising it expands the
 * terminal so the keyboard takes its room from the session list and the tabs,
 * not from the terminal rows; lowering it restores the layout it rose from.
 */
export function useTerminalKeyboard({
  rootRef,
  viewRef,
  expanded,
  onExpandedChange,
}: TerminalKeyboardOptions): TerminalKeyboard {
  const [open, setOpen] = useState(false);
  const [rotatePrompt, setRotatePrompt] = useState(false);
  const [height, setHeight] = useState<number | null>(null);
  const expandedBeforeRef = useRef(false);

  const measure = useCallback((): number | null => {
    const viewport = window.visualViewport;
    const root = rootRef.current;
    if (!viewport || !root) return null;
    return Math.max(
      0,
      viewport.offsetTop + viewport.height - root.getBoundingClientRect().top,
    );
  }, [rootRef]);

  const close = useCallback(() => {
    setRotatePrompt(false);
    if (!open) return;
    setOpen(false);
    viewRef.current?.setKeyboardOpen(false);
    onExpandedChange(expandedBeforeRef.current);
  }, [onExpandedChange, open, viewRef]);

  const toggle = useCallback(() => {
    if (open) {
      close();
      return;
    }
    if (phoneLandscape()?.matches) {
      setRotatePrompt((shown) => !shown);
      return;
    }
    expandedBeforeRef.current = expanded;
    setOpen(true);
    setHeight(measure());
    viewRef.current?.setKeyboardOpen(true);
    onExpandedChange(true);
  }, [close, expanded, measure, onExpandedChange, open, viewRef]);

  useEffect(() => {
    const query = phoneLandscape();
    if (!query) return;
    const rotate = (event: MediaQueryListEvent) => {
      if (!event.matches) {
        setRotatePrompt(false);
      } else if (open) {
        close();
        setRotatePrompt(true);
      }
    };
    query.addEventListener("change", rotate);
    return () => query.removeEventListener("change", rotate);
  }, [close, open]);

  useEffect(() => {
    const viewport = window.visualViewport;
    if (!open || !viewport) return;
    const sync = () => setHeight(measure());
    viewport.addEventListener("resize", sync);
    viewport.addEventListener("scroll", sync);
    return () => {
      viewport.removeEventListener("resize", sync);
      viewport.removeEventListener("scroll", sync);
    };
  }, [measure, open]);

  return { open, rotatePrompt, height: open ? height : null, toggle, close };
}
