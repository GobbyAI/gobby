import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  clearProviderModelCache,
  fetchProviderModelCatalog,
  getModelLabel,
  getModelsForProvider,
  getOrderedProviders,
  getPreferredModelForProvider,
  getProviderDisplayName,
  getPreferredReasoningEffort,
  getReasoningOptionsForModel,
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
        value: "claude-sonnet-4-5-20250901",
        label: "Sonnet",
        canonical_id: "claude-sonnet-4-5-20250901",
      },
      {
        value: "claude-sonnet-4-5-20251001",
        label: "Sonnet latest",
        canonical_id: "claude-sonnet-4-5-20251001",
      },
      {
        value: "opus",
        label: "Opus",
        canonical_id: "claude-opus-4-6",
        reasoning: {
          supported_efforts: ["low", "medium", "high", "max"],
        },
      },
    ],
  },
  {
    provider: "codex",
    available: true,
    source: "live",
    models: [
      {
        value: "gpt-5.4",
        label: "gpt-5.4",
        reasoning: {
          supported_efforts: ["low", "medium", "high", "xhigh"],
          default_effort: "medium",
        },
      },
      {
        value: "gpt-5.4-mini",
        label: "gpt-5.4-mini",
      },
      {
        value: "gpt-5.3-codex-spark",
        label: "gpt-5.3-codex-spark",
      },
      {
        value: "gpt-5.1-codex",
        label: "gpt-5.1-codex",
        hidden: true,
      },
    ],
  },
  {
    provider: "gemini",
    available: true,
    source: "live",
    models: [
      {
        value: "gemini-3.1-pro-preview",
        label: "pro-3.1",
        reasoning: {
          supported_efforts: ["low", "medium", "high"],
          default_effort: "medium",
        },
      },
    ],
  },
  {
    provider: "qwen",
    available: true,
    source: "live",
    models: [
      {
        value: "coder-model(qwen-oauth)",
        label: "coder-model (qwen-oauth)",
      },
      {
        value: "gpt-5(openai)",
        label: "gpt-5 (openai)",
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

  it("defaults to the strongest available model for a provider", () => {
    expect(getPreferredModelForProvider(catalog, "claude")).toBe("opus");
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

  it("dedupes friendly labels and keeps the newest matching model", () => {
    const models = getModelsForProvider(catalog, "claude");

    expect(models.map((model) => model.label)).toEqual([
      "Opus 4.6",
      "Sonnet 4.5",
      "Haiku 4.5",
    ]);
    expect(models.find((model) => model.label === "Sonnet 4.5")?.value).toBe(
      "claude-sonnet-4-5-20251001",
    );
  });

  it("derives reasoning options and defaults from the provider catalog", () => {
    expect(
      getReasoningOptionsForModel(catalog, "gemini", "gemini-3.1-pro-preview"),
    ).toEqual([
      { value: "auto", label: "Auto" },
      { value: "low", label: "Low" },
      { value: "medium", label: "Medium" },
      { value: "high", label: "High" },
    ]);
    expect(
      getPreferredReasoningEffort(catalog, "gemini", "gemini-3.1-pro-preview"),
    ).toBe("medium");
  });

  it("exposes Claude reasoning levels when the catalog provides them", () => {
    expect(getReasoningOptionsForModel(catalog, "claude", "opus")).toEqual([
      { value: "auto", label: "Auto" },
      { value: "low", label: "Low" },
      { value: "medium", label: "Medium" },
      { value: "high", label: "High" },
      { value: "max", label: "Max" },
    ]);
  });

  it("renders Codex xhigh reasoning as Extra-High", () => {
    expect(getReasoningOptionsForModel(catalog, "codex", "gpt-5.4")).toEqual([
      { value: "auto", label: "Auto" },
      { value: "low", label: "Low" },
      { value: "medium", label: "Medium" },
      { value: "high", label: "High" },
      { value: "xhigh", label: "Extra-High" },
    ]);
  });

  it("renders friendly labels for parsed model identifiers", () => {
    expect(getModelLabel(catalog, "claude", "claude-sonnet-4-5-20251001")).toBe(
      "Sonnet 4.5",
    );
  });

  it("renders shorter codex labels without raw dash-separated ids", () => {
    expect(
      getModelsForProvider(catalog, "codex").map((model) => model.label),
    ).toEqual(["GPT 5.4", "GPT 5.4 Mini", "GPT 5.3 Codex Spark"]);
    expect(getModelLabel(catalog, "codex", "gpt-5.4-mini")).toBe(
      "GPT 5.4 Mini",
    );
  });

  it("labels and orders Droid as a known provider", () => {
    expect(getProviderDisplayName("droid")).toBe("Droid");
    expect(getOrderedProviders(["qwen", "droid", "claude"])).toEqual([
      "claude",
      "qwen",
      "droid",
    ]);
  });

  it("strips Qwen transport suffixes from live catalog labels", () => {
    expect(getModelsForProvider(catalog, "qwen").map((model) => model.label)).toEqual([
      "GPT 5",
      "Coder Model",
    ]);
    expect(getModelLabel(catalog, "qwen", "gpt-5(openai)")).toBe("GPT 5");
    expect(getModelLabel(catalog, "qwen", "coder-model(qwen-oauth)")).toBe(
      "Coder Model",
    );
  });

  it("falls back to the preferred model when the provider is unknown", () => {
    expect(
      getPreferredModelForProvider(catalog, "nonexistent", "some-model"),
    ).toBe("some-model");
    expect(getPreferredModelForProvider(catalog, "nonexistent")).toBeNull();
  });

  it("falls back to the provider's strongest model when the canonical id is unknown", () => {
    expect(
      getPreferredModelForProvider(catalog, "claude", "claude-unknown-zzz"),
    ).toBe("opus");
  });

  it("resolves an unknown provider by echoing the requested model", () => {
    expect(
      resolveProviderModelPair(catalog, {
        provider: "nonexistent",
        model: "foo",
      }),
    ).toEqual({ provider: "nonexistent", model: "foo" });
  });

  it("resolves an unknown canonical id to the provider's strongest model", () => {
    expect(
      resolveProviderModelPair(catalog, {
        provider: "claude",
        model: "claude-unknown-zzz",
      }),
    ).toEqual({ provider: "claude", model: "opus" });
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
