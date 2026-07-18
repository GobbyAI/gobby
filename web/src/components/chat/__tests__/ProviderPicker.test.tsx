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

function buildLocalCatalog() {
  return {
    providers: [
      ...buildCatalog().providers,
      {
        provider: "local:lm-studio",
        execution_provider: "codex",
        available: true,
        models: [
          {
            value: "local:lm-studio/qwen3-coder",
            label: "Qwen3 Coder",
          },
        ],
        source: "live",
        display_name: "LM Studio",
        supports_web_chat: true,
      },
      {
        provider: "local:ollama",
        execution_provider: "codex",
        available: true,
        models: [
          {
            value: "local:ollama/llama3.2",
            label: "Llama 3.2",
          },
        ],
        source: "live",
        display_name: "Ollama",
        supports_web_chat: true,
      },
      {
        provider: "local:offline",
        execution_provider: "codex",
        available: false,
        models: [],
        source: "failed",
        display_name: "Offline Local",
        supports_web_chat: false,
        unavailable_reason: "Local endpoint is unreachable",
      },
    ],
  };
}

function getProviderHeader(displayName: string) {
  const header = screen.getByText(displayName).parentElement;
  if (!header) throw new Error(`Missing provider header for ${displayName}`);
  return within(header);
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

  it("shows Grok and disables AGY", async () => {
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
    expect(screen.getByText("unavailable")).toBeTruthy();
    expect(screen.getByText("No documented machine transport")).toBeTruthy();
    expect(screen.getAllByRole("button", { name: "Default" }).some(
      (button) => button.hasAttribute("disabled"),
    )).toBe(true);
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

  it("renders local catalog groups and routes selections through their execution provider", async () => {
    const onSelect = vi.fn();
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => buildLocalCatalog(),
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

    expect(await screen.findByText("LM Studio")).toBeTruthy();
    expect(screen.getByText("Ollama")).toBeTruthy();
    expect(screen.getByText("Offline Local")).toBeTruthy();
    expect(screen.getByText("Local endpoint is unreachable")).toBeTruthy();

    const disabledDefaults = screen
      .getAllByRole("button", { name: "Default" })
      .filter((button) => button.hasAttribute("disabled"));
    expect(disabledDefaults).toHaveLength(1);
    await userEvent.click(disabledDefaults[0]);
    expect(onSelect).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "Qwen3 Coder" }));

    expect(onSelect).toHaveBeenCalledWith(
      "codex",
      "local:lm-studio/qwen3-coder",
    );
  });

  it("maps an exact Codex local selector only to its owning catalog group", async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => buildLocalCatalog(),
    }) as typeof fetch;

    render(
      <ProviderPicker
        open={true}
        onClose={vi.fn()}
        currentProvider="codex"
        currentModel="local:lm-studio/qwen3-coder"
        availableProviders={["claude", "codex"]}
        onModelChange={vi.fn()}
        onProviderChange={vi.fn()}
        hasMessages={false}
      />,
    );

    await screen.findByText("LM Studio");

    expect(getProviderHeader("LM Studio").getByText("active")).toBeTruthy();
    expect(getProviderHeader("Codex").queryByText("active")).toBeNull();
    expect(getProviderHeader("Ollama").queryByText("active")).toBeNull();
    expect(
      screen.getByRole("button", { name: "Qwen3 Coder●" }),
    ).toBeTruthy();
    expect(screen.getByRole("button", { name: "Llama 3.2" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "GPT 5.4" })).toBeTruthy();
  });

  it("switches local models directly when the execution provider is unchanged", async () => {
    const onModelChange = vi.fn();
    const onProviderChange = vi.fn();
    const onSwitchProvider = vi.fn();
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => buildLocalCatalog(),
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
      await screen.findByRole("button", { name: "Qwen3 Coder" }),
    );

    expect(screen.queryByText("Switch provider?")).toBeNull();
    expect(onModelChange).toHaveBeenCalledWith("local:lm-studio/qwen3-coder");
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
      json: async () => buildLocalCatalog(),
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
      await screen.findByRole("button", { name: "Qwen3 Coder" }),
    );
    expect(screen.getByText("Switch provider?")).toBeTruthy();

    await userEvent.click(screen.getByRole("button", { name: "Switch" }));

    expect(onProviderChange).toHaveBeenCalledWith("codex");
    expect(onModelChange).toHaveBeenCalledWith(
      "local:lm-studio/qwen3-coder",
    );
    expect(onSwitchProvider).toHaveBeenCalledWith("codex");
  });

  it("confirms execution-provider changes before invoking the integrated selection callback", async () => {
    const onSelect = vi.fn();
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => buildLocalCatalog(),
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
      await screen.findByRole("button", { name: "Qwen3 Coder" }),
    );
    expect(screen.getByText("Switch provider?")).toBeTruthy();
    expect(onSelect).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "Switch" }));

    expect(onSelect).toHaveBeenCalledWith(
      "codex",
      "local:lm-studio/qwen3-coder",
    );
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
});
