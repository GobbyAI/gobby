import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ProviderPicker } from "../ProviderPicker";
import { clearProviderModelCache } from "../../../lib/providerModels";

function buildCatalog(qwenModels: { value: string; label: string }[] = []) {
  return {
    providers: [
      {
        provider: "claude",
        available: true,
        models: [
          { value: "opus", label: "Opus" },
          { value: "sonnet", label: "Sonnet" },
        ],
        source: "static",
      },
      {
        provider: "qwen",
        available: true,
        models: qwenModels,
        source: "live",
      },
      {
        provider: "codex",
        available: true,
        models: [
          { value: "gpt-5.4", label: "codex-5.4" },
          { value: "gpt-5.4-mini", label: "mini-5.4" },
          { value: "gpt-5.3-codex", label: "codex-5.3" },
        ],
        source: "static",
      },
      {
        provider: "droid",
        available: true,
        models: [{ value: "claude-opus-4-7", label: "Claude Opus 4.7" }],
        source: "static",
      },
    ],
  };
}

function buildEndpointCatalog() {
  const catalog = buildCatalog();
  return {
    providers: catalog.providers.map((provider) =>
      provider.provider === "codex"
        ? {
            ...provider,
            models: [
              ...provider.models,
              {
                value: "endpoint:openrouter/moonshotai/kimi-k3",
                label: "OpenRouter: moonshotai/kimi-k3",
                input_modalities: ["text", "image"],
              },
            ],
          }
        : provider,
    ),
  };
}

function buildLocalEndpointCatalog(overrides: Record<string, unknown> = {}) {
  const catalog = buildCatalog();
  return {
    providers: [
      ...catalog.providers,
      {
        provider: "endpoint:studio",
        execution_provider: "codex",
        available: true,
        display_name: "LM Studio",
        provider_type: "lmstudio",
        supports_web_chat: true,
        models: [
          {
            value: "endpoint:studio/mistralai/devstral-small",
            label: "Devstral Small",
            canonical_id: "mistralai/devstral-small",
          },
        ],
        source: "live",
        ...overrides,
      },
    ],
  };
}

