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
  onInitError: (resolution: RendererResolution, error: unknown) => void;
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
        input?.setAttribute("inputmode", "text");
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
          // Never fall below the standard terminal width. When the panel is
          // too narrow to show MIN_COLS at the base font, keep the grid at
          // MIN_COLS and zoom the rendering down so full rows stay visible
          // instead of clipping or wrapping.
          const fitCols = Math.floor(availWidth / charWidth);
          const cols = Math.max(MIN_TERMINAL_COLS, fitCols);
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
            readyTerminal.resize(cols, rows);
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
    { onSizeChange, onReady, onProtocolResponse },
    forwardedRef,
  ) {
    const terminalRef = useRef<WTerm | null>(null);
    const sizeRef = useRef<TerminalSize | null>(null);
    const rowHeightRef = useRef(0);
    const onSizeChangeRef = useRef(onSizeChange);
    const onReadyRef = useRef(onReady);
    const onProtocolResponseRef = useRef(onProtocolResponse);
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
    }, [onProtocolResponse, onReady, onSizeChange]);

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
      return () => element.removeEventListener("scroll", sync);
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
        write: (data: string) => terminalRef.current?.write(data),
        getSize: () => sizeRef.current,
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
          // One write, so wterm makes exactly one syncScrollback pass.
          terminal.write(`${marker}${text}${"\r\n".repeat(rows)}`);
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
      // region rather than a child of it.
      <div className="relative h-full min-h-0 w-full">
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

        {!followingLiveEdge ? (
          <Button
            type="button"
            variant="secondary"
            size="icon"
            // Opaque, tokenized surface: a transparent control sitting over
            // arbitrary ANSI cell colors has no provable contrast.
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
