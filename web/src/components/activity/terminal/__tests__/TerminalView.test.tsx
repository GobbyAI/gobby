import {
  act,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode, createRef, type ReactNode } from "react";
import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from "vitest";

import {
  TerminalView,
  type TerminalViewHandle,
} from "../TerminalView";

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason?: unknown) => void;
}

interface MockWTermOptions {
  core?: unknown;
  autoResize?: boolean;
  onData?: (data: string) => void;
  onResize?: (cols: number, rows: number) => void;
}

interface MockWTermInstance {
  element: HTMLElement;
  options: MockWTermOptions;
  rows: number;
  cols: number;
  textarea: HTMLTextAreaElement;
  marker: HTMLSpanElement;
  init: () => Promise<MockWTermInstance>;
  write: (data: string) => void;
  resize: (cols: number, rows: number) => void;
  destroy: () => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

const terminalMock = vi.hoisted(() => ({
  instances: [] as MockWTermInstance[],
  initGates: [] as Array<Promise<void>>,
  loadGhosttyCore: vi.fn(),
}));

vi.mock("../../../../lib/ghosttyCore", () => ({
  loadGhosttyCore: terminalMock.loadGhosttyCore,
}));

vi.mock("@wterm/dom", () => {
  class MockWTerm implements MockWTermInstance {
    element: HTMLElement;
    options: MockWTermOptions;
    rows = 57;
    cols = 211;
    textarea: HTMLTextAreaElement;
    marker: HTMLSpanElement;
    init: () => Promise<MockWTermInstance>;
    write = vi.fn((_data: string) => undefined);
    resize = vi.fn((cols: number, rows: number) => {
      this.cols = cols;
      this.rows = rows;
      this.options.onResize?.(cols, rows);
    });
    destroy: () => void;

    constructor(element: HTMLElement, options: MockWTermOptions) {
      this.element = element;
      this.options = options;
      this.textarea = document.createElement("textarea");
      this.marker = document.createElement("span");
      this.marker.dataset.mockTerminal = String(terminalMock.instances.length + 1);
      this.marker.textContent = `terminal-${terminalMock.instances.length + 1}`;
      this.element.append(this.textarea, this.marker);
      this.element.addEventListener("click", () => {
        if (!this.textarea.disabled) this.textarea.focus();
      });

      const gate = terminalMock.initGates.shift();
      this.init = vi.fn(async () => {
        try {
          if (gate) await gate;
          this.textarea.focus();
          return this;
        } catch (error) {
          this.destroy();
          throw error;
        }
      });
      this.destroy = vi.fn(() => {
        this.element.innerHTML = "";
      });
      terminalMock.instances.push(this);
    }
  }

  return { WTerm: MockWTerm };
});

async function settleAsyncWork(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

function latestInstance(): MockWTermInstance {
  const instance = terminalMock.instances[terminalMock.instances.length - 1];
  if (!instance) throw new Error("Expected a WTerm instance");
  return instance;
}

function TerminalFocusHarness({ children }: { children: ReactNode }) {
  return (
    <>
      <button type="button">Before terminal</button>
      {children}
      <button type="button">After terminal</button>
    </>
  );
}

beforeEach(() => {
  terminalMock.instances.length = 0;
  terminalMock.initGates.length = 0;
  terminalMock.loadGhosttyCore.mockReset();
  terminalMock.loadGhosttyCore.mockResolvedValue({ kind: "ghostty" });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("TerminalView", () => {
  it("read only and ready handshake", async () => {
    const user = userEvent.setup();
    const onProtocolResponse = vi.fn();
    const onReady = vi.fn();
    const terminalRef = createRef<TerminalViewHandle>();

    render(
      <TerminalFocusHarness>
        <TerminalView
          ref={terminalRef}
          onProtocolResponse={onProtocolResponse}
          onReady={onReady}
        />
      </TerminalFocusHarness>,
    );

    await waitFor(() => expect(onReady).toHaveBeenCalledWith(57, 211));

    const view = screen.getByTestId("terminal-view");
    const instance = latestInstance();
    expect(view).toHaveAttribute("role", "log");
    expect(view).toHaveAttribute(
      "aria-label",
      "Terminal output (read-only)",
    );
    expect(view).not.toHaveAttribute("aria-multiline");
    expect(view).not.toHaveAttribute("aria-readonly");
    expect(view).not.toHaveAttribute("tabindex");
    expect(instance.textarea).toBeDisabled();
    expect(instance.textarea).toHaveAttribute("tabindex", "-1");
    expect(document.activeElement).not.toBe(instance.textarea);
    expect(instance.element.style.getPropertyValue("--term-bg")).toBe(
      "var(--bg-primary)",
    );
    expect(instance.element.style.getPropertyValue("--term-font-family")).toBe(
      "var(--font-mono)",
    );

    await user.click(instance.marker);
    expect(document.activeElement).not.toBe(instance.textarea);

    await user.click(screen.getByRole("button", { name: "Before terminal" }));
    await user.tab();
    expect(screen.getByRole("button", { name: "After terminal" })).toHaveFocus();
    await user.keyboard("terminal input must stay inert");
    expect(onProtocolResponse).not.toHaveBeenCalled();

    const selection = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(instance.marker);
    selection?.removeAllRanges();
    selection?.addRange(range);
    expect(selection?.toString()).toBe(instance.marker.textContent);

    act(() => instance.options.onData?.("\u001b[?1;2c"));
    expect(onProtocolResponse).toHaveBeenCalledWith("\u001b[?1;2c");

    act(() => terminalRef.current?.write("hello"));
    expect(instance.write).toHaveBeenCalledWith("hello");
    expect(terminalRef.current?.getSize()).toEqual({ rows: 57, cols: 211 });
  });

  it("fits the grid to the mount size once measurable", async () => {
    const rect = (width: number, height: number): DOMRect =>
      ({
        width,
        height,
        x: 0,
        y: 0,
        top: 0,
        left: 0,
        bottom: height,
        right: width,
        toJSON: () => ({}),
      }) as DOMRect;
    const rectSpy = vi
      .spyOn(Element.prototype, "getBoundingClientRect")
      .mockImplementation(function (this: Element) {
        if (this instanceof HTMLSpanElement) return rect(8, 16);
        if (this.classList.contains("term-row")) return rect(800, 16);
        return rect(800, 320);
      });

    try {
      const onSizeChange = vi.fn();
      render(<TerminalView onSizeChange={onSizeChange} />);
      await settleAsyncWork();

      // 800px / 8px per char = 100 cols; 320px / 16px per row = 20 rows.
      const instance = latestInstance();
      expect(instance.resize).toHaveBeenCalledWith(100, 20);
      await waitFor(() => expect(onSizeChange).toHaveBeenCalledWith(20, 100));
    } finally {
      rectSpy.mockRestore();
    }
  });

  it("subtracts the wterm element's padding from the fitted grid", async () => {
    const rect = (width: number, height: number): DOMRect =>
      ({
        width,
        height,
        x: 0,
        y: 0,
        top: 0,
        left: 0,
        bottom: height,
        right: width,
        toJSON: () => ({}),
      }) as DOMRect;
    const rectSpy = vi
      .spyOn(Element.prototype, "getBoundingClientRect")
      .mockImplementation(function (this: Element) {
        if (this instanceof HTMLSpanElement) return rect(8, 16);
        if (this.classList.contains("term-row")) return rect(800, 16);
        return rect(800, 320);
      });
    // A grid sized to the raw container overflows the element by its padding
    // and leaves it scrollable — the fit must subtract the padding box.
    const fitCallbacks: Array<() => void> = [];
    class ManualResizeObserver {
      constructor(callback: () => void) {
        fitCallbacks.push(callback);
      }
      observe(): void {}
      unobserve(): void {}
      disconnect(): void {}
    }
    vi.stubGlobal("ResizeObserver", ManualResizeObserver);

    try {
      render(<TerminalView />);
      await settleAsyncWork();

      const instance = latestInstance();
      instance.element.style.paddingTop = "12px";
      instance.element.style.paddingBottom = "12px";
      instance.element.style.paddingLeft = "12px";
      instance.element.style.paddingRight = "12px";
      act(() => {
        for (const callback of fitCallbacks) callback();
      });

      // (800-24)px / 8px per char = 97 cols; (320-24)px / 16px = 18.5 -> 18.
      expect(instance.resize).toHaveBeenCalledWith(97, 18);
    } finally {
      rectSpy.mockRestore();
      vi.unstubAllGlobals();
    }
  });

  it("resize transposition", async () => {
    vi.useFakeTimers();
    const onReady = vi.fn();
    const onSizeChange = vi.fn();
    const terminalRef = createRef<TerminalViewHandle>();

    render(
      <TerminalView
        ref={terminalRef}
        onReady={onReady}
        onSizeChange={onSizeChange}
      />,
    );
    await settleAsyncWork();

    expect(onReady).toHaveBeenCalledWith(57, 211);
    act(() => latestInstance().options.onResize?.(211, 57));
    expect(terminalRef.current?.getSize()).toEqual({ rows: 57, cols: 211 });
    expect(onSizeChange).not.toHaveBeenCalled();

    act(() => vi.advanceTimersByTime(199));
    expect(onSizeChange).not.toHaveBeenCalled();
    act(() => vi.advanceTimersByTime(1));
    expect(onSizeChange).toHaveBeenCalledOnce();
    expect(onSizeChange).toHaveBeenCalledWith(57, 211);
  });

  it("uses the built-in core after Ghostty fails and retries with a fresh core", async () => {
    const user = userEvent.setup();
    const freshCore = { kind: "retried-ghostty" };
    const onReady = vi.fn();
    terminalMock.loadGhosttyCore
      .mockRejectedValueOnce(new Error("Ghostty unavailable"))
      .mockResolvedValueOnce(freshCore);

    render(<TerminalView onReady={onReady} />);

    expect(
      await screen.findByText("Reduced terminal fidelity"),
    ).toBeInTheDocument();
    await waitFor(() => expect(onReady).toHaveBeenCalledWith(57, 211));
    const fallback = latestInstance();
    expect(fallback.options).not.toHaveProperty("core");

    await user.click(screen.getByRole("button", { name: "Retry Ghostty renderer" }));
    await waitFor(() => expect(terminalMock.instances).toHaveLength(2));
    await waitFor(() => expect(onReady).toHaveBeenCalledTimes(2));

    expect(fallback.destroy).toHaveBeenCalledOnce();
    expect(latestInstance().options.core).toBe(freshCore);
    expect(screen.queryByText("Reduced terminal fidelity")).not.toBeInTheDocument();
  });

  it("renders a recoverable error card when the built-in core also fails", async () => {
    const user = userEvent.setup();
    const fallbackGate = deferred<void>();
    terminalMock.loadGhosttyCore
      .mockRejectedValueOnce(new Error("Ghostty unavailable"))
      .mockResolvedValueOnce({ kind: "recovered" });
    terminalMock.initGates.push(fallbackGate.promise);

    render(<TerminalView />);

    await screen.findByText("Reduced terminal fidelity");
    await act(async () => fallbackGate.reject(new Error("Built-in core failed")));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Terminal renderer unavailable");
    expect(alert).toHaveTextContent("Built-in core failed");

    await user.click(
      screen.getByRole("button", { name: "Retry terminal renderer" }),
    );
    await waitFor(() =>
      expect(screen.queryByRole("alert")).not.toBeInTheDocument(),
    );
    await waitFor(() => expect(latestInstance().options.core).toEqual({ kind: "recovered" }));
  });
});

describe("lifecycle destroy", () => {
  it("destroys a StrictMode replay immediately and suppresses its late ready work", async () => {
    const firstInit = deferred<void>();
    const onReady = vi.fn();
    const onProtocolResponse = vi.fn();
    const terminalRef = createRef<TerminalViewHandle>();
    terminalMock.initGates.push(firstInit.promise, Promise.resolve());

    render(
      <StrictMode>
        <TerminalView
          ref={terminalRef}
          onReady={onReady}
          onProtocolResponse={onProtocolResponse}
        />
      </StrictMode>,
    );
    await settleAsyncWork();

    expect(terminalMock.instances).toHaveLength(2);
    const [replayed, survivor] = terminalMock.instances;
    expect(replayed.destroy).toHaveBeenCalledOnce();
    expect(survivor.destroy).not.toHaveBeenCalled();
    expect(survivor.marker).toBeInTheDocument();
    expect(onReady).toHaveBeenCalledOnce();
    expect(terminalRef.current?.getSize()).toEqual({ rows: 57, cols: 211 });
    act(() => survivor.options.onData?.("survivor-response"));
    expect(onProtocolResponse).toHaveBeenCalledWith("survivor-response");

    await act(async () => firstInit.resolve());
    expect(onReady).toHaveBeenCalledOnce();
    expect(terminalRef.current?.getSize()).toEqual({ rows: 57, cols: 211 });
    expect(survivor.marker).toBeInTheDocument();
  });

  it("contains an upstream late rejection inside the dead mount node", async () => {
    const firstInit = deferred<void>();
    terminalMock.initGates.push(firstInit.promise, Promise.resolve());

    render(
      <StrictMode>
        <TerminalView />
      </StrictMode>,
    );
    await settleAsyncWork();

    const [replayed, survivor] = terminalMock.instances;
    expect(replayed.destroy).toHaveBeenCalledOnce();
    expect(survivor.marker).toBeInTheDocument();

    await act(async () => firstInit.reject(new Error("late init rejection")));
    expect(replayed.destroy).toHaveBeenCalledTimes(2);
    expect(survivor.destroy).not.toHaveBeenCalled();
    expect(survivor.marker).toBeInTheDocument();
  });

  it("destroys fallback and keyed instances exactly once", async () => {
    const user = userEvent.setup();
    const fallbackInit = deferred<void>();
    const onReady = vi.fn();
    const terminalRef = createRef<TerminalViewHandle>();
    terminalMock.loadGhosttyCore
      .mockRejectedValueOnce(new Error("Ghostty unavailable"))
      .mockResolvedValue({ kind: "ghostty" });
    terminalMock.initGates.push(
      fallbackInit.promise,
      Promise.resolve(),
      Promise.resolve(),
    );

    const { rerender } = render(
      <TerminalView key="fallback" ref={terminalRef} onReady={onReady} />,
    );
    await screen.findByText("Reduced terminal fidelity");
    const fallback = latestInstance();

    await user.click(screen.getByRole("button", { name: "Retry Ghostty renderer" }));
    await waitFor(() => expect(terminalMock.instances).toHaveLength(2));
    await waitFor(() => expect(onReady).toHaveBeenCalledOnce());
    const retried = latestInstance();
    expect(fallback.destroy).toHaveBeenCalledOnce();
    expect(retried.marker).toBeInTheDocument();

    await act(async () => fallbackInit.resolve());
    expect(onReady).toHaveBeenCalledOnce();
    expect(terminalRef.current?.getSize()).toEqual({ rows: 57, cols: 211 });
    expect(retried.marker).toBeInTheDocument();

    rerender(
      <TerminalView key="replacement" ref={terminalRef} onReady={onReady} />,
    );
    expect(retried.destroy).toHaveBeenCalledOnce();
    await waitFor(() => expect(terminalMock.instances).toHaveLength(3));
    await waitFor(() => expect(onReady).toHaveBeenCalledTimes(2));
    expect(latestInstance().marker).toBeInTheDocument();
  });

  it("cancels a pending resize report before unmount", async () => {
    vi.useFakeTimers();
    const onSizeChange = vi.fn();
    const { unmount } = render(<TerminalView onSizeChange={onSizeChange} />);
    await settleAsyncWork();

    const instance = latestInstance();
    act(() => instance.options.onResize?.(211, 57));
    unmount();
    expect(instance.destroy).toHaveBeenCalledOnce();

    act(() => vi.advanceTimersByTime(200));
    expect(onSizeChange).not.toHaveBeenCalled();
  });
});
