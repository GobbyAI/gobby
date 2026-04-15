import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  clearProviderModelCache,
  fetchProviderModelCatalog,
  getPreferredModelForProvider,
  resolveProviderModelPair,
  type ProviderModelEntry,
} from "../providerModels";

const catalog: ProviderModelEntry[] = [
  {
    provider: "claude",
    available: true,
    source: "live",
    models: [
      {
        value: "haiku",
        label: "Haiku",
        canonical_id: "claude-haiku-4-5-20251001",
      },
      {
        value: "sonnet",
        label: "Sonnet",
        canonical_id: "claude-sonnet-4-6",
      },
      {
        value: "opus",
        label: "Opus",
        canonical_id: "claude-opus-4-6",
      },
    ],
  },
];

describe("providerModels", () => {
  beforeEach(() => {
    clearProviderModelCache();
  });

  afterEach(() => {
    clearProviderModelCache();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("normalizes canonical model identifiers to the provider alias", () => {
    expect(
      getPreferredModelForProvider(catalog, "claude", "claude-opus-4-6"),
    ).toBe("opus");
  });

  it("resolves provider/model pairs using canonical model identifiers", () => {
    expect(
      resolveProviderModelPair(catalog, {
        provider: "claude",
        model: "claude-opus-4-6",
      }),
    ).toEqual({
      provider: "claude",
      model: "opus",
    });
  });

  it("logs and returns an empty catalog when the fetch fails", async () => {
    const debugSpy = vi.spyOn(console, "debug").mockImplementation(() => {});
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));

    await expect(fetchProviderModelCatalog()).resolves.toEqual([]);
    expect(debugSpy).toHaveBeenCalledWith(
      "Failed to load provider catalog",
      expect.any(Error),
    );
  });

  it("clearProviderModelCache resets the cached catalog", async () => {
    const fetchSpy = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          providers: [
            { provider: "claude", available: true, source: "live", models: [] },
          ],
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          providers: [
            { provider: "codex", available: true, source: "live", models: [] },
          ],
        }),
      });
    vi.stubGlobal("fetch", fetchSpy);

    await expect(fetchProviderModelCatalog()).resolves.toEqual([
      { provider: "claude", available: true, source: "live", models: [] },
    ]);
    await expect(fetchProviderModelCatalog()).resolves.toEqual([
      { provider: "claude", available: true, source: "live", models: [] },
    ]);

    clearProviderModelCache();

    await expect(fetchProviderModelCatalog()).resolves.toEqual([
      { provider: "codex", available: true, source: "live", models: [] },
    ]);
    expect(fetchSpy).toHaveBeenCalledTimes(2);
  });
});
