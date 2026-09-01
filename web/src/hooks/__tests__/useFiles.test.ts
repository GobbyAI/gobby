import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, expectTypeOf, it, vi } from "vitest";

import { useFiles } from "../useFiles";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("useFiles projects", () => {
  it("accepts checkout objects and projects without a checkout", async () => {
    const projects = [
      {
        id: "project-1",
        name: "Project One",
        checkout: { machine_id: "machine-1", root_path: "/tmp/project-one" },
      },
      { id: "project-2", name: "Project Two", checkout: null },
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        if (String(input).endsWith("/api/files/projects")) {
          return { ok: true, json: async () => projects } as Response;
        }
        throw new Error(`Unexpected request: ${String(input)}`);
      }),
    );

    const { result } = renderHook(() => useFiles());

    await waitFor(() => expect(result.current.projects).toEqual(projects));
    expectTypeOf(result.current.projects[0]!.checkout).toEqualTypeOf<{
      machine_id: string;
      root_path: string;
    } | null>();
  });
});

describe("useFiles saves", () => {
  it("preserves unsaved edits and clears the error after a successful retry", async () => {
    let writeAttempts = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/api/files/projects")) {
        return { ok: true, json: async () => [] } as Response;
      }
      if (url.includes("/api/files/read?")) {
        return {
          ok: true,
          json: async () => ({
            content: "original",
            image: false,
            binary: false,
            mime_type: "text/plain",
            size: 8,
          }),
        } as Response;
      }
      if (url.includes("/api/files/git-status?")) {
        return { ok: true, json: async () => ({}) } as Response;
      }
      if (url.endsWith("/api/files/write")) {
        writeAttempts += 1;
        return writeAttempts === 1
          ? ({
              ok: false,
              status: 503,
              json: async () => ({ detail: "temporarily unavailable" }),
            } as Response)
          : ({ ok: true, json: async () => ({}) } as Response);
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useFiles());

    await act(async () => {
      await result.current.openFile("project-1", "notes.txt", "notes.txt");
    });
    await waitFor(() =>
      expect(result.current.openFiles[0]?.loading).toBe(false),
    );

    act(() => {
      result.current.toggleEditing(0);
      result.current.updateEditContent(0, "unsaved draft");
    });

    await act(async () => {
      await result.current.saveFile(0);
    });

    expect(result.current.openFiles[0]).toMatchObject({
      editing: true,
      dirty: true,
      editContent: "unsaved draft",
      saveError: "Error: temporarily unavailable",
    });

    act(() => result.current.clearSaveError(0));
    expect(result.current.openFiles[0].saveError).toBeNull();

    await act(async () => {
      await result.current.saveFile(0);
    });

    expect(result.current.openFiles[0]).toMatchObject({
      content: "unsaved draft",
      originalContent: "unsaved draft",
      editContent: "unsaved draft",
      dirty: false,
      saveError: null,
    });
    expect(writeAttempts).toBe(2);
  });

  it("finishes saving the same file when an earlier tab closes", async () => {
    let resolveWrite!: (response: Response) => void;
    const writeResponse = new Promise<Response>((resolve) => {
      resolveWrite = resolve;
    });
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/api/files/projects")) {
        return { ok: true, json: async () => [] } as Response;
      }
      if (url.includes("/api/files/read?")) {
        return {
          ok: true,
          json: async () => ({
            content: "original",
            image: false,
            binary: false,
            mime_type: "text/plain",
            size: 8,
          }),
        } as Response;
      }
      if (url.includes("/api/files/git-status?")) {
        return { ok: true, json: async () => ({}) } as Response;
      }
      if (url.endsWith("/api/files/write")) {
        return writeResponse;
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useFiles());

    await act(async () => {
      await result.current.openFile("project-1", "first.txt", "first.txt");
      await result.current.openFile("project-1", "second.txt", "second.txt");
    });
    await waitFor(() => expect(result.current.openFiles).toHaveLength(2));

    act(() => {
      result.current.toggleEditing(1);
      result.current.updateEditContent(1, "saved content");
    });

    let savePromise!: Promise<void>;
    act(() => {
      savePromise = result.current.saveFile(1);
    });
    await waitFor(() => expect(result.current.openFiles[1]?.saving).toBe(true));

    act(() => result.current.closeFile(0));
    expect(result.current.openFiles[0]).toMatchObject({
      path: "second.txt",
      saving: true,
    });

    await act(async () => {
      resolveWrite({ ok: true, json: async () => ({}) } as Response);
      await savePromise;
    });

    expect(result.current.openFiles[0]).toMatchObject({
      path: "second.txt",
      content: "saved content",
      originalContent: "saved content",
      dirty: false,
      saving: false,
      saveError: null,
    });
  });
});
