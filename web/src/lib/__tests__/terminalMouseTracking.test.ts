import { readFile } from "node:fs/promises";
import { join } from "node:path";

import { WTerm } from "@wterm/dom";
import { GhosttyCore } from "@wterm/ghostty";
import {
  afterAll,
  afterEach,
  beforeAll,
  describe,
  expect,
  it,
  vi,
} from "vitest";

import {
  createMouseModeProbe,
  withAnyMotionMouseTracking,
} from "../terminalMouseTracking";

const WASM_PATH = join(
  process.cwd(),
  "node_modules/@wterm/ghostty/wasm/ghostty-vt.wasm",
);

describe("createMouseModeProbe", () => {
  it("reports 1003 from any-event tracking plus SGR", () => {
    const probe = createMouseModeProbe();
    probe.feed("\x1b[?1003h\x1b[?1006h");
    expect(probe.tracking()).toBe(1003);
    expect(probe.sgr()).toBe(true);
  });

  it("parses combined private-mode params", () => {
    const probe = createMouseModeProbe();
    probe.feed("\x1b[?1000;1003;1006h");
    expect(probe.tracking()).toBe(1003);
    expect(probe.sgr()).toBe(true);
  });

  it("prefers 1003 over 1002 and 1000", () => {
    const probe = createMouseModeProbe();
    probe.feed("\x1b[?1000h\x1b[?1002h\x1b[?1003h");
    expect(probe.tracking()).toBe(1003);
  });

  it("falls back to 1002 after 1003 is reset", () => {
    const probe = createMouseModeProbe();
    probe.feed("\x1b[?1002h\x1b[?1003h\x1b[?1003l");
    expect(probe.tracking()).toBe(1002);
  });

  it("clears tracking when the active mode is reset", () => {
    const probe = createMouseModeProbe();
    probe.feed("\x1b[?1003h\x1b[?1003l");
    expect(probe.tracking()).toBe(0);
  });

  it("stitches a CSI sequence split across writes", () => {
    const probe = createMouseModeProbe();
    probe.feed("\x1b[?1003");
    expect(probe.tracking()).toBe(0);
    probe.feed("h\x1b[?1006h");
    expect(probe.tracking()).toBe(1003);
    expect(probe.sgr()).toBe(true);
  });

  it("ignores unrelated DEC private modes", () => {
    const probe = createMouseModeProbe();
    probe.feed("\x1b[?25h\x1b[?2004h\x1b[?1049h");
    expect(probe.tracking()).toBe(0);
    expect(probe.sgr()).toBe(false);
  });
});

describe("withAnyMotionMouseTracking", () => {
  let core: GhosttyCore;

  beforeAll(async () => {
    const bytes = await readFile(WASM_PATH);
    const buffer = bytes.buffer.slice(
      bytes.byteOffset,
      bytes.byteOffset + bytes.byteLength,
    );
    vi.stubGlobal("fetch", async () => ({
      ok: true,
      status: 200,
      statusText: "OK",
      arrayBuffer: async () => buffer,
    }));
    core = withAnyMotionMouseTracking(
      await GhosttyCore.load({ wasmPath: "test" }),
    );
    core.init(80, 24);
  });

  afterAll(() => {
    vi.unstubAllGlobals();
  });

  afterEach(() => {
    core.writeString("\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l");
  });

  it("exposes 1003 that Ghostty WASM's mouse_tracking export does not", () => {
    core.writeString("\x1b[?1003h\x1b[?1006h");
    expect(core.mouseTracking()).toBe(1003);
    expect(core.mouseSgr()).toBe(true);
  });

  it("still reports 1000 when the native export already does", () => {
    core.writeString("\x1b[?1000h\x1b[?1006h");
    expect(core.mouseTracking()).toBe(1000);
    expect(core.mouseSgr()).toBe(true);
  });

  it("does not invent tracking from unrelated writes", () => {
    core.writeString("hello\r\n\x1b[?25h\x1b[?1049h");
    expect(core.mouseTracking()).toBe(0);
  });

  it("recovers 1003 from writeRaw", () => {
    core.writeRaw(new TextEncoder().encode("\x1b[?1003h\x1b[?1006h"));
    expect(core.mouseTracking()).toBe(1003);
  });

  it("stitches 1003 across two writeString calls", () => {
    core.writeString("\x1b[?1003");
    expect(core.mouseTracking()).toBe(0);
    core.writeString("h");
    expect(core.mouseTracking()).toBe(1003);
  });
});

describe("wterm SGR wheel under 1003", () => {
  let wrapped: GhosttyCore;
  let unwrapped: GhosttyCore;

  beforeAll(async () => {
    const bytes = await readFile(WASM_PATH);
    const buffer = bytes.buffer.slice(
      bytes.byteOffset,
      bytes.byteOffset + bytes.byteLength,
    );
    vi.stubGlobal("fetch", async () => ({
      ok: true,
      status: 200,
      statusText: "OK",
      arrayBuffer: async () => buffer,
    }));
    unwrapped = await GhosttyCore.load({ wasmPath: "test-unwrapped" });
    unwrapped.init(80, 24);
    wrapped = withAnyMotionMouseTracking(
      await GhosttyCore.load({ wasmPath: "test-wrapped" }),
    );
    wrapped.init(80, 24);
  });

  afterAll(() => {
    vi.unstubAllGlobals();
  });

  it("does not encode wheel when Ghostty reports tracking 0", async () => {
    const onData = vi.fn();
    const { terminal, element } = await mountTerm(unwrapped, onData);
    unwrapped.writeString("\x1b[?1003h\x1b[?1006h");
    expect(unwrapped.mouseTracking()).toBe(0);
    dispatchWheel(element, -40);
    expect(onData).not.toHaveBeenCalled();
    terminal.destroy();
  });

  it("encodes SGR wheel once 1003 is recovered", async () => {
    const onData = vi.fn();
    const { terminal, element } = await mountTerm(wrapped, onData);
    wrapped.writeString("\x1b[?1003h\x1b[?1006h");
    expect(wrapped.mouseTracking()).toBe(1003);
    dispatchWheel(element, -40);
    expect(onData).toHaveBeenCalled();
    const payload = String(onData.mock.calls[0]?.[0]);
    expect(payload.startsWith("\u001b[<64;")).toBe(true);
    expect(payload.endsWith("M")).toBe(true);
    terminal.destroy();
  });
});

async function mountTerm(core: GhosttyCore, onData: (data: string) => void) {
  const element = document.createElement("div");
  document.body.append(element);
  element.getBoundingClientRect = () =>
    ({
      x: 0,
      y: 0,
      top: 0,
      left: 0,
      width: 640,
      height: 384,
      right: 640,
      bottom: 384,
      toJSON() {
        return {};
      },
    }) as DOMRect;
  const terminal = new WTerm(element, {
    core,
    cols: 80,
    rows: 24,
    autoResize: false,
    onData,
  });
  await terminal.init();
  Reflect.set(terminal, "_charWidth", 8);
  Reflect.set(terminal, "_rowHeight", 16);
  return { terminal, element };
}

function dispatchWheel(element: HTMLElement, deltaY: number): void {
  element.dispatchEvent(
    new WheelEvent("wheel", {
      deltaY,
      clientX: 16,
      clientY: 16,
      bubbles: true,
      cancelable: true,
    }),
  );
}
