import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { ProvidersModelsSection } from "../ProvidersModelsSection";
import {
  SettingsSectionContext,
  type SettingsSectionContextValue,
} from "../SettingsSectionContext";
import type { UseSettingsReturn } from "../../../../hooks/useSettings";

// Deterministic catalog so the live provider/model selects render real options
// without hitting the network.
vi.mock("../../../../lib/providerModels", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("../../../../lib/providerModels")>();
  return {
    ...actual,
    fetchProviderModelCatalog: vi.fn(async () => [
      {
        provider: "claude",
        available: true,
        source: "static",
        models: [
          { value: "opus", label: "Claude Opus" },
          { value: "sonnet", label: "Claude Sonnet" },
        ],
      },
      {
        provider: "codex",
        available: true,
        source: "static",
        models: [{ value: "gpt-5", label: "GPT-5" }],
      },
      {
        provider: "endpoint:vllm",
        execution_provider: "codex",
        available: true,
        provider_type: "vllm",
        source: "live",
        models: [
          {
            value: "endpoint:vllm",
            label: "Qwen2.5-VL",
            is_default: true,
            input_modalities: ["text", "image"],
          },
          {
            value: "endpoint:vllm/llama-3",
            label: "Llama 3",
            canonical_id: "llama-3",
          },
        ],
      },
      {
        provider: "endpoint:lmstudio",
        execution_provider: "codex",
        available: true,
        provider_type: "lmstudio",
        source: "live",
        models: [
          {
            value: "endpoint:lmstudio/gemma",
            label: "Gemma",
            canonical_id: "gemma",
            input_modalities: ["text"],
          },
        ],
      },
      {
        provider: "endpoint:openrouter",
        execution_provider: "codex",
        available: true,
        source: "live",
        models: [
          {
            value: "endpoint:openrouter/gpt-4",
            label: "GPT-4",
            input_modalities: null,
          },
        ],
      },
    ]),
  };
});

// Minimal schema covering the rows the assertions touch: the shared
// FeatureProfile enum reached through a $ref, mirroring the real DaemonConfig.
const SCHEMA: Record<string, unknown> = {
  $defs: {
    FeatureProfile: {
      enum: ["feature_low", "feature_mid", "feature_high"],
      type: "string",
    },
    RecommendToolsConfig: {
      type: "object",
      properties: {
        profile: { $ref: "#/$defs/FeatureProfile" },
        candidates: { type: "array", items: { type: "string" } },
        enabled: { type: "boolean" },
        prompt_path: { anyOf: [{ type: "string" }, { type: "null" }] },
      },
    },
    GenerationEndpointConfig: {
      type: "object",
      additionalProperties: false,
      properties: {
        protocol: {
          enum: ["openai-compatible", "lmstudio", "ollama", "vllm"],
          type: "string",
        },
        wire_api: {
          enum: ["chat-completions", "responses"],
          type: "string",
        },
        api_base: { type: "string" },
        model: { type: "string" },
        api_key: { anyOf: [{ type: "string" }, { type: "null" }] },
        tool_chat: { type: "boolean" },
      },
    },
    GenerationConfig: {
      type: "object",
      properties: {
        timeout_seconds: { type: "number" },
        candidate_timeout_seconds: { type: "number" },
        cli_candidate_timeout_seconds: { type: "number" },
        endpoints: {
          type: "object",
          additionalProperties: { $ref: "#/$defs/GenerationEndpointConfig" },
        },
      },
    },
    AIConfig: {
      type: "object",
      properties: { generation: { $ref: "#/$defs/GenerationConfig" } },
    },
  },
  type: "object",
  properties: {
    recommend_tools: { $ref: "#/$defs/RecommendToolsConfig" },
    ai: { $ref: "#/$defs/AIConfig" },
  },
};

function optionValues(select: HTMLElement): string[] {
  return within(select)
    .getAllByRole("option")
    .map((option) => (option as HTMLOptionElement).value);
}

function withProtocolEnum(values: string[]): Record<string, unknown> {
  const schema = JSON.parse(JSON.stringify(SCHEMA)) as Record<string, unknown>;
  const defs = schema.$defs as Record<string, Record<string, unknown>>;
  const endpoint = defs.GenerationEndpointConfig as {
    properties: { protocol: { enum: string[] } };
  };
  endpoint.properties.protocol.enum = values;
  return schema;
}

