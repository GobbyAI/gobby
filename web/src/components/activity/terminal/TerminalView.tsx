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
import { cn } from "../../../lib/utils";
import { Button } from "../../ui/Button";
import { coarseHitAreaCls } from "../../ui/controlStyles";

const RESIZE_DEBOUNCE_MS = 200;

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
}

export interface TerminalViewProps {
  onSizeChange?: (rows: number, cols: number) => void;
  onReady?: (rows: number, cols: number) => void;
  onProtocolResponse?: (data: string) => void;
}

interface TerminalInstanceProps {
  container: HTMLDivElement;
  resolution: Extract<
    RendererResolution,
    { status: "ghostty" | "fallback" }
  >;
  terminalRef: MutableRefObject<WTerm | null>;
  sizeRef: MutableRefObject<TerminalSize | null>;
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

// wterm's built-in autoResize probes character metrics once during init and
// silently gives up when the measurement lands at 0 (fonts not yet loaded,
// layout not settled), leaving the grid stuck at the 80x24 default forever.
// Measure with the same .term-row probe so the numbers match the renderer.
function measureCell(root: HTMLElement): { charWidth: number; rowHeight: number } | null {
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

function TerminalInstance({
  container,
  resolution,
  terminalRef,
  sizeRef,
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
        const textarea = readyTerminal.element.querySelector("textarea");
        if (textarea) {
          textarea.disabled = true;
          textarea.tabIndex = -1;
          textarea.blur();
        }
        // Own the container-to-grid fit: resize the terminal whenever the
        // outer container changes size (dock expand/collapse, panel resize)
        // and once fonts finish loading so the first measurement isn't
        // garbage. Observe the container, not the wterm element — the
        // renderer locks the wterm element's height to the current grid, so
        // it never reports growth on its own.
        const applyFit = () => {
          if (disposed) return;
          const cell = measureCell(readyTerminal.element);
          if (!cell) return;
          const rect = container.getBoundingClientRect();
          const cols = Math.max(1, Math.floor(rect.width / cell.charWidth));
          const rows = Math.max(1, Math.floor(rect.height / cell.rowHeight));
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
      terminal.destroy();
      mountElement.remove();
    };
  }, [
    container,
    onInitError,
    onProtocolResponseRef,
    onReadyRef,
    onSizeChangeRef,
    resolution,
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
    const onSizeChangeRef = useRef(onSizeChange);
    const onReadyRef = useRef(onReady);
    const onProtocolResponseRef = useRef(onProtocolResponse);
    const [container, setContainer] = useState<HTMLDivElement | null>(null);
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
      }),
      [],
    );

    const activeResolution =
      resolution.status === "ghostty" || resolution.status === "fallback"
        ? resolution
        : null;

    return (
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
                <strong className="font-semibold">Terminal renderer unavailable</strong>
                <span className="overflow-wrap-break-word text-[var(--text-secondary)]">
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
    );
  },
);
