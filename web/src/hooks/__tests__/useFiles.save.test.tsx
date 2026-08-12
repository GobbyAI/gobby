import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useFiles } from "../useFiles";

describe("useFiles save", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("refreshes git status and clears a prior error after a successful save", async () => {
    let writeAttempts = 0;
    const fetchMock = vi.fn((input: RequestInfo | URL): Promise<Response> => {
      const url = String(input);
      if (url.includes("/api/files/projects")) {
        return Promise.resolve({ ok: true, json: async () => [] } as Response);
      }
      if (url.includes("/api/files/read")) {
        return Promise.resolve({
          ok: true,
          json: async () => ({
            content: "original",
            image: false,
            binary: false,
            truncated: false,
            mime_type: "text/plain",
            size: 8,
          }),
        } as Response);
      }
      if (url.includes("/api/files/write")) {
        writeAttempts += 1;
        return Promise.resolve({
          ok: writeAttempts > 1,
          status: writeAttempts > 1 ? 200 : 500,
          json: async () => ({ detail: "save failed" }),
        } as Response);
      }
      if (url.includes("/api/files/git-status")) {
        return Promise.resolve({
          ok: true,
          json: async () => ({ branch: "main", files: { "notes.txt": "M" } }),
        } as Response);
      }
      return Promise.resolve({ ok: true, json: async () => ({}) } as Response);
    });
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useFiles());
    await act(async () => {
      await result.current.openFile("project-1", "notes.txt", "notes.txt");
    });
    act(() => {
      result.current.toggleEditing(0);
      result.current.updateEditContent(0, "updated");
    });

    await act(async () => {
      await result.current.saveFile(0);
    });
    expect(result.current.openFiles[0]?.saveError).toContain("save failed");

    await act(async () => {
      await result.current.saveFile(0);
    });

    await waitFor(() => {
      expect(result.current.openFiles[0]?.error).toBeNull();
      expect(result.current.openFiles[0]?.saveError).toBeNull();
      expect(result.current.gitStatuses.get("project-1")?.files).toEqual({
        "notes.txt": "M",
      });
    });
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/api/files/git-status?project_id=project-1"),
    );
  });
});