function makeConfigValues(): Record<string, unknown> {
  return {
    recommend_tools: {
      profile: "feature_mid",
      candidates: ["claude/sonnet"],
      enabled: true,
      prompt_path: null,
    },
    tool_summarizer: { profile: "feature_low", candidates: [], enabled: false },
    import_mcp_server: {
      profile: "feature_mid",
      candidates: [],
      enabled: true,
    },
    project_verification_synthesis: {
      profile: "feature_high",
      candidates: [],
      confidence_threshold: 0.7,
    },
    merge_resolution: { profile: "feature_mid", candidates: [] },
    skill_description: { profile: "feature_mid", candidates: [] },
    ai: {
      generation: {
        timeout_seconds: 600,
        candidate_timeout_seconds: 60,
        cli_candidate_timeout_seconds: 150,
        endpoints: {
          lmstudio: {
            protocol: "lmstudio",
            wire_api: "chat-completions",
            api_base: "http://localhost:1234",
            model: "gemma",
            vision_extract: true,
          },
          openrouter: {
            protocol: "openai-compatible",
            wire_api: "responses",
            api_base: "https://openrouter.ai/api/v1",
            model: "gpt-4",
            vision_extract: true,
          },
          vllm: {
            protocol: "vllm",
            wire_api: "chat-completions",
            api_base: "http://127.0.0.1:8000/v1",
            model: "auto",
            vision_extract: true,
          },
        },
        profile_defaults: { feature_mid: ["claude/sonnet"] },
      },
    },
    context_window_overrides: { opus: 1000000 },
  };
}

function makeClientSettings(): UseSettingsReturn {
  return {
    settings: { model: "opus" },
    updateModel: vi.fn(),
  } as unknown as UseSettingsReturn;
}

function makeContext(
  overrides: Partial<SettingsSectionContextValue> = {},
): SettingsSectionContextValue {
  return {
    schema: SCHEMA,
    configValues: makeConfigValues(),
    secretKeys: [],
    isLoading: false,
    saveConfig: vi.fn(async () => ({ ok: true })),
    registerDirtyGuard: () => () => {},
    clientSettings: makeClientSettings(),
    providerSelection: {
      selectedProvider: "claude",
      onSelectProvider: vi.fn(),
    },
    ...overrides,
  };
}

function renderSection(ctx: SettingsSectionContextValue) {
  return render(
    <SettingsSectionContext.Provider value={ctx}>
      <ProvidersModelsSection />
    </SettingsSectionContext.Provider>,
  );
}

async function waitForProviderCatalog() {
  const provider = screen.getByLabelText(
    "Default provider",
  ) as HTMLSelectElement;
  await waitFor(() =>
    expect(provider.querySelector('option[value="codex"]')).not.toBeNull(),
  );
}

