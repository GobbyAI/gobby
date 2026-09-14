import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import { TerminalTab } from "../TerminalTab";
import {
  makeHookState,
  makeTmuxSession,
  type HookResult,
} from "./terminalTabFixtures";

const mockUseTmuxSessions = vi.hoisted(() => vi.fn<() => HookResult>());
const renderer = vi.hoisted(() => ({
  resize: vi.fn<(cols: number, rows: number) => void>(),
}));

vi.mock("../../../../hooks/useTmuxSessions", () => ({
  useTmuxSessions: mockUseTmuxSessions,
}));

vi.mock("../../../../lib/ghosttyCore", () => ({
  loadGhosttyCore: vi.fn(() => Promise.resolve({ kind: "ghostty" })),
}));

// The renderer is the one piece jsdom cannot run. This fake keeps its grid
// contract, so the real TerminalView fits rows against it.
vi.mock("@wterm/dom", () => ({
  WTerm: class {
    readonly element: HTMLElement;
    readonly options: { onResize?: (cols: number, rows: number) => void };
    rows = 24;
    cols = 80;

    constructor(
      element: HTMLElement,
      options: { onResize?: (cols: number, rows: number) => void },
    ) {
      this.element = element;
      this.options = options;
      element.append(document.createElement("textarea"));
    }

    init() {
      return Promise.resolve(this);
    }

    write() {}

    resize(cols: number, rows: number) {
      this.cols = cols;
      this.rows = rows;
      renderer.resize(cols, rows);
      this.options.onResize?.(cols, rows);
    }

    destroy() {
      this.element.replaceChildren();
    }
  },
}));

const rect = (width: number, height: number): DOMRect => ({
  x: 0,
  y: 0,
  top: 0,
  left: 0,
  width,
  height,
  right: width,
  bottom: height,
  toJSON: () => ({}),
});

/**
 * jsdom has no layout, so it never resizes an observed box. This observer
 * fires when a style change lands on the target or an ancestor, which is how
 * the raised-keyboard root height reaches the terminal pane in a browser.
 */
class LayoutResizeObserver {
  private readonly targets = new Set<Element>();
  private readonly mutations: MutationObserver;

  constructor(callback: () => void) {
    this.mutations = new MutationObserver((records) => {
      const targets = [...this.targets];
      const moved = records.some((record) =>
        targets.some((target) => record.target.contains(target)),
      );
      if (moved) callback();
    });
  }

  observe(target: Element): void {
    if (this.targets.size === 0) {
      this.mutations.observe(document.body, {
        attributes: true,
        attributeFilter: ["style"],
        subtree: true,
      });
    }
    this.targets.add(target);
  }

  unobserve(target: Element): void {
    this.targets.delete(target);
  }

  disconnect(): void {
    this.targets.clear();
    this.mutations.disconnect();
  }
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

it("refits the terminal rows to the visual viewport while the keyboard is raised", async () => {
  const user = userEvent.setup();
  window.localStorage.setItem("gobby:terminal:session-scope", "all");
  mockUseTmuxSessions.mockReturnValue(
    makeHookState({
      sessionsLoaded: true,
      sessions: [makeTmuxSession({ name: "interactive" })],
      attachedTarget: { terminal_id: "default:interactive" },
      streamingId: "stream-input",
    }),
  );
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: query === "(pointer: coarse)",
    media: query,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
  }));
  const viewport = Object.assign(new EventTarget(), {
    height: 800,
    offsetTop: 0,
  });
  vi.stubGlobal("visualViewport", viewport);
  vi.stubGlobal("ResizeObserver", LayoutResizeObserver);
  // Cells measure 8x16 and every box spans the TerminalTab root: its inline
  // height while the keyboard is raised, otherwise an 800px screen.
  let root: HTMLElement | null = null;
  vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(
    function (this: Element) {
      if (this instanceof HTMLSpanElement) return rect(8, 16);
      if (this.classList.contains("term-row")) return rect(800, 16);
      return rect(800, Number.parseFloat(root?.style.height ?? "") || 800);
    },
  );

  const { container } = render(<TerminalTab />);
  root = container.firstElementChild as HTMLElement;
  // 800px / 8px = 100 cols; 800px / 16px = 50 rows.
  await waitFor(() => {
    expect(renderer.resize).toHaveBeenLastCalledWith(100, 50);
  });

  const key = await screen.findByRole("button", { name: "Keyboard" });
  await user.click(key);
  act(() => {
    viewport.height = 460;
    viewport.dispatchEvent(new Event("resize"));
  });
  // 460px / 16px = 28.75, so 28 rows stay fully above the keyboard.
  await waitFor(() => {
    expect(renderer.resize).toHaveBeenLastCalledWith(100, 28);
  });

  await user.click(key);
  await waitFor(() => {
    expect(renderer.resize).toHaveBeenLastCalledWith(100, 50);
  });
});
