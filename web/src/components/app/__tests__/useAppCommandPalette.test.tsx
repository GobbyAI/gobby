import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ACTIVITY_PANEL_TABS } from "../../activity/ActivityPanelTabs";
import { RESTART_TIMEOUT_MS } from "../../../lib/api";
import { useAppCommandPalette } from "../useAppCommandPalette";

function makeHookArgs(addSystemMessage = vi.fn()) {
  return {
    startNewChat: vi.fn(),
    clearHistory: vi.fn(),
    sendMessage: vi.fn(() => true),
    settings: {
      model: "claude-sonnet",
      chatMode: "normal" as const,
      ttsEnabled: false,
    },
    effectiveProjectId: "project-1",
    currentMainReasoning: null,
    updateChatMode: vi.fn(),
    sendMode: vi.fn(),
    addSystemMessage,
    setActiveModal: vi.fn(),
    settingsOverlay: { open: vi.fn() },
    setResumeModalOpen: vi.fn(),
    showPlanRef: { current: vi.fn() },
    openActivityTab: vi.fn(),
  };
}

describe("useAppCommandPalette", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("treats a restart request abort as an accepted daemon restart", async () => {
    vi.useFakeTimers();
    const addSystemMessage = vi.fn();
    let capturedSignal: AbortSignal | undefined;
    const fetchMock = vi.fn(
      (_url: string | URL | Request, init?: RequestInit) => {
        capturedSignal = init?.signal ?? undefined;
        return new Promise<Response>((_resolve, reject) => {
          capturedSignal?.addEventListener(
            "abort",
            () => reject(new DOMException("aborted", "AbortError")),
            { once: true },
          );
        });
      },
    );
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() =>
      useAppCommandPalette(makeHookArgs(addSystemMessage)),
    );
    const restartAction = result.current.commandPaletteActions.find(
      (action) => action.id === "restart",
    );

    act(() => {
      restartAction?.onSelect();
    });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/admin/restart",
      expect.objectContaining({
        credentials: "include",
        method: "POST",
        signal: expect.any(AbortSignal),
      }),
    );
    expect(capturedSignal).toBeInstanceOf(AbortSignal);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(RESTART_TIMEOUT_MS);
      await Promise.resolve();
    });

    expect(addSystemMessage).toHaveBeenCalledTimes(2);
    expect(addSystemMessage).toHaveBeenCalledWith(
      "Requesting daemon restart...",
    );
    expect(addSystemMessage).toHaveBeenCalledWith(
      "Daemon restart requested; reconnecting...",
    );
    expect(addSystemMessage).not.toHaveBeenCalledWith(
      "Failed to restart daemon",
    );
  });

  it("surfaces protected work and offers a force restart", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            status: "restart_protected",
            message: "Restart blocked by active protected cron runs",
            protected_runs: [
              {
                run_id: "run-1",
                job_id: "job-1",
                job_name: "gobby:memory-dream",
                started_at: "2026-08-27T07:00:00+00:00",
                elapsed_seconds: 3725,
                remaining_seconds: 12475,
              },
            ],
          }),
          { status: 409, headers: { "Content-Type": "application/json" } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ status: "restarting" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    vi.stubGlobal("fetch", fetchMock);
    const addSystemMessage = vi.fn();
    const { result } = renderHook(() =>
      useAppCommandPalette(makeHookArgs(addSystemMessage)),
    );

    act(() => {
      result.current.commandPaletteActions
        .find((action) => action.id === "restart")
        ?.onSelect();
    });

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(fetchMock.mock.calls.map((call) => call[0])).toEqual([
      "/api/admin/restart",
      "/api/admin/restart?force=true",
    ]);
    expect(confirm).toHaveBeenCalledWith(
      expect.stringContaining(
        "gobby:memory-dream (running 1h 2m 5s, at most 3h 27m 55s left)",
      ),
    );
    expect(addSystemMessage).toHaveBeenCalledWith(
      expect.stringContaining(
        "gobby:memory-dream (running 1h 2m 5s, at most 3h 27m 55s left)",
      ),
    );
    expect(addSystemMessage).toHaveBeenCalledWith(
      "Daemon restart requested; reconnecting...",
    );
  });

  it("routes legacy MCP browse commands to the MCP activity tab", () => {
    const args = makeHookArgs();
    const { result } = renderHook(() => useAppCommandPalette(args));

    act(() => {
      result.current.handlePaletteSelect({
        kind: "command",
        name: "mcp",
        description: "Open MCP activity",
        action: "open_mcp",
      });
    });

    expect(args.openActivityTab).toHaveBeenCalledWith("mcp");
    expect(args.setActiveModal).not.toHaveBeenCalledWith("mcp");
  });

  it("opens the settings overlay exactly once from both settings actions", () => {
    const args = makeHookArgs();
    const { result } = renderHook(() => useAppCommandPalette(args));

    act(() => {
      result.current.handlePaletteSelect({
        kind: "command",
        name: "settings",
        description: "Open settings",
        action: "open_settings",
      });
    });
    expect(args.settingsOverlay.open).toHaveBeenCalledOnce();

    args.settingsOverlay.open.mockClear();
    const settingsAction = result.current.commandPaletteActions.find(
      (action) => action.id === "settings",
    );
    act(() => settingsAction?.onSelect());

    expect(args.settingsOverlay.open).toHaveBeenCalledOnce();
  });

  it("derives activity navigation actions from the activity tab registry", () => {
    const args = makeHookArgs();
    const { result } = renderHook(() => useAppCommandPalette(args));
    const navigationActions = result.current.commandPaletteActions.filter(
      (action) => action.category === "navigate",
    );

    expect(navigationActions.map(({ id, label }) => ({ id, label }))).toEqual(
      ACTIVITY_PANEL_TABS.map(({ id, label }) => ({ id: `nav-${id}`, label })),
    );

    for (const tab of ACTIVITY_PANEL_TABS) {
      const action = navigationActions.find(({ id }) => id === `nav-${tab.id}`);

      act(() => action?.onSelect());
      expect(args.openActivityTab).toHaveBeenLastCalledWith(tab.id);
    }
  });
});
