import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ProviderPicker } from "../ProviderPicker";

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
    ],
  };
}

describe("ProviderPicker", () => {
  const originalFetch = global.fetch;

  beforeEach(() => {
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
        availableProviders={["claude", "gemini", "qwen", "codex"]}
        onModelChange={vi.fn()}
        onProviderChange={vi.fn()}
        onSwitchProvider={vi.fn()}
        hasMessages={false}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText("pro-3.1")).toBeTruthy();
      expect(screen.getByText("flash-3")).toBeTruthy();
      expect(screen.getByText("codex-5.4")).toBeTruthy();
      expect(screen.getByText("mini-5.4")).toBeTruthy();
      expect(screen.getByText("codex-5.3")).toBeTruthy();
      expect(screen.getByText("coder-model (qwen-oauth)")).toBeTruthy();
      expect(screen.getByText("gpt-5 (openai)")).toBeTruthy();
    });
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
        availableProviders={["claude", "gemini", "qwen", "codex"]}
        onModelChange={onModelChange}
        onProviderChange={onProviderChange}
        onSwitchProvider={onSwitchProvider}
        hasMessages={false}
      />,
    );

    await userEvent.click(await screen.findByText("codex-5.4"));

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
        availableProviders={["claude", "gemini", "qwen", "codex"]}
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
