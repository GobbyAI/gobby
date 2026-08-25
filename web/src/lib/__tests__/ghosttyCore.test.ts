import { beforeEach, describe, expect, it, vi } from "vitest";

const { load } = vi.hoisted(() => ({
  load: vi.fn<
    (options: { wasmPath: string; scrollbackLimit: number }) => Promise<object>
  >(),
}));

vi.mock("@wterm/ghostty", () => ({
  GhosttyCore: { load },
}));

import { loadGhosttyCore } from "../ghosttyCore";

// The loader pipes every core through withGobbyAnsiPalette, which rebinds
// these two cell accessors on the loaded instance.
function fakeCore(id: string) {
  return { id, getCell: vi.fn(), getScrollbackCell: vi.fn() };
}

describe("loadGhosttyCore", () => {
  beforeEach(() => {
    load.mockReset();
  });

  it("returns a fresh core per call", async () => {
    const firstCore = fakeCore("first");
    const secondCore = fakeCore("second");
    load.mockResolvedValueOnce(firstCore).mockResolvedValueOnce(secondCore);

    await expect(loadGhosttyCore()).resolves.toBe(firstCore);
    await expect(loadGhosttyCore()).resolves.toBe(secondCore);

    expect(load).toHaveBeenCalledTimes(2);
    expect(load).toHaveBeenNthCalledWith(1, {
      wasmPath: "/wasm/ghostty-vt.wasm",
      scrollbackLimit: 10_000_000,
    });
    expect(load).toHaveBeenNthCalledWith(2, {
      wasmPath: "/wasm/ghostty-vt.wasm",
      scrollbackLimit: 10_000_000,
    });
  });

  it("can load after a rejected attempt", async () => {
    const recoveredCore = fakeCore("recovered");
    load
      .mockRejectedValueOnce(new Error("wasm unavailable"))
      .mockResolvedValueOnce(recoveredCore);

    await expect(loadGhosttyCore()).rejects.toThrow("wasm unavailable");
    await expect(loadGhosttyCore()).resolves.toBe(recoveredCore);
    expect(load).toHaveBeenCalledTimes(2);
  });
});