describe("ProvidersModelsSection", () => {
  it("wires the live provider and model selects to App state and client settings", async () => {
    const ctx = makeContext();
    renderSection(ctx);

    const provider = screen.getByLabelText(
      "Default provider",
    ) as HTMLSelectElement;
    expect(provider).toHaveValue("claude");
    // Provider options derive from the (async) catalog.
    await waitForProviderCatalog();
    fireEvent.change(provider, { target: { value: "codex" } });
    expect(ctx.providerSelection?.onSelectProvider).toHaveBeenCalledWith(
      "codex",
    );

    // Model reads from the shared useSettings instance.
    expect(screen.getByLabelText("Default model")).toHaveValue("opus");
  });

  it("renders a feature profile select bound to nested config with enum options", async () => {
    renderSection(makeContext());
    await waitForProviderCatalog();

    const profile = screen.getByLabelText("Tool recommendation profile");
    // Proves nested configValues are read by dotted path (pickPaths traversal).
    expect(profile).toHaveValue("feature_mid");
    expect(within(profile).getAllByRole("option")).toHaveLength(3);
  });

  it("reads feature candidates, toggles, generation numbers, and maps from nested config", async () => {
    renderSection(makeContext());
    await waitForProviderCatalog();

    expect(
      screen.getByLabelText("Tool recommendation candidates item 1"),
    ).toHaveValue("claude/sonnet");
    expect(
      screen.getByRole("switch", { name: "Tool recommendation enabled" }),
    ).toBeChecked();
    expect(screen.getByLabelText("Generation timeout (seconds)")).toHaveValue(
      600,
    );
    expect(
      screen.getByLabelText("Verification synthesis confidence threshold"),
    ).toHaveValue(0.7);

    // Structured map editors surface their nested entries.
    expect(screen.getByLabelText("Context window override key 1")).toHaveValue(
      "opus",
    );
    expect(screen.getByLabelText("Generation endpoint key 1")).toHaveValue(
      "lmstudio",
    );
    expect(screen.getByLabelText("API base (lmstudio)")).toHaveValue(
      "http://localhost:1234",
    );
    expect(screen.getByLabelText("Profile default key 1")).toHaveValue(
      "feature_mid",
    );
  });

  it("persists an edited config row through the section draft Save", async () => {
    const ctx = makeContext();
    renderSection(ctx);

    fireEvent.change(screen.getByLabelText("Tool recommendation profile"), {
      target: { value: "feature_high" },
    });

    const save = screen.getByRole("button", { name: "Save" });
    await waitFor(() => expect(save).toBeEnabled());
    fireEvent.click(save);

    await waitFor(() => expect(ctx.saveConfig).toHaveBeenCalledTimes(1));
    expect(ctx.saveConfig).toHaveBeenCalledWith(
      expect.objectContaining({ "recommend_tools.profile": "feature_high" }),
    );
  });

  it("decodes stored context-window keys for display and re-encodes on save", async () => {
    const ctx = makeContext({
      configValues: {
        ...makeConfigValues(),
        // `context_window_overrides.{model_match}` keys are dynamic
        // segments, stored encoded ("gpt-4.1" contains a dot).
        context_window_overrides: {
          "gpt-4%2E1": 200000,
          "gpt-4.1": 100000,
        },
      },
    });
    renderSection(ctx);

    const key = screen.getByLabelText("Context window override key 1");
    expect(key).toHaveValue("gpt-4.1");

    fireEvent.change(key, { target: { value: "gpt-4.2" } });
    const save = screen.getByRole("button", { name: "Save" });
    await waitFor(() => expect(save).toBeEnabled());
    fireEvent.click(save);

    await waitFor(() => expect(ctx.saveConfig).toHaveBeenCalledTimes(1));
    const payload = vi.mocked(ctx.saveConfig).mock.calls[0][0];
    expect(payload["context_window_overrides"]).toEqual({
      "gpt-4%2E2": 200000,
      "gpt-4.1": 100000,
    });
  });

  it("degrades gracefully when client settings and provider selection are absent", async () => {
    renderSection(
      makeContext({ clientSettings: undefined, providerSelection: undefined }),
    );

    expect(screen.queryByLabelText("Default provider")).toBeNull();
    expect(
      screen.getByText(/Model and provider selection is unavailable/i),
    ).toBeInTheDocument();
    // The config-backed controls still render without the client surface.
    expect(screen.getByLabelText("Tool recommendation profile")).toHaveValue(
      "feature_mid",
    );
    await waitFor(() =>
      expect(screen.getByLabelText("Capabilities (vllm)")).toBeTruthy(),
    );
  });

  it("lists protocol options from the daemon schema including vllm", async () => {
    renderSection(makeContext());
    await waitForProviderCatalog();

    const protocol = screen.getByLabelText("Protocol (lmstudio)");
    expect(optionValues(protocol)).toEqual([
      "openai-compatible",
      "lmstudio",
      "ollama",
      "vllm",
    ]);
  });

  it("derives protocol options from the schema rather than a hardcoded list", async () => {
    const schema = withProtocolEnum(["vllm", "openai-compatible"]);
    renderSection(
      makeContext({
        schema,
        configValues: {
          ...makeConfigValues(),
          ai: {
            generation: {
              timeout_seconds: 600,
              candidate_timeout_seconds: 60,
              cli_candidate_timeout_seconds: 150,
              endpoints: {
                local: {
                  protocol: "vllm",
                  wire_api: "chat-completions",
                  api_base: "http://127.0.0.1:8000/v1",
                  model: "auto",
                },
              },
              profile_defaults: {},
            },
          },
        },
      }),
    );
    await waitForProviderCatalog();

    expect(optionValues(screen.getByLabelText("Protocol (local)"))).toEqual([
      "vllm",
      "openai-compatible",
    ]);
  });

  it("exposes wire_api for openai-compatible and pins chat-completions for vllm", async () => {
    renderSection(makeContext());
    await waitForProviderCatalog();

    const wireApi = screen.getByLabelText("Wire API (openrouter)");
    expect(wireApi).toHaveValue("responses");
    expect(optionValues(wireApi)).toEqual(["chat-completions", "responses"]);
    expect(screen.queryByLabelText("Wire API (vllm)")).toBeNull();
    expect(screen.queryByLabelText("Wire API (lmstudio)")).toBeNull();

    fireEvent.change(screen.getByLabelText("Protocol (openrouter)"), {
      target: { value: "vllm" },
    });
    expect(screen.queryByLabelText("Wire API (openrouter)")).toBeNull();
  });

  it("writes chat-completions on a vllm switch and restores wire_api choices after switching back", async () => {
    const ctx = makeContext();
    renderSection(ctx);
    await waitForProviderCatalog();

    fireEvent.change(screen.getByLabelText("Protocol (openrouter)"), {
      target: { value: "vllm" },
    });
    const save = screen.getByRole("button", { name: "Save" });
    await waitFor(() => expect(save).toBeEnabled());
    fireEvent.click(save);
    await waitFor(() => expect(ctx.saveConfig).toHaveBeenCalledTimes(1));

    const vllmPayload = vi.mocked(ctx.saveConfig).mock.calls[0][0] as Record<
      string,
      Record<string, { protocol?: string; wire_api?: string }>
    >;
    expect(vllmPayload["ai.generation.endpoints"].openrouter).toEqual(
      expect.objectContaining({
        protocol: "vllm",
        wire_api: "chat-completions",
      }),
    );

    fireEvent.change(screen.getByLabelText("Protocol (openrouter)"), {
      target: { value: "openai-compatible" },
    });
    const restored = screen.getByLabelText("Wire API (openrouter)");
    expect(optionValues(restored)).toEqual(["chat-completions", "responses"]);
    expect(restored).toHaveValue("chat-completions");

    fireEvent.change(restored, { target: { value: "responses" } });
    await waitFor(() => expect(save).toBeEnabled());
    fireEvent.click(save);
    await waitFor(() => expect(ctx.saveConfig).toHaveBeenCalledTimes(2));

    const restoredPayload = vi.mocked(ctx.saveConfig).mock
      .calls[1][0] as Record<
      string,
      Record<string, { protocol?: string; wire_api?: string }>
    >;
    expect(restoredPayload["ai.generation.endpoints"].openrouter).toEqual(
      expect.objectContaining({
        protocol: "openai-compatible",
        wire_api: "responses",
      }),
    );
  });

  it("neither renders nor submits vision_extract for any protocol", async () => {
    const ctx = makeContext();
    renderSection(ctx);
    await waitForProviderCatalog();

    expect(screen.queryAllByLabelText(/vision extract/i)).toHaveLength(0);
    expect(screen.queryAllByText(/vision extract/i)).toHaveLength(0);

    fireEvent.change(screen.getByLabelText("Model (lmstudio)"), {
      target: { value: "gemma-2" },
    });
    const save = screen.getByRole("button", { name: "Save" });
    await waitFor(() => expect(save).toBeEnabled());
    fireEvent.click(save);
    await waitFor(() => expect(ctx.saveConfig).toHaveBeenCalledTimes(1));

    const payload = vi.mocked(ctx.saveConfig).mock.calls[0][0] as Record<
      string,
      Record<string, Record<string, unknown>>
    >;
    const endpoints = payload["ai.generation.endpoints"];
    expect(Object.keys(endpoints).sort()).toEqual([
      "lmstudio",
      "openrouter",
      "vllm",
    ]);
    for (const endpoint of Object.values(endpoints)) {
      expect(endpoint).not.toHaveProperty("vision_extract");
    }
    expect(endpoints.lmstudio.model).toBe("gemma-2");
  });

  it("renders Text/Image chips on generation endpoint rows from catalog modalities", async () => {
    renderSection(makeContext());
    await waitForProviderCatalog();

    const vllmChips = await waitFor(() => {
      const group = screen.getByLabelText("Capabilities (vllm)");
      return [...group.querySelectorAll(".capability-chip")].map(
        (element) => element.textContent,
      );
    });
    expect(vllmChips).toEqual(["Text", "Image"]);

    const lmstudioChips = [
      ...screen
        .getByLabelText("Capabilities (lmstudio)")
        .querySelectorAll(".capability-chip"),
    ].map((element) => element.textContent);
    expect(lmstudioChips).toEqual(["Text"]);

    expect(screen.queryByLabelText("Capabilities (openrouter)")).toBeNull();
  });
});
