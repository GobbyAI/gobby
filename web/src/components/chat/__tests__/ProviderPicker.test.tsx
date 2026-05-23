import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
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
        provider: "gemini",
        available: true,
        models: [
          { value: "gemini-3.1-pro-preview", label: "pro-3.1" },
          { value: "gemini-3-flash-preview", label: "flash-3" },
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

describe("ProviderPicker", () => {
  const originalFetch = global.fetch;

  beforeEach(() => {
    clearProviderModelCache();
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () =>
        buildCatalog([
          { value: "coder-model(qwen-oauth)", label: "coder-model (qwen-oauth)" },
          { value: "gpt-5(openai)", label: "gpt-5 (openai)" },
        ]),
    }) as typeof fetch;
  });

  afterEach(() => {
    clearProviderModelCache();
    global.fetch = originalFetch;
    vi.clearAllMocks();
  });

  it("shows friendly Gemini and Codex labels plus Qwen catalog entries", async () => {
    render(
      <ProviderPicker
        open={true}
        onClose={vi.fn()}
        currentProvider="claude"
        currentModel="opus"
        availableProviders={["claude", "gemini", "qwen", "codex", "droid"]}
        onModelChange={vi.fn()}
        onProviderChange={vi.fn()}
        onSwitchProvider={vi.fn()}
        hasMessages={false}
      />,
    );

    await waitFor(() => {
      // Labels are transformed by parseGemini/parseCodex/parseQwen to friendly
      // display forms by resolveVisibleModels in providerModels.ts.
      expect(screen.getByText("Gemini 3.1 Pro")).toBeTruthy();
      expect(screen.getByText("Gemini 3 Flash")).toBeTruthy();
      expect(screen.getByText("GPT 5.4")).toBeTruthy();
      expect(screen.getByText("GPT 5.4 Mini")).toBeTruthy();
      expect(screen.getByText("GPT 5.3 Codex")).toBeTruthy();
      expect(screen.getByText("Coder Model")).toBeTruthy();
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
        availableProviders={["qwen", "droid", "claude", "gemini", "codex"]}
        onModelChange={vi.fn()}
        onProviderChange={vi.fn()}
        onSwitchProvider={vi.fn()}
        hasMessages={false}
      />,
    );

    await screen.findByText("GPT 5.4");

    const providerLabels = screen
      .getAllByText(/^(Claude|Codex|Droid|Gemini|Qwen)$/)
      .map((element) => element.textContent);
    expect(providerLabels).toEqual(["Claude", "Codex", "Droid", "Gemini", "Qwen"]);
  });

  it("shows Grok, disables AGY, and labels Gemini as deprecated", async () => {
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
          {
            provider: "gemini",
            available: true,
            models: [{ value: "gemini-3.1-pro-preview", label: "pro-3.1" }],
            source: "static",
            deprecated: true,
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
    expect(screen.getByText("deprecated")).toBeTruthy();
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
        availableProviders={["claude", "gemini", "qwen", "codex", "droid"]}
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
        availableProviders={["claude", "gemini", "qwen", "codex", "droid"]}
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
