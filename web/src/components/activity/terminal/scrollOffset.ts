/**
 * Scrolling a native (gterm-backed) terminal.
 *
 * Native rendering is frame based: every frame is a screen snapshot, so the
 * renderer accumulates no history of its own and there is nothing in the DOM
 * to scroll. The scrollback lives in the host, and the only way back into it
 * is to ask the daemon to re-render from an offset — `terminal_set_scroll_offset`,
 * answered by `terminal_scroll_offset_applied`.
 *
 * Tmux attachments are the other half of the contract: their wheel becomes SGR
 * mouse reports written to the tmux attach client, so they must never send a
 * scroll-offset message. `useTerminalScrollOffset` reports `rows: null` for
 * them, which is what tells the pane to leave the wheel alone.
 */
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type MutableRefObject,
} from "react";

import type { TerminalScrollApplied } from "../../../hooks/terminalOutputSink";

/**
 * One wheel notch in line mode, matching gclient's `MOUSE_SCROLL_LINES`. Pixel
 * mode divides by the measured row height instead, so a trackpad pans a row
 * per row rather than in notches.
 */
const WHEEL_SCROLL_ROWS = 3;

interface ScrollOffsetState {
  /** Rows the rendered window sits back from the live edge. */
  rows: number;
  /**
   * The deepest offset the daemon has confirmed. Zero means the ceiling is
   * still unknown (nothing applied yet) or the host holds no scrollback at
   * all; either way the right move is to ask and let the reply decide.
   */
  maxRows: number;
}

const LIVE_EDGE: ScrollOffsetState = { rows: 0, maxRows: 0 };

/** Clamp a requested offset against the last ceiling the daemon confirmed. */
function clampScrollRows(state: ScrollOffsetState, requested: number): number {
  const rows = Math.max(0, Math.trunc(requested));
  return state.maxRows > 0 ? Math.min(rows, state.maxRows) : rows;
}

/**
 * A wheel or touch delta in rows away from the live edge: positive scrolls
 * back into history, which is the direction the wire counts in.
 */
function wheelScrollRows(
  event: { deltaY: number; deltaMode: number },
  rowHeight: number,
  pageRows: number,
): number {
  if (event.deltaMode === 1) return -event.deltaY * WHEEL_SCROLL_ROWS;
  if (event.deltaMode === 2) return -event.deltaY * Math.max(pageRows, 1);
  return -event.deltaY / Math.max(rowHeight, 1);
}

/**
 * Whole rows out of a fractional stream. Trackpads deliver deltas of a few
 * pixels at a time, well under one row, so truncating each event on its own
 * would leave the pane dead under a slow pan; the remainder carries over.
 */
function createRowAccumulator(): (rows: number) => number {
  let carry = 0;
  return (rows: number) => {
    carry += rows;
    const whole = Math.trunc(carry);
    carry -= whole;
    return whole;
  };
}

export interface TerminalScrollOffset {
  /**
   * Rows back from the live edge, or `null` when the daemon owns no scrollback
   * for this attachment — no native attachment, or a tmux one, which scrolls
   * through its own mouse reports.
   */
  rows: number | null;
  /** Wheel, touch pan and page keys, in rows; positive moves into history. */
  scrollBy: (deltaRows: number) => void;
  /** The explicit way back, and where a jump-to-live control points. */
  jumpToLive: () => void;
  /**
   * The operator sent bytes to the PTY. Typing lands at the live edge, so the
   * pane follows it there rather than leaving the window parked in history.
   */
  returnToLiveEdge: () => void;
}

interface ScrollOffsetOptions {
  /** The attached terminal is gterm-backed. False keeps `rows` at `null`. */
  native: boolean;
  /** The live attachment; a new one starts back at the live edge. */
  streamingId: string | null;
  setScrollOffset: (rowsFromLiveEdge: number, maxRows: number) => void;
  onScrollOffsetApplied: (
    callback: (applied: TerminalScrollApplied) => void,
  ) => void;
}

