import {
  afterEach,
  beforeEach,
  describe,
  expect,
  expectTypeOf,
  it,
  vi,
} from "vitest";

import {
  clearProviderModelCache,
  fetchProviderModelCatalog,
  getModelLabel,
  getModelsForProvider,
  getModelsForSelection,
  isHiddenProvider,
  modelSupportsImageInput,
  getOrderedProviders,
  getPreferredModelForProvider,
  getProviderDisplayName,
  getProviderDisplayNameFromEntry,
  getPreferredReasoningEffort,
  getReasoningOptionsForModel,
  resolveProviderModelPair,
  type ProviderModelEntry,
  type ProviderModelOption,
} from "../providerModels";
import { isProviderModelEntry } from "../providerModelCatalog";

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
    provider: "qwen",
    available: true,
    source: "live",
    models: [
      {
        value: "coder-model(qwen-oauth)",
        label: "Qwen Coder (OAuth)",
      },
      {
        value: "gpt-5(openai)",
        label: "gpt-5",
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

  it("ranks Claude model family ahead of version", () => {
    const claudeCatalog: ProviderModelEntry[] = [
      {
        provider: "claude",
        available: true,
        source: "live",
        models: [
          { value: "opus", label: "Opus", canonical_id: "claude-opus-4-1" },
          { value: "haiku", label: "Haiku", canonical_id: "claude-haiku-4-5" },
        ],
      },
    ];

    expect(getPreferredModelForProvider(claudeCatalog, "claude")).toBe("opus");
  });

  it("recognizes Fable as the strongest Claude family", () => {
    const claudeCatalog: ProviderModelEntry[] = [
      {
        provider: "claude",
        available: true,
        source: "live",
        models: [
          { value: "fable", label: "Fable", canonical_id: "claude-fable-5" },
          { value: "opus", label: "Opus", canonical_id: "claude-opus-5" },
        ],
      },
    ];

    expect(getPreferredModelForProvider(claudeCatalog, "claude")).toBe("fable");
  });

  it("prefers the provider-declared default over heuristic ranking", () => {
    const claudeCatalog: ProviderModelEntry[] = [
      {
        provider: "claude",
        available: true,
        source: "live",
        models: [
          { value: "fable", label: "Fable", canonical_id: "claude-fable-5" },
          {
            value: "haiku",
            label: "Haiku",
            canonical_id: "claude-haiku-4-5",
            is_default: true,
          },
        ],
      },
    ];

    expect(getPreferredModelForProvider(claudeCatalog, "claude")).toBe("haiku");
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

  it("keeps distinct models whose friendly labels collide", () => {
    const models = getModelsForProvider(catalog, "claude");

    expect(models.map((model) => model.label)).toEqual([
      "Opus 4.6",
      "Sonnet 4.5",
      "Sonnet 4.5",
      "Haiku 4.5",
    ]);
    expect(
      models
        .filter((model) => model.label === "Sonnet 4.5")
        .map((model) => model.value),
    ).toEqual(["claude-sonnet-4-5-20251001", "claude-sonnet-4-5-20250901"]);
  });

  it("honors the provider default ahead of inferred model strength", () => {
    const defaultCatalog: ProviderModelEntry[] = [
      {
        provider: "codex",
        available: true,
        source: "live",
        models: [
          { value: "gpt-5.4", label: "gpt-5.4" },
          { value: "gpt-5.4-mini", label: "gpt-5.4-mini", is_default: true },
        ],
      },
    ];

    expect(getModelsForProvider(defaultCatalog, "codex")[0]?.value).toBe(
      "gpt-5.4-mini",
    );
    expect(getPreferredModelForProvider(defaultCatalog, "codex")).toBe(
      "gpt-5.4-mini",
    );
  });

  it("only applies the Pro tier to Gemini model tokens", () => {
    const qwenCatalog: ProviderModelEntry[] = [
      {
        provider: "qwen",
        available: true,
        source: "live",
        models: [
          { value: "improved-99", label: "improved-99" },
          { value: "gemini-2.5-pro", label: "gemini-2.5-pro" },
        ],
      },
    ];

    expect(getModelsForProvider(qwenCatalog, "qwen")[0]?.value).toBe(
      "gemini-2.5-pro",
    );
  });

  it("derives reasoning options and defaults from the provider catalog", () => {
    expect(getReasoningOptionsForModel(catalog, "codex", "gpt-5.4")).toEqual([
      { value: "auto", label: "Auto" },
      { value: "low", label: "Low" },
      { value: "medium", label: "Med" },
      { value: "high", label: "High" },
      { value: "xhigh", label: "XHigh" },
    ]);
    expect(getPreferredReasoningEffort(catalog, "codex", "gpt-5.4")).toBe(
      "medium",
    );
  });

  it("exposes Claude reasoning levels when the catalog provides them", () => {
    expect(getReasoningOptionsForModel(catalog, "claude", "opus")).toEqual([
      { value: "auto", label: "Auto" },
      { value: "low", label: "Low" },
      { value: "medium", label: "Med" },
      { value: "high", label: "High" },
      { value: "max", label: "Max" },
    ]);
  });

  it("renders Codex xhigh reasoning as XHigh", () => {
    expect(getReasoningOptionsForModel(catalog, "codex", "gpt-5.4")).toEqual([
      { value: "auto", label: "Auto" },
      { value: "low", label: "Low" },
      { value: "medium", label: "Med" },
      { value: "high", label: "High" },
      { value: "xhigh", label: "XHigh" },
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

  it("labels and orders Codex flavor families deterministically", () => {
    const flavorCatalog: ProviderModelEntry[] = [
      {
        provider: "codex",
        available: true,
        source: "live",
        models: [
          { value: "gpt-5.6-terra", label: "gpt-5.6-terra" },
          { value: "gpt-5.6-mini", label: "gpt-5.6-mini" },
          { value: "gpt-5.6-luna", label: "gpt-5.6-luna" },
          { value: "gpt-5.6-codex-spark", label: "gpt-5.6-codex-spark" },
          { value: "gpt-5.6-sol", label: "gpt-5.6-sol" },
          { value: "gpt-5.6-codex", label: "gpt-5.6-codex" },
          { value: "gpt-5.6", label: "gpt-5.6" },
        ],
      },
    ];

    expect(
      getModelsForProvider(flavorCatalog, "codex").map((model) => model.label),
    ).toEqual([
      "GPT 5.6",
      "GPT 5.6 Luna",
      "GPT 5.6 Sol",
      "GPT 5.6 Terra",
      "GPT 5.6 Codex",
      "GPT 5.6 Mini",
      "GPT 5.6 Codex Spark",
    ]);
  });

  it("preserves labels for local selectors routed through Codex", () => {
    const localCatalog: ProviderModelEntry[] = [
      {
        provider: "codex",
        available: true,
        source: "live",
        models: [
          {
            value: "endpoint:lm-studio/gpt-5.6-sol",
            label: "LM Studio: Sol Dev",
          },
        ],
      },
    ];

    expect(getModelsForProvider(localCatalog, "codex")).toEqual([
      {
        value: "endpoint:lm-studio/gpt-5.6-sol",
        label: "LM Studio: Sol Dev",
        match_identifiers: ["endpoint:lm-studio/gpt-5.6-sol"],
        _parsed: {
          displayLabel: "LM Studio: Sol Dev",
          strengthRank: 0,
          versionParts: [],
          releaseDate: null,
        },
      },
    ]);
    expect(
      getModelLabel(localCatalog, "codex", "endpoint:lm-studio/gpt-5.6-sol"),
    ).toBe("LM Studio: Sol Dev");
  });

  it("labels and sorts known providers alphabetically by display name", () => {
    expect(getProviderDisplayName("droid")).toBe("Droid");
    expect(getOrderedProviders(["qwen", "droid", "claude", "codex"])).toEqual([
      "claude",
      "codex",
      "droid",
      "qwen",
    ]);
  });

  it("humanizes endpoint provider ids instead of raw scheme casing (#20047)", () => {
    expect(getProviderDisplayName("endpoint:lm-studio")).toBe("LM Studio");
    expect(getProviderDisplayName("endpoint:ollama")).toBe("Ollama");
    expect(getProviderDisplayName("endpoint:vllm")).toBe("vLLM");
    expect(getProviderDisplayName("endpoint:my-box")).toBe("My Box");
  });

  it("handles Grok, AGY, and provider metadata fields", () => {
    const entries: ProviderModelEntry[] = [
      {
        provider: "grok",
        available: true,
        source: "live",
        display_name: "Grok",
        installed: true,
        supports_web_chat: true,
        supports_agent_spawn: true,
        models: [{ value: "grok-build", label: "Grok Build" }],
      },
      {
        provider: "agy",
        available: false,
        source: "unsupported",
        display_name: "AGY",
        installed: true,
        supports_web_chat: false,
        supports_agent_spawn: false,
        unavailable_reason: "No documented machine transport",
        models: [],
      },
    ];

    expect(getProviderDisplayName("grok")).toBe("Grok");
    expect(getProviderDisplayName("agy")).toBe("AGY");
    expect(getProviderDisplayNameFromEntry(entries[1])).toBe("AGY");
    expect(getOrderedProviders(["qwen", "agy", "grok"])).toEqual([
      "agy",
      "grok",
      "qwen",
    ]);
    expect(
      getModelsForProvider(entries, "grok").map(({ value, label }) => ({
        value,
        label,
      })),
    ).toEqual([{ value: "grok-build", label: "Grok Build" }]);
    expect(entries[1].source).toBe("unsupported");
  });

  it("uses curated backend Qwen labels and humanizes raw-id labels", () => {
    expect(
      getModelsForProvider(catalog, "qwen").map((model) => model.label),
    ).toEqual(["GPT 5", "Qwen Coder (OAuth)"]);
    expect(getModelLabel(catalog, "qwen", "gpt-5(openai)")).toBe("GPT 5");
    expect(getModelLabel(catalog, "qwen", "coder-model(qwen-oauth)")).toBe(
      "Qwen Coder (OAuth)",
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

  it("rejects a stale local selector instead of silently switching providers", () => {
    expect(
      getPreferredModelForProvider(catalog, "codex", "local:openrouter/kimi"),
    ).toBe("gpt-5.4");
    expect(
      resolveProviderModelPair(catalog, {
        provider: "codex",
        model: "local:openrouter/kimi",
      }),
    ).toEqual({ provider: "codex", model: "gpt-5.4" });
  });

  it("gates image input from endpoint model capabilities", () => {
    const responseCatalog: ProviderModelEntry[] = [
      {
        provider: "codex",
        available: true,
        source: "live",
        models: [
          {
            value: "endpoint:openrouter/moonshotai/kimi-k3",
            label: "OpenRouter: moonshotai/kimi-k3",
            input_modalities: ["text"],
          },
        ],
      },
    ];

    expect(
      modelSupportsImageInput(
        responseCatalog,
        "codex",
        "endpoint:openrouter/moonshotai/kimi-k3",
      ),
    ).toBe(false);
  });

  it("follows capability chips for image eligibility on endpoint-backed options", () => {
    const mixedCatalog: ProviderModelEntry[] = [
      {
        provider: "codex",
        available: true,
        source: "live",
        models: [
          { value: "gpt-5.4", label: "GPT-5.4" },
          {
            value: "endpoint:openrouter/moonshotai/kimi-k3",
            label: "OpenRouter: moonshotai/kimi-k3",
            input_modalities: null,
          } as ProviderModelOption,
        ],
      },
      {
        provider: "endpoint:generic",
        execution_provider: "codex",
        available: true,
        source: "live",
        models: [
          {
            value: "endpoint:generic/llama",
            label: "Llama",
          },
        ],
      },
      {
        provider: "endpoint:vllm",
        execution_provider: "codex",
        available: true,
        source: "live",
        models: [
          {
            value: "endpoint:vllm",
            label: "Qwen2.5-VL",
            is_default: true,
            input_modalities: ["text", "image"],
          },
        ],
      },
    ];

    expect(
      modelSupportsImageInput(
        mixedCatalog,
        "codex",
        "endpoint:openrouter/moonshotai/kimi-k3",
      ),
    ).toBe(false);
    expect(modelSupportsImageInput(mixedCatalog, "codex", "gpt-5.4")).toBe(
      true,
    );
    expect(
      modelSupportsImageInput(mixedCatalog, "codex", "endpoint:generic/llama"),
    ).toBe(false);
    expect(
      modelSupportsImageInput(mixedCatalog, "codex", "endpoint:vllm"),
    ).toBe(true);
  });

  it("maps a source-less matrix response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          providers: [
            {
              provider: "codex",
              execution_provider: "codex",
              available: true,
              startup_error: null,
              display_name: "Codex",
              installed: true,
              deprecated: false,
              deprecation_message: null,
              supports_web_chat: true,
              supports_agent_spawn: true,
              unavailable_reason: null,
              models: [
                {
                  canonical_model: "gpt-5.4",
                  display_name: "GPT-5.4",
                  aliases: [],
                  available: true,
                  hidden: false,
                  is_default: true,
                  context_length: {
                    value: 200_000,
                    source: "provider-catalog",
                  },
                  max_output_tokens: {
                    value: 100_000,
                    source: "provider-catalog",
                  },
                  latency_class: null,
                  reasoning: {
                    status: "known",
                    supported_efforts: ["low", "medium", "high"],
                    default_effort: "medium",
                  },
                  input_modalities: ["text", "image"],
                  supports_tools: true,
                  routes: {},
                  provenance: {},
                },
              ],
              refresh: { generation: 4, sources: [] },
            },
          ],
        }),
      }),
    );

    await expect(fetchProviderModelCatalog()).resolves.toEqual([
      {
        provider: "codex",
        execution_provider: "codex",
        available: true,
        startup_error: null,
        display_name: "Codex",
        installed: true,
        deprecated: false,
        deprecation_message: null,
        supports_web_chat: true,
        supports_agent_spawn: true,
        unavailable_reason: null,
        models: [
          {
            value: "gpt-5.4",
            label: "GPT-5.4",
            canonical_id: "gpt-5.4",
            hidden: false,
            is_default: true,
            context_length: 200_000,
            context_length_source: "provider-catalog",
            input_modalities: ["text", "image"],
            supports_tools: true,
            reasoning: {
              supported_efforts: ["low", "medium", "high"],
              default_effort: "medium",
            },
            routes: {},
          },
        ],
        refresh: { generation: 4, sources: [] },
      },
    ]);
  });

  it("exposes_routes_and_refresh", async () => {
    const routes = {
      standard: {
        selector: "gpt-5.4",
        available: true,
        usage_multiplier: "1",
        throughput_multiplier: null,
        latency_class: null,
        activations: [
          { kind: "model_selector", surface: "spawn-cli", params: {} },
        ],
      },
      fast: {
        selector: "gpt-5.4-fast",
        available: true,
        usage_multiplier: "5",
        throughput_multiplier: "2",
        latency_class: "fast",
        activations: [
          { kind: "model_selector", surface: "spawn-cli", params: {} },
        ],
      },
    };
    const refresh = {
      generation: 7,
      sources: [{ source_key: "codex-app-server", state: "ok" }],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          providers: [
            {
              provider: "codex",
              available: true,
              models: [
                {
                  canonical_model: "gpt-5.4",
                  display_name: "GPT-5.4",
                  aliases: [],
                  available: true,
                  hidden: false,
                  is_default: false,
                  context_length: { value: null, source: "unknown" },
                  max_output_tokens: { value: null, source: "unknown" },
                  latency_class: null,
                  reasoning: {
                    status: "unknown",
                    supported_efforts: null,
                    default_effort: null,
                  },
                  input_modalities: null,
                  supports_tools: null,
                  routes,
                  provenance: {},
                },
              ],
              refresh,
            },
          ],
        }),
      }),
    );

    const result = await fetchProviderModelCatalog();

    expect(result[0]?.models[0]?.routes).toBe(routes);
    expect(result[0]?.refresh).toBe(refresh);
    expectTypeOf(result[0]?.models[0]?.routes?.fast?.selector).toEqualTypeOf<
      string | undefined
    >();
    expectTypeOf(result[0]?.refresh?.generation).toEqualTypeOf<
      number | undefined
    >();
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

  it("returns the stale cached catalog when a refresh fails", async () => {
    const cachedCatalog = [
      { provider: "claude", available: true, source: "live", models: [] },
    ];
    const fetchSpy = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ providers: cachedCatalog }),
      })
      .mockRejectedValueOnce(new Error("offline"));
    vi.stubGlobal("fetch", fetchSpy);
    vi.spyOn(Date, "now")
      .mockReturnValueOnce(0)
      .mockReturnValueOnce(5 * 60 * 1000 + 1);
    vi.spyOn(console, "debug").mockImplementation(() => {});

    await expect(fetchProviderModelCatalog()).resolves.toEqual(cachedCatalog);
    await expect(fetchProviderModelCatalog()).resolves.toEqual(cachedCatalog);
    expect(fetchSpy).toHaveBeenCalledTimes(2);
  });

  it("filters malformed provider catalog entries before caching", async () => {
    const validEntry = {
      provider: "claude",
      available: true,
      source: "live",
      models: [{ value: "opus", label: "Opus" }],
    };
    const fetchSpy = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        providers: [
          null,
          { provider: "codex", available: true, source: "live" },
          {
            provider: "qwen",
            available: true,
            source: "live",
            models: [null],
          },
          validEntry,
        ],
      }),
    });
    vi.stubGlobal("fetch", fetchSpy);

    await expect(fetchProviderModelCatalog()).resolves.toEqual([validEntry]);
    await expect(fetchProviderModelCatalog()).resolves.toEqual([validEntry]);
    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });

  it("keeps explicit unknown context metadata and rejects retired sources", async () => {
    const unknownEntry = {
      provider: "codex",
      available: true,
      source: "live",
      models: [
        {
          value: "future-model",
          label: "Future Model",
          context_length: null,
          context_length_source: "unknown",
        },
      ],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          providers: [
            unknownEntry,
            {
              ...unknownEntry,
              provider: "legacy",
              models: [
                {
                  value: "legacy-model",
                  label: "Legacy Model",
                  context_length: 200_000,
                  context_length_source: "static_default",
                },
              ],
            },
          ],
        }),
      }),
    );

    await expect(fetchProviderModelCatalog()).resolves.toEqual([unknownEntry]);
  });

  async function testAgyProviderVisibility() {
    const codexEntry = {
      provider: "codex",
      available: true,
      source: "live",
      models: [{ value: "gpt-5.4", label: "GPT-5.4" }],
    };
    const agyEntry = {
      provider: "agy",
      available: true,
      source: "live",
      models: [{ value: "agy-1", label: "AGY 1" }],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          providers: [codexEntry, agyEntry],
        }),
      }),
    );

    await expect(fetchProviderModelCatalog()).resolves.toEqual([
      codexEntry,
      agyEntry,
    ]);
    expect(isHiddenProvider("agy")).toBe(false);
    expect(isHiddenProvider("AGY")).toBe(false);
    expect(isHiddenProvider("codex")).toBe(false);
    expect(isHiddenProvider(null)).toBe(false);
    expect(getProviderDisplayName("agy")).toBe("AGY");
  }

  it("keeps available AGY catalog entries and display support", async () => {
    await expect(testAgyProviderVisibility()).resolves.toBeUndefined();
  });

  it("drops unavailable AGY catalog entries", async () => {
    const codexEntry = {
      provider: "codex",
      available: true,
      source: "live",
      models: [{ value: "gpt-5.4", label: "GPT-5.4" }],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          providers: [
            codexEntry,
            {
              provider: "agy",
              available: false,
              source: "unsupported",
              models: [],
            },
          ],
        }),
      }),
    );

    await expect(fetchProviderModelCatalog()).resolves.toEqual([codexEntry]);
  });

  it("preserves nonblank execution-provider metadata and catalog identity", async () => {
    const validEntry: ProviderModelEntry = {
      provider: "endpoint:lm-studio",
      execution_provider: "codex",
      available: true,
      source: "live",
      models: [{ value: "endpoint:lm-studio/qwen3", label: "Qwen3" }],
    };
    expectTypeOf(validEntry.execution_provider).toEqualTypeOf<
      string | undefined
    >();
    const fetchSpy = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        providers: [
          validEntry,
          { ...validEntry, provider: "endpoint:blank", execution_provider: "" },
          {
            ...validEntry,
            provider: "endpoint:whitespace",
            execution_provider: "   ",
          },
          {
            ...validEntry,
            provider: "endpoint:null",
            execution_provider: null,
          },
          { ...validEntry, provider: "endpoint:number", execution_provider: 1 },
        ],
      }),
    });
    vi.stubGlobal("fetch", fetchSpy);

    await expect(fetchProviderModelCatalog()).resolves.toEqual([validEntry]);
    expect(validEntry.provider).toBe("endpoint:lm-studio");
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

  it("preserves a Responses endpoint selector grouped under Codex", () => {
    const endpointSelector = "endpoint:openrouter/moonshotai/kimi-k3";
    const endpointCatalog: ProviderModelEntry[] = [
      {
        provider: "codex",
        available: true,
        source: "static",
        models: [
          { value: "gpt-5.4", label: "GPT 5.4", is_default: true },
          {
            value: endpointSelector,
            label: "OpenRouter: moonshotai/kimi-k3",
            input_modalities: ["text", "image"],
          },
        ],
      },
    ];

    expect(
      getModelsForSelection(endpointCatalog, "codex", endpointSelector),
    ).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          value: endpointSelector,
          label: "OpenRouter: Moonshotai/Kimi K3",
        }),
      ]),
    );
    expect(
      getPreferredModelForProvider(endpointCatalog, "codex", endpointSelector),
    ).toBe(endpointSelector);
    expect(getModelLabel(endpointCatalog, "codex", endpointSelector)).toBe(
      "OpenRouter: Moonshotai/Kimi K3",
    );
    expect(
      resolveProviderModelPair(endpointCatalog, {
        provider: "codex",
        model: endpointSelector,
      }),
    ).toEqual({ provider: "codex", model: endpointSelector });
  });
});

