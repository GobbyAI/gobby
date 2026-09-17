import "@wterm/dom/css";

import { WTerm } from "@wterm/dom";
import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useLayoutEffect,
  useRef,
  useState,
  type FocusEvent,
  type MutableRefObject,
} from "react";

import { loadGhosttyCore } from "../../../lib/ghosttyCore";
import { withAnyMotionMouseTracking } from "../../../lib/terminalMouseTracking";
import { cn } from "../../../lib/utils";
import { Button } from "../../ui/Button";
import { coarseHitAreaCls } from "../../ui/controlStyles";

const RESIZE_DEBOUNCE_MS = 200;

// Neutral because either bound may have cut: the configured line window
// (tmux.attach_history_lines) or the byte backstop. Plain text, never faint
// SGR — dim escapes leave contrast unproven and land below AA in light mode.
const HISTORY_TRUNCATED_LABEL = "earlier output not shown";
const HISTORY_UNAVAILABLE_LABEL = "history unavailable";

// RIS, then ED 3. Exactly one history frame arrives per attachment, so this
// fires against an empty buffer on the first attach — a no-op — and against
// the previous attachment's tail on every replacement, which is the case that
// matters: a reconnect's window overlaps what is already rendered, and
// appending it paints those lines a second time. ED 3 rides along with RIS
// because a core that reads RIS as screen-only would keep precisely the
// scrollback that has to go, and neither core exposes a reset API to call
// instead.
const RESET_BUFFER = "c[3J";

type GhosttyCore = Awaited<ReturnType<typeof loadGhosttyCore>>;

type RendererResolution =
  | { status: "loading"; attempt: number }
  | { status: "ghostty"; attempt: number; core: GhosttyCore }
  | { status: "fallback"; attempt: number }
  | { status: "error"; attempt: number; message: string };

interface TerminalSize {
  rows: number;
  cols: number;
}

export interface TerminalViewHandle {
  write: (data: string) => void;
  getSize: () => TerminalSize | null;
  /**
   * Raise or lower the soft keyboard (with `keyboardOnDemand`). Call it inside
   * the tap that asked for it: iOS only raises the keyboard for a focus made
   * during a user gesture.
   */
  setKeyboardOpen: (open: boolean) => void;
  applyAttachHistory: (
    text: string,
    truncated: boolean,
    unavailable: boolean,
  ) => void;
}

export interface TerminalViewProps {
  onSizeChange?: (rows: number, cols: number) => void;
  onReady?: (rows: number, cols: number) => void;
  onProtocolResponse?: (data: string) => void;
  /**
   * Column floor for the grid. Defaults to the standard 80: a narrower panel
   * keeps 80 columns and zooms the rendering down. The mobile tier passes 1
   * so the grid tracks the panel width and the PTY wraps instead — mono
   * content wraps on that tier (.impeccable.md), and a CSS-scaled scroll
   * container does not touch-scroll on iOS.
   */
  minCols?: number;
  /**
   * Coarse pointers: the soft keyboard stays down until `setKeyboardOpen`
   * raises it, so a tap to scroll or select does not raise it and squeeze
   * the pane.
   */
  keyboardOnDemand?: boolean;
  /**
   * Another session holds the daemon's writer lease. The pane says so and
   * offers it back; it never silently swallows what the user types.
   */
  readOnly?: boolean;
  /** Focus entered the pane: the moment to ask for the writer lease. */
  onFocus?: () => void;
  /** Focus left the pane entirely: release the lease. */
  onBlur?: () => void;
  /** Clipboard text, sent as one bracketed write rather than as keystrokes. */
  onPaste?: (text: string) => void;
  onTakeControl?: () => void;
}

interface TerminalInstanceProps {
  container: HTMLDivElement;
  resolution: Extract<RendererResolution, { status: "ghostty" | "fallback" }>;
  terminalRef: MutableRefObject<WTerm | null>;
  sizeRef: MutableRefObject<TerminalSize | null>;
  rowHeightRef: MutableRefObject<number>;
  onScrollElement: (element: HTMLElement | null) => void;
  onSizeChangeRef: MutableRefObject<TerminalViewProps["onSizeChange"]>;
  onReadyRef: MutableRefObject<TerminalViewProps["onReady"]>;
  onProtocolResponseRef: MutableRefObject<
    TerminalViewProps["onProtocolResponse"]
  >;
  minColsRef: MutableRefObject<number>;
  keyboardOnDemandRef: MutableRefObject<boolean>;
  onInitError: (resolution: RendererResolution, error: unknown) => void;
}

