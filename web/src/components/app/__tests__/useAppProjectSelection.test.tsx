import { act, renderHook, waitFor } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ProjectWithStats } from "../../../hooks/useProjects";
import { useAppProjectSelection } from "../useAppProjectSelection";

const PROJECTS = [
  { id: "persisted-project", name: "persisted" },
  { id: "user-project", name: "user" },
] as ProjectWithStats[];

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

describe("useAppProjectSelection", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("preserves and persists selections made while UI settings are loading", async () => {
    const settingsResponse = deferred<{
      selectedProjectId: string;
      selectedProvider: string;
    }>();
    const fetchMock = vi.fn((_url: string | URL | Request, init?: RequestInit) => {
      if (!init) {
        return Promise.resolve({
          ok: true,
          json: () => settingsResponse.promise,
        });
      }
      return Promise.resolve({ ok: true });
    }) as unknown as typeof fetch;
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => {
      const [selectedProvider, setSelectedProvider] = useState<string | null>(
        "claude",
      );
      const selection = useAppProjectSelection({
        allProjects: PROJECTS,
        onProjectSelect: vi.fn(),
        selectedProvider,
        setSelectedProvider,
        startNewChat: vi.fn(),
        setProjectIdRef: vi.fn(),
        sendProjectChange: vi.fn(),
      });
      return { selectedProvider, selection };
    });

    act(() => {
      result.current.selection.selectProject("user-project");
      result.current.selection.selectProvider("qwen");
    });

    await act(async () => {
      settingsResponse.resolve({
        selectedProjectId: "persisted-project",
        selectedProvider: "codex",
      });
      await settingsResponse.promise;
    });

    expect(result.current.selection.effectiveProjectId).toBe("user-project");
    expect(result.current.selectedProvider).toBe("qwen");
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/config/ui-settings",
        expect.objectContaining({
          method: "PUT",
          body: JSON.stringify({ selectedProjectId: "user-project" }),
        }),
      );
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/config/ui-settings",
        expect.objectContaining({
          method: "PUT",
          body: JSON.stringify({ selectedProvider: "qwen" }),
        }),
      );
    });
  });
});
