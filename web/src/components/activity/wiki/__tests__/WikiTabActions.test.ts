import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useWikiTabActions } from "../WikiTabActions";
import { writeConflictBody, writeSuccessEnvelope } from "./fixtures";

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status < 400,
    status,
    json: async () => body,
  } as Response;
}

function makeWikiHelpers() {
  return {
    refresh: vi.fn(async () => {}),
    attach: vi.fn(async () => ({})),
    ingest: vi.fn(async () => ({})),
    compileWiki: vi.fn(async () => ({})),
    audit: vi.fn(async () => ({})),
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("useWikiTabActions", () => {
  it("saves a page, refetches, and reports success", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(writeSuccessEnvelope)));
    const onRefetch = vi.fn(async () => {});
    const { result } = renderHook(() =>
      useWikiTabActions({ scope: { projectId: "p1" }, wiki: makeWikiHelpers(), onRefetch }),
    );

    let saved: unknown;
    await act(async () => {
      saved = await result.current.savePageAndRefresh({
        path: "knowledge/topics/example.md",
        content: "# Example",
      });
    });

    expect(saved).toMatchObject({ ok: true, path: "knowledge/topics/example.md" });
    expect(onRefetch).toHaveBeenCalledTimes(1);
    expect(result.current.status.error).toBeNull();
    expect(result.current.status.message).toMatch(/saved/i);
  });

  it("surfaces a write conflict without refetching", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(writeConflictBody, 412)));
    const onRefetch = vi.fn(async () => {});
    const { result } = renderHook(() =>
      useWikiTabActions({ scope: {}, wiki: makeWikiHelpers(), onRefetch }),
    );

    let saved: unknown;
    await act(async () => {
      saved = await result.current.savePageAndRefresh({
        path: "knowledge/topics/example.md",
        content: "# Stale",
        expectedHash: "stale",
      });
    });

    expect(saved).toMatchObject({ ok: false, conflict: true, code: "precondition_failed" });
    expect(onRefetch).not.toHaveBeenCalled();
    expect(result.current.status.error).toMatch(/hash/i);
  });

  it("creates a page then navigates to it", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse({
          ...writeSuccessEnvelope,
          payload: { ...writeSuccessEnvelope.payload, created: true },
        }),
      ),
    );
    const onNavigate = vi.fn(async () => {});
    const { result } = renderHook(() =>
      useWikiTabActions({ scope: {}, wiki: makeWikiHelpers(), onNavigate }),
    );

    await act(async () => {
      await result.current.createPageAndNavigate("knowledge/topics/example.md", "# New");
    });

    expect(onNavigate).toHaveBeenCalledWith("knowledge/topics/example.md");
  });

  it("runs compile through the kept useWiki helper with busy state", async () => {
    const wiki = makeWikiHelpers();
    let release: () => void = () => {};
    wiki.compileWiki.mockImplementation(
      () =>
        new Promise((resolve) => {
          release = () => resolve({});
        }),
    );
    const { result } = renderHook(() => useWikiTabActions({ scope: {}, wiki }));

    let pending: Promise<void>;
    act(() => {
      pending = result.current.runCompile();
    });
    await waitFor(() => expect(result.current.status.busy).toBe("compile"));

    release();
    await act(async () => {
      await pending;
    });
    expect(result.current.status.busy).toBeNull();
    expect(wiki.compileWiki).toHaveBeenCalledTimes(1);
  });

  it("captures helper failures as status errors", async () => {
    const wiki = makeWikiHelpers();
    wiki.audit.mockRejectedValue(new Error("audit exploded"));
    const { result } = renderHook(() => useWikiTabActions({ scope: {}, wiki }));

    await act(async () => {
      await result.current.runAudit();
    });

    expect(result.current.status.error).toMatch(/audit exploded/);
    expect(result.current.status.busy).toBeNull();
  });

  it("ingests a URL via the kept helper", async () => {
    const wiki = makeWikiHelpers();
    const { result } = renderHook(() => useWikiTabActions({ scope: {}, wiki }));

    await act(async () => {
      await result.current.ingestUrl("https://example.com/notes");
    });

    expect(wiki.ingest).toHaveBeenCalledWith({ urls: ["https://example.com/notes"] });
  });
});