type CoreStage = "write" | "resize" | "history";

/**
 * Run one call into the renderer core and keep its failure here.
 *
 * The Ghostty wasm core is built with runtime safety on, so a page-integrity
 * fault inside it traps (`RuntimeError: unreachable`) out of `write` or
 * `resize` instead of returning. Left alone, that throw unwinds the tab's
 * attach-history listener before the pane is marked ready, and the attaching
 * scrim stays over a terminal that can never be scrolled or focused. The
 * empty write afterwards is the scheduled paint the trap skipped.
 */
function guardCore(
  terminal: { write(data: string): void },
  stage: CoreStage,
  run: () => void,
): void {
  try {
    run();
  } catch (error) {
    console.warn(`[terminal] renderer core fault during ${stage}`, error);
    try {
      terminal.write("");
    } catch {
      // The core is wedged; the next attach or retry builds a fresh one.
    }
  }
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function applyGobbyTheme(element: HTMLElement): void {
  element.style.height = "100%";
  element.style.minHeight = "0";
  element.style.boxSizing = "border-box";
  element.style.borderRadius = "0";
  element.style.boxShadow = "none";
  element.style.touchAction = "pan-y pinch-zoom";
  element.style.overscrollBehaviorY = "contain";
  element.style.setProperty("-webkit-overflow-scrolling", "touch");
  element.style.setProperty("--term-fg", "var(--text-primary)");
  element.style.setProperty("--term-bg", "var(--bg-primary)");
  element.style.setProperty("--term-cursor", "var(--accent)");
  element.style.setProperty("--term-font-family", "var(--font-mono)");
  element.style.setProperty("--term-font-size", "var(--text-sm)");
  element.style.setProperty("--term-color-0", "var(--bg-primary)");
  element.style.setProperty("--term-color-1", "var(--color-error)");
  element.style.setProperty("--term-color-2", "var(--color-success)");
  element.style.setProperty("--term-color-3", "var(--color-warning)");
  element.style.setProperty("--term-color-4", "var(--color-info)");
  element.style.setProperty("--term-color-5", "var(--accent)");
  element.style.setProperty("--term-color-6", "var(--color-info)");
  element.style.setProperty("--term-color-7", "var(--text-primary)");
  element.style.setProperty("--term-color-8", "var(--text-muted)");
  element.style.setProperty("--term-color-9", "var(--color-error)");
  element.style.setProperty("--term-color-10", "var(--color-success)");
  element.style.setProperty("--term-color-11", "var(--color-warning)");
  element.style.setProperty("--term-color-12", "var(--color-info)");
  element.style.setProperty("--term-color-13", "var(--accent)");
  element.style.setProperty("--term-color-14", "var(--color-info)");
  element.style.setProperty("--term-color-15", "var(--text-primary)");
}

// The narrowest grid the viewer will request. Below this the rendering zooms
// out (CSS scale on the mount element) instead of shrinking the PTY, so full
// standard-width rows stay readable in a narrow activity panel.
const MIN_TERMINAL_COLS = 80;

// wterm's built-in autoResize probes character metrics once during init and
// silently gives up when the measurement lands at 0 (fonts not yet loaded,
// layout not settled), leaving the grid stuck at the 80x24 default forever.
// Measure with the same .term-row probe so the numbers match the renderer.
function measureCell(
  root: HTMLElement,
): { charWidth: number; rowHeight: number } | null {
  const row = document.createElement("div");
  row.className = "term-row";
  row.style.visibility = "hidden";
  row.style.position = "absolute";
  const probe = document.createElement("span");
  probe.textContent = "W";
  row.appendChild(probe);
  root.appendChild(row);
  const charWidth = probe.getBoundingClientRect().width;
  const rowHeight = row.getBoundingClientRect().height;
  row.remove();
  if (charWidth === 0 || rowHeight === 0) return null;
  return { charWidth, rowHeight };
}

// Full-width centered rule, so a marker reads as a divider without leaning on
// color to carry the meaning.
function composeMarker(label: string, cols: number): string {
  const text = ` ${label} `;
  const width = Math.max(0, cols - 1);
  if (width <= text.length) return label;
  const remaining = width - text.length;
  const left = Math.floor(remaining / 2);
  return `${"─".repeat(left)}${text}${"─".repeat(remaining - left)}`;
}

function LockIcon() {
  return (
    <svg
      className="size-3.5 shrink-0"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <rect x="3.25" y="7" width="9.5" height="6.25" rx="1.25" />
      <path d="M5.5 7V5a2.5 2.5 0 0 1 5 0v2" />
    </svg>
  );
}

/** Focus that only moved between the pane's own elements never left it. */
function stayedInside(event: FocusEvent<HTMLDivElement>): boolean {
  const next = event.relatedTarget;
  return next instanceof Node && event.currentTarget.contains(next);
}

function ChevronDownIcon() {
  return (
    <svg
      className="size-3.5"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M4 6.25 8 10.25l4-4" />
    </svg>
  );
}

function TerminalInstance({
  container,
  resolution,
  terminalRef,
  sizeRef,
  rowHeightRef,
  onScrollElement,
  onSizeChangeRef,
  onReadyRef,
  onProtocolResponseRef,
  minColsRef,
  keyboardOnDemandRef,
  onInitError,
}: TerminalInstanceProps) {
  useLayoutEffect(() => {
    let disposed = false;
    let resizeTimer: ReturnType<typeof setTimeout> | null = null;
    let fitObserver: ResizeObserver | null = null;
    const mountElement = document.createElement("div");
    applyGobbyTheme(mountElement);
    container.appendChild(mountElement);
    // applyGobbyTheme owns the touch/overscroll rules here because this is the
    // element that actually scrolls.
    onScrollElement(mountElement);

    const sharedOptions = {
      autoResize: true,
      onData: (data: string) => {
        if (!disposed) onProtocolResponseRef.current?.(data);
      },
      onResize: (cols: number, rows: number) => {
        if (disposed) return;
        sizeRef.current = { rows, cols };
        if (resizeTimer !== null) clearTimeout(resizeTimer);
        resizeTimer = setTimeout(() => {
          resizeTimer = null;
          if (!disposed) onSizeChangeRef.current?.(rows, cols);
        }, RESIZE_DEBOUNCE_MS);
      },
    };
    const terminal =
      resolution.status === "ghostty"
        ? new WTerm(mountElement, {
            ...sharedOptions,
            core: resolution.core,
          })
        : new WTerm(mountElement, sharedOptions);
    terminalRef.current = terminal;

    void terminal
      .init()
      .then((readyTerminal) => {
        if (disposed) return;
        // Ghostty cores are wrapped at load. The fallback Zig core is created
        // inside WTerm.init, so 1003 has to be recovered here before PTY bytes.
        if (resolution.status !== "ghostty" && readyTerminal.bridge) {
          withAnyMotionMouseTracking(readyTerminal.bridge);
        }
        const input = readyTerminal.element.querySelector("textarea");
        input?.setAttribute(
          "inputmode",
          keyboardOnDemandRef.current ? "none" : "text",
        );
        input?.setAttribute("autocomplete", "on");
        input?.setAttribute("autocorrect", "on");
        input?.setAttribute("spellcheck", "true");
        // Own the container-to-grid fit: resize the terminal whenever the
        // outer container changes size (dock expand/collapse, panel resize)
        // and once fonts finish loading so the first measurement isn't
        // garbage. Observe the container, not the wterm element — the
        // renderer locks the wterm element's height to the current grid, so
        // it never reports growth on its own.
        let appliedScale = 1;
        const applyFit = () => {
          if (disposed) return;
          const cell = measureCell(readyTerminal.element);
          if (!cell) return;
          // Cell probes measure through any active zoom transform on the
          // mount element; normalize back to base-font metrics so the fit
          // math is stable across passes.
          const charWidth = cell.charWidth / appliedScale;
          const rowHeight = cell.rowHeight / appliedScale;
          // WTerm's own _rowHeight is private, so the follow-live-edge
          // threshold reads the value the fit already derived. Unscaled,
          // matching the scroll metrics of the transformed mount element.
          rowHeightRef.current = rowHeight;
          const rect = container.getBoundingClientRect();
          // The grid must fit INSIDE the wterm element's padding box: sizing
          // to the raw container makes the grid overflow by the padding,
          // which leaves the element scrollable — the visible row offset then
          // depends on the write/fit ordering instead of the layout.
          const style = getComputedStyle(readyTerminal.element);
          const pad = (value: string) => Number.parseFloat(value) || 0;
          const padX = pad(style.paddingLeft) + pad(style.paddingRight);
          const padY = pad(style.paddingTop) + pad(style.paddingBottom);
          const availWidth = Math.max(0, rect.width - padX);
          const availHeight = Math.max(0, rect.height - padY);
          // Never fall below the column floor. When the panel is too narrow
          // to show it at the base font, keep the grid at the floor and zoom
          // the rendering down so full rows stay visible instead of clipping
          // or wrapping. A floor of 1 (mobile tier) disables the zoom.
          const fitCols = Math.floor(availWidth / charWidth);
          const cols = Math.max(1, minColsRef.current, fitCols);
          const scale = Math.min(1, availWidth / (cols * charWidth) || 1);
          const rows = Math.max(
            1,
            Math.floor(availHeight / (rowHeight * scale)),
          );
          if (scale !== appliedScale) {
            appliedScale = scale;
            if (scale < 1) {
              mountElement.style.transform = `scale(${scale})`;
              mountElement.style.transformOrigin = "top left";
              mountElement.style.width = `${rect.width / scale}px`;
              mountElement.style.height = `${rect.height / scale}px`;
            } else {
              mountElement.style.transform = "";
              mountElement.style.transformOrigin = "";
              mountElement.style.width = "";
              mountElement.style.height = "";
            }
          }
          if (cols !== readyTerminal.cols || rows !== readyTerminal.rows) {
            guardCore(readyTerminal, "resize", () =>
              readyTerminal.resize(cols, rows),
            );
          }
        };
        if (typeof ResizeObserver !== "undefined") {
          fitObserver = new ResizeObserver(() => applyFit());
          fitObserver.observe(container);
        }
        void document.fonts?.ready.then(() => applyFit());
        applyFit();

        const size = {
          rows: readyTerminal.rows,
          cols: readyTerminal.cols,
        };
        sizeRef.current = size;
        onReadyRef.current?.(size.rows, size.cols);
      })
      .catch((error: unknown) => {
        if (!disposed) onInitError(resolution, error);
      });

    return () => {
      disposed = true;
      if (resizeTimer !== null) clearTimeout(resizeTimer);
      fitObserver?.disconnect();
      if (terminalRef.current === terminal) terminalRef.current = null;
      sizeRef.current = null;
      onScrollElement(null);
      terminal.destroy();
      mountElement.remove();
    };
  }, [
    container,
    keyboardOnDemandRef,
    minColsRef,
    onInitError,
    onProtocolResponseRef,
    onReadyRef,
    onScrollElement,
    onSizeChangeRef,
    resolution,
    rowHeightRef,
    sizeRef,
    terminalRef,
  ]);

  return null;
}

export const TerminalView = forwardRef<TerminalViewHandle, TerminalViewProps>(
  function TerminalView(
    {
      onSizeChange,
      onReady,
      onProtocolResponse,
      minCols = MIN_TERMINAL_COLS,
      keyboardOnDemand = false,
      readOnly = false,
      onFocus,
      onBlur,
      onPaste,
      onTakeControl,
    },
    forwardedRef,
  ) {
    const terminalRef = useRef<WTerm | null>(null);
    const sizeRef = useRef<TerminalSize | null>(null);
    const rowHeightRef = useRef(0);
    const onSizeChangeRef = useRef(onSizeChange);
    const onReadyRef = useRef(onReady);
    const onProtocolResponseRef = useRef(onProtocolResponse);
    const minColsRef = useRef(minCols);
    const keyboardOnDemandRef = useRef(keyboardOnDemand);
    // Set while setKeyboardOpen refocuses the input: that blur and focus are
    // not the user leaving and entering the pane, so the lease stays put.
    const refocusingRef = useRef(false);
    const [container, setContainer] = useState<HTMLDivElement | null>(null);
    const scrollElementRef = useRef<HTMLElement | null>(null);
    const [scrollGeneration, setScrollGeneration] = useState(0);
    const [followingLiveEdge, setFollowingLiveEdge] = useState(true);
    const [attempt, setAttempt] = useState(0);
    const [resolution, setResolution] = useState<RendererResolution>({
      status: "loading",
      attempt: 0,
    });

    useLayoutEffect(() => {
      onSizeChangeRef.current = onSizeChange;
      onReadyRef.current = onReady;
      onProtocolResponseRef.current = onProtocolResponse;
      minColsRef.current = minCols;
      keyboardOnDemandRef.current = keyboardOnDemand;
    }, [keyboardOnDemand, minCols, onProtocolResponse, onReady, onSizeChange]);

    const captureContainer = useCallback((node: HTMLDivElement | null) => {
      setContainer(node);
    }, []);

    // The element lives in a ref because the jump control writes scrollTop on
    // it; the generation counter is what makes the listener re-subscribe when
    // a renderer swap replaces the mount element.
    const handleScrollElement = useCallback((element: HTMLElement | null) => {
      scrollElementRef.current = element;
      setScrollGeneration((current) => current + 1);
    }, []);

    useEffect(() => {
      const element = scrollElementRef.current;
      if (!element) {
        setFollowingLiveEdge(true);
        return;
      }
      const sync = () => {
        // One rendered row of slack: a live-edge write can land a fraction of
        // a row short of scrollHeight without meaning the user scrolled away.
        const threshold = Math.max(rowHeightRef.current, 1);
        setFollowingLiveEdge(
          element.scrollTop + element.clientHeight >=
            element.scrollHeight - threshold,
        );
      };
      sync();
      element.addEventListener("scroll", sync, { passive: true });
      // A re-fit rebuilds every row, and while the grid is empty the scroll
      // extent collapses to the viewport — which reads as "at the live edge"
      // even though the viewport never moved. No scroll event follows the
      // rebuild, so without this the pane stays parked in history with the
      // jump control gone. What grows and shrinks is the grid inside the
      // scroller, not the scroller's own box, so that is the box to watch.
      const observer = new ResizeObserver(sync);
      observer.observe(element);
      const grid = element.firstElementChild;
      if (grid) observer.observe(grid);
      return () => {
        element.removeEventListener("scroll", sync);
        observer.disconnect();
      };
    }, [scrollGeneration]);

    const jumpToBottom = useCallback(() => {
      // preventScroll is load-bearing: wterm's input textarea is parked at the
      // top of the scroll container, so a plain focus() scrolls it back into
      // view and undoes the jump.
      terminalRef.current?.element
        .querySelector<HTMLTextAreaElement>("textarea")
        ?.focus({ preventScroll: true });
      const element = scrollElementRef.current;
      if (element) {
        element.scrollTop = element.scrollHeight;
      }
      setFollowingLiveEdge(true);
    }, []);

    useEffect(() => {
      let disposed = false;
      void loadGhosttyCore().then(
        (core) => {
          if (!disposed) setResolution({ status: "ghostty", attempt, core });
        },
        () => {
          if (!disposed) setResolution({ status: "fallback", attempt });
        },
      );
      return () => {
        disposed = true;
      };
    }, [attempt]);

    const retry = useCallback(() => {
      const nextAttempt = attempt + 1;
      sizeRef.current = null;
      setResolution({ status: "loading", attempt: nextAttempt });
      setAttempt(nextAttempt);
    }, [attempt]);

    const handleInitError = useCallback(
      (failedResolution: RendererResolution, error: unknown) => {
        setResolution((current) => {
          if (current !== failedResolution) return current;
          return failedResolution.status === "ghostty"
            ? { status: "fallback", attempt: failedResolution.attempt }
            : {
                status: "error",
                attempt: failedResolution.attempt,
                message: errorMessage(error),
              };
        });
      },
      [],
    );

    useImperativeHandle(
      forwardedRef,
      () => ({
        write: (data: string) => {
          const terminal = terminalRef.current;
          if (!terminal) return;
          guardCore(terminal, "write", () => terminal.write(data));
        },
        getSize: () => sizeRef.current,
        setKeyboardOpen: (open: boolean) => {
          const input = terminalRef.current?.element.querySelector("textarea");
          if (!input) return;
          input.setAttribute("inputmode", open ? "text" : "none");
          if (document.activeElement !== input) {
            // Focusing asks for the lease like any other focus: raising the
            // keyboard is the user reaching to type.
            if (open) input.focus({ preventScroll: true });
            return;
          }
          // inputmode is read when focus lands, so an input that already has
          // focus has to be refocused for the keyboard to rise or fall.
          refocusingRef.current = true;
          try {
            input.blur();
            input.focus({ preventScroll: true });
          } finally {
            refocusingRef.current = false;
          }
        },
        applyAttachHistory: (
          text: string,
          truncated: boolean,
          unavailable: boolean,
        ) => {
          const terminal = terminalRef.current;
          if (!terminal) return;
          const rows = Math.max(sizeRef.current?.rows ?? terminal.rows, 1);
          const cols = Math.max(sizeRef.current?.cols ?? terminal.cols, 1);
          const label = unavailable
            ? HISTORY_UNAVAILABLE_LABEL
            : truncated
              ? HISTORY_TRUNCATED_LABEL
              : null;
          const marker = label ? `${composeMarker(label, cols)}\r\n` : "";
          // The screen pad is what makes the fix work at all: after the last
          // history line the cursor sits on the bottom row with a full
          // screenful of history still visible, and tmux's redraw erases the
          // display with ED 2, which does not scroll those rows into
          // scrollback. Padding by `rows` scrolls all of it into scrollback and
          // hands the repaint a blank screen.
          //
          //
          // The three calls below have to stay in this order.
          //
          // RESET_BUFFER clears the core: screen, then scrollback. That alone
          // is not enough, because the renderer's DOM scrollback is a mirror
          // driven by a *count* delta — Renderer.syncScrollback appends the
          // rows the count grew by and shifts off the front the rows it shrank
          // by. It has no notion of the scrollback being replaced. Clearing
          // and refilling nets out to roughly no change, so the previous
          // attachment's rows stay on screen and not one line of the new
          // window is ever rendered.
          //
          // WTerm.resize is what repairs that: it calls Renderer.setup
          // unconditionally, which empties the container and zeroes the
          // rendered count, even when the geometry is unchanged. Re-applying
          // the grid a replacement attachment was just given is the only
          // public call that rebuilds the mirror; neither core exposes a reset.
          guardCore(terminal, "history", () => {
            terminal.write(RESET_BUFFER);
            terminal.resize(cols, rows);
            terminal.write(`${marker}${text}${"\r\n".repeat(rows)}`);
          });
        },
      }),
      [],
    );

    const activeResolution =
      resolution.status === "ghostty" || resolution.status === "fallback"
        ? resolution
        : null;

    return (
      // Non-semantic wrapper so the jump control is a sibling of the live
      // region rather than a child of it. Focus, keys and paste are bound here
      // so they cover the renderer's own textarea and the pane's controls
      // alike; the two capture handlers have to run before the renderer's own
      // listeners, which sit on that textarea.
      <div
        className="relative h-full min-h-0 w-full"
        onFocus={(event) => {
          if (refocusingRef.current || stayedInside(event)) return;
          onFocus?.();
        }}
        onBlur={(event) => {
          if (refocusingRef.current || stayedInside(event)) return;
          // Leaving lowers the keyboard for good: the next tap back in is as
          // likely a scroll as a reach to type.
          if (keyboardOnDemandRef.current) {
            terminalRef.current?.element
              .querySelector("textarea")
              ?.setAttribute("inputmode", "none");
          }
          onBlur?.();
        }}
        onKeyDownCapture={(event) => {
          // Tab belongs to focus navigation. The renderer would send it to the
          // PTY and never let go, trapping the keyboard in a pane whose own
          // read-only, retry, discard and jump controls all sit after it —
          // and the keys bar already owns Tab and Shift+Tab as quick keys, so
          // nothing is lost by leaving this one to the browser.
          if (event.key === "Tab") event.stopPropagation();
        }}
        onPasteCapture={(event) => {
          // Ahead of the renderer, not behind it: its textarea handles paste
          // too and would push the clipboard down `onData` as keystrokes,
          // whose newlines each submit. One lease-gated `terminal_paste`
          // instead, which the daemon brackets.
          const text = event.clipboardData.getData("text");
          if (!text) return;
          event.preventDefault();
          event.stopPropagation();
          onPaste?.(text);
        }}
      >
        <div
          ref={captureContainer}
          className="relative h-full min-h-0 w-full overflow-hidden bg-[var(--bg-primary)]"
          role="log"
          aria-label="Terminal output (read-only)"
          data-testid="terminal-view"
        >
          {container && activeResolution ? (
            <TerminalInstance
              key={`${activeResolution.status}-${activeResolution.attempt}`}
              container={container}
              resolution={activeResolution}
              terminalRef={terminalRef}
              sizeRef={sizeRef}
              rowHeightRef={rowHeightRef}
              onScrollElement={handleScrollElement}
              onSizeChangeRef={onSizeChangeRef}
              onReadyRef={onReadyRef}
              onProtocolResponseRef={onProtocolResponseRef}
              minColsRef={minColsRef}
              keyboardOnDemandRef={keyboardOnDemandRef}
              onInitError={handleInitError}
            />
          ) : null}

          {resolution.status === "loading" ? (
            <div className="absolute inset-0 grid place-items-center bg-[var(--bg-primary)] text-xs text-muted-foreground">
              Loading terminal renderer…
            </div>
          ) : null}

          {resolution.status === "fallback" ? (
            <div className="absolute end-2 top-2 z-10 inline-flex max-w-[calc(100%-1rem)] items-center gap-2 rounded-md border border-warning/40 bg-[var(--bg-secondary)] px-2 py-1 text-xs text-warning shadow-sm">
              <span className="truncate">Reduced terminal fidelity</span>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                dense
                className={cn("shrink-0 px-1.5 py-0.5", coarseHitAreaCls)}
                aria-label="Retry Ghostty renderer"
                onClick={retry}
              >
                Retry
              </Button>
            </div>
          ) : null}

          {resolution.status === "error" ? (
            <div className="absolute inset-0 z-20 grid place-items-center bg-[var(--bg-primary)] p-4">
              <div
                className="flex max-w-md flex-col gap-3 rounded-md border border-[color-mix(in_srgb,var(--color-error)_45%,var(--border))] bg-[var(--color-error-soft)] p-4 text-sm text-[var(--color-error)]"
                role="alert"
              >
                <div className="flex flex-col gap-1">
                  <strong className="font-semibold">
                    Terminal renderer unavailable
                  </strong>
                  <span className="[overflow-wrap:anywhere] text-[var(--text-secondary)]">
                    {resolution.message}
                  </span>
                </div>
                <Button
                  type="button"
                  variant="secondary"
                  size="sm"
                  dense
                  className={cn(
                    "min-h-9 self-start bg-[var(--bg-primary)] px-3 py-1.5 text-[var(--text-primary)] hover:bg-[var(--bg-tertiary)]",
                    coarseHitAreaCls,
                  )}
                  onClick={retry}
                >
                  Retry terminal renderer
                </Button>
              </div>
            </div>
          ) : null}
        </div>

        {readOnly ? (
          // State is carried by the lock glyph and the words, never by hue.
          <div
            className="absolute start-2 top-2 z-30 inline-flex max-w-[calc(100%-1rem)] items-center gap-2 rounded-md border border-border bg-[var(--bg-secondary)] px-2 py-1 text-xs text-[var(--text-primary)] shadow-sm"
            data-testid="terminal-read-only"
          >
            <LockIcon />
            {/* The live region is the sentence alone. A `role="status"` is
                atomic, so wrapping the button too would re-announce the whole
                notice on every change while the user's focus sits inside it. */}
            <span className="truncate" role="status">
              Read-only — another session has control
            </span>
            <Button
              type="button"
              variant="secondary"
              size="sm"
              dense
              className={cn("shrink-0 px-1.5 py-0.5", coarseHitAreaCls)}
              data-testid="terminal-take-control"
              onClick={onTakeControl}
            >
              Take back control
            </Button>
          </div>
        ) : null}

        {!followingLiveEdge ? (
          <Button
            type="button"
            variant="secondary"
            size="icon"
            // Opaque, tokenized surface: a transparent control sitting over
            // arbitrary ANSI cell colors has no provable contrast.
            //
            // No `coarseHitAreaCls` here, deliberately: it opens with a bare
            // `relative`, which tailwind-merge resolves against this `absolute`
            // by keeping the later one — the control would lose its corner and
            // fall back into the flow. It is also the wrong tool, being for
            // `dense` controls; this button is not `dense`, so the variant
            // already floors it at 44×44 under a coarse pointer.
            className="absolute end-3 bottom-3 z-30 rounded-full border-border bg-[var(--bg-secondary)] text-[var(--text-primary)] shadow-md hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)]"
            aria-label="Jump to newest terminal output"
            data-testid="terminal-jump-to-bottom"
            onClick={jumpToBottom}
          >
            <ChevronDownIcon />
          </Button>
        ) : null}
      </div>
    );
  },
);