function buildCapabilityCatalog() {
  const catalog = buildCatalog();
  return {
    providers: [
      ...catalog.providers,
      {
        provider: "endpoint:vllm",
        execution_provider: "codex",
        available: true,
        display_name: "vLLM",
        provider_type: "vllm",
        supports_web_chat: true,
        source: "live",
        models: [
          {
            // Default alias after model: auto on a single-model endpoint —
            // modalities copied from 1.2 resolution, not a literal id match.
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
        provider: "endpoint:studio",
        execution_provider: "codex",
        available: true,
        display_name: "LM Studio",
        provider_type: "lmstudio",
        supports_web_chat: true,
        source: "live",
        models: [
          {
            value: "endpoint:studio/qwen-vl",
            label: "Qwen VL",
            canonical_id: "qwen-vl",
            input_modalities: ["text", "image"],
          },
        ],
      },
      {
        provider: "endpoint:ollama",
        execution_provider: "codex",
        available: true,
        display_name: "Ollama",
        provider_type: "ollama",
        supports_web_chat: true,
        source: "live",
        models: [
          {
            value: "endpoint:ollama/llava",
            label: "Llava",
            canonical_id: "llava",
            input_modalities: ["text", "image"],
          },
          {
            value: "endpoint:ollama/mistral",
            label: "Mistral",
            canonical_id: "mistral",
          },
        ],
      },
    ],
  };
}

function getProviderHeader(displayName: string) {
  const header = screen.getByText(displayName).parentElement;
  if (!header) throw new Error(`Missing provider header for ${displayName}`);
  return within(header);
}

function capabilityChips(name: string | RegExp) {
  const button = screen.getByRole("button", { name });
  return [...button.querySelectorAll(".capability-chip")].map(
    (element) => element.textContent,
  );
}

describe("ProviderPicker", () => {
  const originalFetch = global.fetch;

  beforeEach(() => {
    clearProviderModelCache();
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () =>
        buildCatalog([
          { value: "coder-model(qwen-oauth)", label: "Qwen Coder (OAuth)" },
          { value: "gpt-5(openai)", label: "gpt-5" },
        ]),
    }) as typeof fetch;
  });

  afterEach(() => {
    clearProviderModelCache();
    global.fetch = originalFetch;
    vi.clearAllMocks();
  });

  it("shows friendly Codex labels plus Qwen catalog entries", async () => {
    render(
      <ProviderPicker
        open={true}
        onClose={vi.fn()}
        currentProvider="claude"
        currentModel="opus"
        availableProviders={["claude", "qwen", "codex", "droid"]}
        onModelChange={vi.fn()}
        onProviderChange={vi.fn()}
        onSwitchProvider={vi.fn()}
        hasMessages={false}
      />,
    );

    await waitFor(() => {
      // Labels are transformed by parseCodex/parseQwen to friendly
      // display forms by resolveVisibleModels in providerModels.ts.
      expect(screen.getByText("GPT 5.4")).toBeTruthy();
      expect(screen.getByText("GPT 5.4 Mini")).toBeTruthy();
      expect(screen.getByText("GPT 5.3 Codex")).toBeTruthy();
      expect(screen.getByText("Qwen Coder (OAuth)")).toBeTruthy();
      expect(screen.getByText("GPT 5")).toBeTruthy();
      expect(screen.queryByText(/qwen oauth/i)).toBeNull();
      expect(screen.queryByText(/openai/i)).toBeNull();
    });
  });

  it("sorts visible providers alphabetically by display name", async () => {
    render(
      <ProviderPicker
        open={true}
        onClose={vi.fn()}
        currentProvider="claude"
        currentModel="opus"
        availableProviders={["qwen", "droid", "claude", "codex"]}
        onModelChange={vi.fn()}
        onProviderChange={vi.fn()}
        onSwitchProvider={vi.fn()}
        hasMessages={false}
      />,
    );

    await screen.findByText("GPT 5.4");

    const providerLabels = screen
      .getAllByText(/^(Claude|Codex|Droid|Qwen)$/)
      .map((element) => element.textContent);
    expect(providerLabels).toEqual(["Claude", "Codex", "Droid", "Qwen"]);
  });

  it("shows Grok and hides AGY entirely", async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        providers: [
          ...buildCatalog([{ value: "default", label: "Default" }]).providers,
          {
            provider: "grok",
            available: true,
            models: [{ value: "grok-build", label: "Grok Build" }],
            source: "live",
            supports_web_chat: true,
          },
          {
            provider: "agy",
            available: false,
            models: [],
            source: "unsupported",
            supports_web_chat: false,
            unavailable_reason: "No documented machine transport",
          },
        ],
      }),
    }) as typeof fetch;

    render(
      <ProviderPicker
        open={true}
        onClose={vi.fn()}
        currentProvider="claude"
        currentModel="opus"
        availableProviders={["claude"]}
        onModelChange={vi.fn()}
        onProviderChange={vi.fn()}
        onSwitchProvider={vi.fn()}
        hasMessages={false}
      />,
    );

    expect(await screen.findByText("Grok Build")).toBeTruthy();
    expect(screen.queryByText("AGY")).toBeNull();
    expect(screen.queryByText("unavailable")).toBeNull();
    expect(screen.queryByText("No documented machine transport")).toBeNull();
  });

  it("switches provider, model, and conversation when picking a new provider before first send", async () => {
    const onModelChange = vi.fn();
    const onProviderChange = vi.fn();
    const onSwitchProvider = vi.fn();

    render(
      <ProviderPicker
        open={true}
        onClose={vi.fn()}
        currentProvider="claude"
        currentModel="opus"
        availableProviders={["claude", "qwen", "codex", "droid"]}
        onModelChange={onModelChange}
        onProviderChange={onProviderChange}
        onSwitchProvider={onSwitchProvider}
        hasMessages={false}
      />,
    );

    await userEvent.click(await screen.findByText("GPT 5.4"));

    expect(onProviderChange).toHaveBeenCalledWith("codex");
    expect(onModelChange).toHaveBeenCalledWith("gpt-5.4");
    expect(onSwitchProvider).toHaveBeenCalledWith("codex");
  });

  it("groups a Responses endpoint under Codex and selects its endpoint model", async () => {
    const onSelect = vi.fn();
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => buildEndpointCatalog(),
    }) as typeof fetch;

    render(
      <ProviderPicker
        open={true}
        onClose={vi.fn()}
        currentProvider="claude"
        currentModel="opus"
        availableProviders={["claude", "codex"]}
        onModelChange={vi.fn()}
        onProviderChange={vi.fn()}
        onSelect={onSelect}
        hasMessages={false}
      />,
    );

    expect(await screen.findByText("Codex")).toBeTruthy();
    await userEvent.click(
      screen.getByRole("button", { name: "OpenRouter: Moonshotai/Kimi K3" }),
    );

    expect(onSelect).toHaveBeenCalledWith(
      "codex",
      "endpoint:openrouter/moonshotai/kimi-k3",
    );
  });

  it("marks a selected Responses endpoint as active in the Codex group", async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => buildEndpointCatalog(),
    }) as typeof fetch;

    render(
      <ProviderPicker
        open={true}
        onClose={vi.fn()}
        currentProvider="codex"
        currentModel="endpoint:openrouter/moonshotai/kimi-k3"
        availableProviders={["claude", "codex"]}
        onModelChange={vi.fn()}
        onProviderChange={vi.fn()}
        hasMessages={false}
      />,
    );

    await screen.findByText("Codex");

    expect(getProviderHeader("Codex").getByText("active")).toBeTruthy();
    expect(
      screen.getByRole("button", {
        name: "OpenRouter: Moonshotai/Kimi K3●",
      }),
    ).toBeTruthy();
    expect(screen.getByRole("button", { name: "GPT 5.4" })).toBeTruthy();
  });

  it("switches local models directly when the execution provider is unchanged", async () => {
    const onModelChange = vi.fn();
    const onProviderChange = vi.fn();
    const onSwitchProvider = vi.fn();
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => buildEndpointCatalog(),
    }) as typeof fetch;

    render(
      <ProviderPicker
        open={true}
        onClose={vi.fn()}
        currentProvider="codex"
        currentModel="gpt-5.4"
        availableProviders={["claude", "codex"]}
        onModelChange={onModelChange}
        onProviderChange={onProviderChange}
        onSwitchProvider={onSwitchProvider}
        hasMessages={true}
      />,
    );

    await userEvent.click(
      await screen.findByRole("button", {
        name: "OpenRouter: Moonshotai/Kimi K3",
      }),
    );

    expect(screen.queryByText("Switch provider?")).toBeNull();
    expect(onModelChange).toHaveBeenCalledWith(
      "endpoint:openrouter/moonshotai/kimi-k3",
    );
    expect(onProviderChange).not.toHaveBeenCalled();
    expect(onSwitchProvider).not.toHaveBeenCalled();
  });

  it("keeps native same-provider model changes in the current conversation", async () => {
    const onModelChange = vi.fn();
    const onProviderChange = vi.fn();
    const onSwitchProvider = vi.fn();

    render(
      <ProviderPicker
        open={true}
        onClose={vi.fn()}
        currentProvider="codex"
        currentModel="gpt-5.4"
        availableProviders={["claude", "codex"]}
        onModelChange={onModelChange}
        onProviderChange={onProviderChange}
        onSwitchProvider={onSwitchProvider}
        hasMessages={true}
      />,
    );

    await userEvent.click(
      await screen.findByRole("button", { name: "GPT 5.3 Codex" }),
    );

    expect(screen.queryByText("Switch provider?")).toBeNull();
    expect(onModelChange).toHaveBeenCalledWith("gpt-5.3-codex");
    expect(onProviderChange).not.toHaveBeenCalled();
    expect(onSwitchProvider).not.toHaveBeenCalled();
  });

  it("confirms a real execution-provider change before selecting a local model", async () => {
    const onModelChange = vi.fn();
    const onProviderChange = vi.fn();
    const onSwitchProvider = vi.fn();
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => buildEndpointCatalog(),
    }) as typeof fetch;

    render(
      <ProviderPicker
        open={true}
        onClose={vi.fn()}
        currentProvider="claude"
        currentModel="opus"
        availableProviders={["claude", "codex"]}
        onModelChange={onModelChange}
        onProviderChange={onProviderChange}
        onSwitchProvider={onSwitchProvider}
        hasMessages={true}
      />,
    );

    await userEvent.click(
      await screen.findByRole("button", {
        name: "OpenRouter: Moonshotai/Kimi K3",
      }),
    );
    expect(screen.getByText("Switch provider?")).toBeTruthy();

    await userEvent.click(screen.getByRole("button", { name: "Switch" }));

    expect(onProviderChange).toHaveBeenCalledWith("codex");
    expect(onModelChange).toHaveBeenCalledWith(
      "endpoint:openrouter/moonshotai/kimi-k3",
    );
    expect(onSwitchProvider).toHaveBeenCalledWith("codex");
  });

  it("confirms execution-provider changes before invoking the integrated selection callback", async () => {
    const onSelect = vi.fn();
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => buildEndpointCatalog(),
    }) as typeof fetch;

    render(
      <ProviderPicker
        open={true}
        onClose={vi.fn()}
        currentProvider="claude"
        currentModel="opus"
        availableProviders={["claude", "codex"]}
        onModelChange={vi.fn()}
        onProviderChange={vi.fn()}
        onSelect={onSelect}
        hasMessages={true}
      />,
    );

    await userEvent.click(
      await screen.findByRole("button", {
        name: "OpenRouter: Moonshotai/Kimi K3",
      }),
    );
    expect(screen.getByText("Switch provider?")).toBeTruthy();
    expect(onSelect).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "Switch" }));

    expect(onSelect).toHaveBeenCalledWith(
      "codex",
      "endpoint:openrouter/moonshotai/kimi-k3",
    );
  });

  it("lists a healthy LM Studio endpoint group and routes selection through Codex", async () => {
    const onSelect = vi.fn();
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => buildLocalEndpointCatalog(),
    }) as typeof fetch;

    render(
      <ProviderPicker
        open={true}
        onClose={vi.fn()}
        currentProvider="codex"
        currentModel="gpt-5.4"
        availableProviders={["claude", "codex"]}
        onModelChange={vi.fn()}
        onProviderChange={vi.fn()}
        onSelect={onSelect}
        hasMessages={true}
      />,
    );

    const groupLabel = await screen.findByText("LM Studio");
    expect(
      groupLabel.parentElement?.querySelector(".source-icon-lmstudio"),
    ).toBeTruthy();

    // Same execution provider (codex) — no switch confirmation, byte-exact
    // endpoint selector round-trips (#19161, #18449 contract).
    await userEvent.click(
      screen.getByRole("button", { name: "Devstral Small" }),
    );
    expect(screen.queryByText("Switch provider?")).toBeNull();
    expect(onSelect).toHaveBeenCalledWith(
      "codex",
      "endpoint:studio/mistralai/devstral-small",
    );
  });

  it("marks the LM Studio group active when its endpoint model is selected", async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => buildLocalEndpointCatalog(),
    }) as typeof fetch;

    render(
      <ProviderPicker
        open={true}
        onClose={vi.fn()}
        currentProvider="codex"
        currentModel="endpoint:studio/mistralai/devstral-small"
        availableProviders={["claude", "codex"]}
        onModelChange={vi.fn()}
        onProviderChange={vi.fn()}
        hasMessages={false}
      />,
    );

    await screen.findByText("LM Studio");

    expect(getProviderHeader("LM Studio").getByText("active")).toBeTruthy();
    expect(getProviderHeader("Codex").queryByText("active")).toBeNull();
  });

  it("hides unreachable local endpoint groups from the picker", async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () =>
        buildLocalEndpointCatalog({
          available: false,
          supports_web_chat: false,
          execution_provider: undefined,
          models: [],
          unavailable_reason: "connection refused",
        }),
    }) as typeof fetch;

    render(
      <ProviderPicker
        open={true}
        onClose={vi.fn()}
        currentProvider="claude"
        currentModel="opus"
        availableProviders={["claude", "codex"]}
        onModelChange={vi.fn()}
        onProviderChange={vi.fn()}
        hasMessages={false}
      />,
    );

    await screen.findByText("GPT 5.4");
    expect(screen.queryByText("LM Studio")).toBeNull();
    expect(screen.queryByText("connection refused")).toBeNull();
  });

  it("falls back to a default model entry for Qwen when the catalog is empty", async () => {
    const onModelChange = vi.fn();
    const onProviderChange = vi.fn();
    const onSwitchProvider = vi.fn();
    const nowSpy = vi.spyOn(Date, "now").mockReturnValue(9_999_999_999_999);
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => buildCatalog([]),
    }) as typeof fetch;

    render(
      <ProviderPicker
        open={true}
        onClose={vi.fn()}
        currentProvider="claude"
        currentModel="opus"
        availableProviders={["claude", "qwen", "codex", "droid"]}
        onModelChange={onModelChange}
        onProviderChange={onProviderChange}
        onSwitchProvider={onSwitchProvider}
        hasMessages={false}
      />,
    );

    await userEvent.click(await screen.findByText("Default"));

    expect(onProviderChange).toHaveBeenCalledWith("qwen");
    expect(onModelChange).toHaveBeenCalledWith("default");
    expect(onSwitchProvider).toHaveBeenCalledWith("qwen");
    nowSpy.mockRestore();
  });

  it("renders Text/Image chips from probe and advertised modalities, and none when modalities are absent", async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => buildCapabilityCatalog(),
    }) as typeof fetch;

    render(
      <ProviderPicker
        open={true}
        onClose={vi.fn()}
        currentProvider="claude"
        currentModel="opus"
        availableProviders={["claude", "codex"]}
        onModelChange={vi.fn()}
        onProviderChange={vi.fn()}
        hasMessages={false}
      />,
    );

    await screen.findByText("vLLM");

    expect(capabilityChips("Qwen2.5 VL")).toEqual(["Text", "Image"]);
    expect(capabilityChips("Llama 3")).toEqual([]);
    expect(capabilityChips("Qwen VL")).toEqual(["Text", "Image"]);
    expect(capabilityChips("Llava")).toEqual(["Text", "Image"]);
    expect(capabilityChips("Mistral")).toEqual([]);
    expect(capabilityChips(/^Opus/)).toEqual([]);
    expect(capabilityChips("GPT 5.4")).toEqual([]);
  });
});