export function useTerminalScrollOffset({
  native,
  streamingId,
  setScrollOffset,
  onScrollOffsetApplied,
}: ScrollOffsetOptions): TerminalScrollOffset {
  const [state, setState] = useState<ScrollOffsetState>(LIVE_EDGE);
  const scrolling = native ? streamingId : null;
  const [scrolled, setScrolled] = useState<string | null>(scrolling);
  const stateRef = useRef(state);
  const streamingIdRef = useRef(streamingId);

  if (scrolled !== scrolling) {
    // Reset during render rather than in an effect: a replacement attachment
    // renders its own live edge, and the ceiling the old one reported belongs
    // to a different host terminal, so neither may survive into this pass.
    setScrolled(scrolling);
    setState(LIVE_EDGE);
  }

  useLayoutEffect(() => {
    stateRef.current = state;
    streamingIdRef.current = streamingId;
  }, [state, streamingId]);

  useEffect(() => {
    onScrollOffsetApplied((applied) => {
      // Same stale-attachment filter the output stream uses: a superseded
      // attachment's reply must not move the replacement's window.
      if (applied.streamingId !== streamingIdRef.current) return;
      setState({
        rows: Math.max(0, applied.appliedRows),
        maxRows: Math.max(0, applied.maxRows),
      });
    });
    return () => onScrollOffsetApplied(() => undefined);
  }, [onScrollOffsetApplied]);

  const request = useCallback(
    (requested: number) => {
      if (!native || streamingIdRef.current === null) return;
      const current = stateRef.current;
      const rows = clampScrollRows(current, requested);
      if (rows === current.rows) return;
      // Mirrored before the reply lands, the way gclient's
      // `set_live_scroll_offset` does: the indicator has to track the gesture,
      // not the round trip. Every reply reconciles it back.
      stateRef.current = { ...current, rows };
      setState(stateRef.current);
      setScrollOffset(rows, current.maxRows);
    },
    [native, setScrollOffset],
  );

  const scrollBy = useCallback(
    (deltaRows: number) => request(stateRef.current.rows + deltaRows),
    [request],
  );

  const jumpToLive = useCallback(() => request(0), [request]);

  const returnToLiveEdge = useCallback(() => {
    if (stateRef.current.rows !== 0) request(0);
  }, [request]);

  return {
    rows: native && streamingId !== null ? state.rows : null,
    scrollBy,
    jumpToLive,
    returnToLiveEdge,
  };
}

interface NativeScrollGestureOptions {
  /** The renderer's own scroll surface — the element gestures land on. */
  elementRef: MutableRefObject<HTMLElement | null>;
  /** Bumped when a renderer swap replaces the mount element. */
  generation: number;
  /** The daemon owns this pane's scrollback; false leaves the wheel alone. */
  active: boolean;
  rowHeightRef: MutableRefObject<number>;
  pageRows: () => number;
  onScrollRows: ((deltaRows: number) => void) | undefined;
}

/**
 * Wheel and touch pan as offset requests. The listeners are native and
 * non-passive because React registers `onWheel` and `onTouchMove` as passive
 * at the root, where `preventDefault` is ignored — and without it the browser
 * scrolls the renderer's few mirrored rows instead, which fights the offset
 * the daemon is applying.
 */
export function useNativeScrollGestures({
  elementRef,
  generation,
  active,
  rowHeightRef,
  pageRows,
  onScrollRows,
}: NativeScrollGestureOptions): void {
  const pageRowsRef = useRef(pageRows);
  const onScrollRowsRef = useRef(onScrollRows);

  useLayoutEffect(() => {
    pageRowsRef.current = pageRows;
    onScrollRowsRef.current = onScrollRows;
  }, [onScrollRows, pageRows]);

  useEffect(() => {
    const element = elementRef.current;
    if (!element) return;
    // `pan-y` hands the vertical gesture to the browser, which has nothing to
    // scroll here and claims the pan before `preventDefault` can. Pinch-zoom
    // stays: it is an accessibility affordance, not a scroll.
    element.style.touchAction = active ? "pinch-zoom" : "pan-y pinch-zoom";
    if (!active) return;

    const accumulate = createRowAccumulator();
    const rowHeight = () => Math.max(rowHeightRef.current, 1);
    const scrollRows = (rows: number) => {
      const whole = accumulate(rows);
      if (whole !== 0) onScrollRowsRef.current?.(whole);
    };
    const handleWheel = (event: WheelEvent) => {
      event.preventDefault();
      scrollRows(wheelScrollRows(event, rowHeight(), pageRowsRef.current()));
    };
    let panY: number | null = null;
    const handleTouchStart = (event: TouchEvent) => {
      panY =
        event.touches.length === 1 ? (event.touches[0]?.clientY ?? null) : null;
    };
    const handleTouchMove = (event: TouchEvent) => {
      const y = event.touches[0]?.clientY;
      if (panY === null || y === undefined) return;
      event.preventDefault();
      // Dragging the content downwards pulls what sits above it into view.
      const moved = y - panY;
      panY = y;
      scrollRows(moved / rowHeight());
    };
    const endPan = () => {
      panY = null;
    };

    element.addEventListener("wheel", handleWheel, { passive: false });
    element.addEventListener("touchstart", handleTouchStart, { passive: true });
    element.addEventListener("touchmove", handleTouchMove, { passive: false });
    element.addEventListener("touchend", endPan, { passive: true });
    element.addEventListener("touchcancel", endPan, { passive: true });
    return () => {
      element.removeEventListener("wheel", handleWheel);
      element.removeEventListener("touchstart", handleTouchStart);
      element.removeEventListener("touchmove", handleTouchMove);
      element.removeEventListener("touchend", endPan);
      element.removeEventListener("touchcancel", endPan);
    };
  }, [active, elementRef, generation, rowHeightRef]);
}