describe("null input_modalities in /api/providers/models", () => {
  const localEntry = {
    provider: "endpoint:generic",
    execution_provider: "codex",
    available: true,
    display_name: "OpenAI Compatible",
    provider_type: "openai-compatible",
    source: "live",
    models: [
      {
        value: "endpoint:generic/llama",
        label: "Llama",
        canonical_id: "llama",
        input_modalities: null,
      },
    ],
  };
  const codexEntry = {
    provider: "codex",
    available: true,
    source: "live",
    models: [
      {
        value: "gpt-5.4",
        label: "GPT-5.4",
        input_modalities: ["text", "image"],
      },
      {
        value: "endpoint:openrouter/kimi-k3",
        label: "OpenRouter: kimi-k3",
        input_modalities: null,
      },
    ],
  };

  it("accepts local and codex entries whose models carry null modalities", () => {
    expect(isProviderModelEntry(localEntry)).toBe(true);
    expect(isProviderModelEntry(codexEntry)).toBe(true);
  });

  it("keeps those providers in the fetched catalog and normalizes null to absent", async () => {
    clearProviderModelCache();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({ providers: [localEntry, codexEntry] }),
      }),
    );

    const result = await fetchProviderModelCatalog();

    expect(result.map((entry) => entry.provider)).toEqual([
      "endpoint:generic",
      "codex",
    ]);
    const llama = getModelsForProvider(result, "endpoint:generic")[0];
    expect(llama).toBeDefined();
    expect("input_modalities" in (llama ?? {})).toBe(false);
    expect(modelSupportsImageInput(result, "codex", "gpt-5.4")).toBe(true);
    expect(
      modelSupportsImageInput(result, "codex", "endpoint:openrouter/kimi-k3"),
    ).toBe(false);
  });
});
