import { readFile } from "node:fs/promises";
import { join } from "node:path";

import { GhosttyCore } from "@wterm/ghostty";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";

import { remapAnsiCell, withGobbyAnsiPalette } from "../ghosttyAnsiPalette";

const WASM_PATH = join(
  process.cwd(),
  "node_modules/@wterm/ghostty/wasm/ghostty-vt.wasm",
);

const DEFAULT_COLOR = 256;

describe("withGobbyAnsiPalette", () => {
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
    core = withGobbyAnsiPalette(await GhosttyCore.load({ wasmPath: "test" }));
    core.init(40, 6);
  });

  afterAll(() => {
    vi.unstubAllGlobals();
  });

  it("maps palette SGR colors back to --term-color indexes", () => {
    core.writeString("\x1b[32mg\x1b[0m\x1b[42mG\x1b[0m\x1b[91mr\x1b[0m");
    core.writeString("\x1b[48;5;4mB\x1b[0m\r\n");

    const green = core.getCell(0, 0);
    expect(green.fg).toBe(2);
    expect(green.fgRgb).toBeUndefined();

    const greenBg = core.getCell(0, 1);
    expect(greenBg.bg).toBe(2);
    expect(greenBg.bgRgb).toBeUndefined();

    const brightRed = core.getCell(0, 2);
    expect(brightRed.fg).toBe(9);
    expect(brightRed.fgRgb).toBeUndefined();

    const indexedBlue = core.getCell(0, 3);
    expect(indexedBlue.bg).toBe(4);
    expect(indexedBlue.bgRgb).toBeUndefined();
  });

  it("passes truecolor SGR through untouched", () => {
    core.writeString("\x1b[48;2;20;80;40mT\x1b[0m\r\n");

    const cell = core.getCell(1, 0);
    expect(cell.bg).toBe(DEFAULT_COLOR);
    expect(cell.bgRgb).toBe(0x145028);
  });

  it("remaps scrollback cells too", () => {
    core.writeString("\x1b[42ms\x1b[0m\r\n".repeat(8));

    const count = core.getScrollbackCount();
    expect(count).toBeGreaterThan(0);
    const cell = core.getScrollbackCell(0, 0);
    expect(cell.bg).toBe(2);
    expect(cell.bgRgb).toBeUndefined();
  });
});

describe("remapAnsiCell", () => {
  it("returns unmatched cells unchanged by identity", () => {
    const cell = {
      char: 65,
      fg: DEFAULT_COLOR,
      bg: DEFAULT_COLOR,
      flags: 0,
      fgRgb: 0x123456,
    };
    expect(remapAnsiCell(cell)).toBe(cell);
  });

  it("remaps fg and bg independently", () => {
    const cell = {
      char: 65,
      fg: DEFAULT_COLOR,
      bg: DEFAULT_COLOR,
      flags: 0,
      fgRgb: 0xb5bd68,
      bgRgb: 0x123456,
    };
    const next = remapAnsiCell(cell);
    expect(next).not.toBe(cell);
    expect(next.fg).toBe(2);
    expect(next.fgRgb).toBeUndefined();
    expect(next.bg).toBe(DEFAULT_COLOR);
    expect(next.bgRgb).toBe(0x123456);
    expect(cell.fgRgb).toBe(0xb5bd68);
  });
});
