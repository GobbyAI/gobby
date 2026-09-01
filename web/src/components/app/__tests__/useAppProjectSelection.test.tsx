import { act, renderHook, waitFor } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { configurationClient } from "../../../api/config";
import type { ProjectWithStats } from "../../../hooks/useProjects";
import { useAppProjectSelection } from "../useAppProjectSelection";

const PROJECTS = [
  { id: "persisted-project", name: "persisted", checkout: null },
  { id: "user-project", name: "user", checkout: null },
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
    configurationClient.reset();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("flags projects without a checkout on this machine and never flags Personal", () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ revision: 1, desired: {} }),
        } as Response),
      ),
    );

    const { result } = renderHook(() =>
      useAppProjectSelection({
        allProjects: [
          { id: "personal", name: "_personal", checkout: null },
          {
            id: "local",
            name: "local",
            checkout: { machine_id: "m-1", root_path: "/repo" },
          },
          { id: "remote", name: "remote", checkout: null },
        ] as ProjectWithStats[],
        onProjectSelect: vi.fn(),
        selectedProvider: null,
        setSelectedProvider: vi.fn(),
        startNewChat: vi.fn(),
        setProjectIdRef: vi.fn(),
        sendProjectChange: vi.fn(),
      }),
    );

    expect(result.current.projectOptions).toEqual([
      { id: "personal", name: "Personal", hasCheckout: true },
      { id: "local", name: "local", hasCheckout: true },
      { id: "remote", name: "remote", hasCheckout: false },
    ]);
  });

  it("persists project selection through the universal config patch", async () => {
    const settingsResponse = deferred<Record<string, unknown>>();
    const fetchMock = vi.fn(
      (_url: string | URL | Request, init?: RequestInit) => {
        if (!init) {
          return Promise.resolve({
            ok: true,
            json: () => settingsResponse.promise,
          } as Response);
        }
        return Promise.resolve({
          ok: true,
          json: () =>
            Promise.resolve({
              committed: true,
              revision: 6,
              changed_keys: ["ui_settings.selectedProjectId"],
              apply_status: "applied",
              pending_restart_keys: [],
              failed_live_keys: {},
            }),
        } as Response);
      },
    );
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

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
        revision: 5,
        desired: {
          ui_settings: {
            selectedProjectId: "persisted-project",
            selectedProvider: "codex",
          },
        },
        active: {
          ui_settings: {
            selectedProjectId: "persisted-project",
            selectedProvider: "codex",
          },
        },
        secret_set: {},
        pending_restart_keys: [],
        failed_live_keys: {},
      });
      await settingsResponse.promise;
    });

    expect(result.current.selection.effectiveProjectId).toBe("user-project");
    expect(result.current.selectedProvider).toBe("qwen");
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/config/values",
        expect.objectContaining({
          method: "PATCH",
        }),
      );
      const patchBodies = fetchMock.mock.calls
        .filter(([, init]) => init?.method === "PATCH")
        .map(([, init]) => JSON.parse(String(init?.body)));
      expect(patchBodies).toContainEqual(
        expect.objectContaining({
          expected_revision: expect.any(Number),
          values: { ui_settings: { selectedProjectId: "user-project" } },
        }),
      );
      expect(patchBodies).toContainEqual(
        expect.objectContaining({
          expected_revision: expect.any(Number),
          values: { ui_settings: { selectedProvider: "qwen" } },
        }),
      );
    });
  });
});
