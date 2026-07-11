import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  WIKI_NAV_HISTORY_CAP,
  WIKI_TAB_KEYS,
  isWikiMode,
  loadLastPage,
  loadStoredMode,
  loadStoredTopic,
  modeForPath,
  readStoredValue,
  storeLastPage,
  storeMode,
  storeTopic,
  useWikiNav,
  writeStoredValue,
  type WikiNavEntry,
} from "../WikiTabState";

const runImmediately = async (action: () => void | Promise<void>) => {
  await action();
};

const blockAlways = async (_action: () => void | Promise<void>) => {
  // Simulates a dirty guard whose confirmLeave() resolved false.
};

afterEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
});

describe("persistence helpers", () => {
  it("exposes the plan-pinned storage keys", () => {
    expect(WIKI_TAB_KEYS.mode).toBe("gobby:wiki-tab:mode");
    expect(WIKI_TAB_KEYS.topic).toBe("gobby:wiki-tab:topic");
    expect(WIKI_TAB_KEYS.treeWidth).toBe("gobby:wiki-tab:tree-width");
    expect(WIKI_TAB_KEYS.split).toBe("gobby:wiki-tab:split");
    expect(WIKI_TAB_KEYS.lastPageWiki).toBe("gobby:wiki-tab:last-page:wiki");
    expect(WIKI_TAB_KEYS.lastPageCode).toBe("gobby:wiki-tab:last-page:code");
    expect(WIKI_TAB_KEYS.graph).toBe("gobby:wiki-tab:graph");
    expect(WIKI_TAB_KEYS.askHistory).toBe("gobby:wiki-tab:ask-history");
  });

  it("round-trips mode and defaults to wiki on missing or invalid values", () => {
    expect(loadStoredMode()).toBe("wiki");
    storeMode("ask");
    expect(window.localStorage.getItem(WIKI_TAB_KEYS.mode)).toBe("ask");
    expect(loadStoredMode()).toBe("ask");
    window.localStorage.setItem(WIKI_TAB_KEYS.mode, "bogus");
    expect(loadStoredMode()).toBe("wiki");
  });

  it("round-trips topic and clears it with null", () => {
    expect(loadStoredTopic()).toBeNull();
    storeTopic("payments");
    expect(loadStoredTopic()).toBe("payments");
    storeTopic(null);
    expect(loadStoredTopic()).toBeNull();
    expect(window.localStorage.getItem(WIKI_TAB_KEYS.topic)).toBeNull();
  });

  it("tracks last page per browse mode", () => {
    storeLastPage("wiki", "knowledge/concepts/gobby.md");
    storeLastPage("code", "code/files/src/gobby/runner.py.md");
    expect(loadLastPage("wiki")).toBe("knowledge/concepts/gobby.md");
    expect(loadLastPage("code")).toBe("code/files/src/gobby/runner.py.md");
  });

  it("tolerates a throwing storage backend", () => {
    const broken = {
      getItem: () => {
        throw new Error("quota");
      },
      setItem: () => {
        throw new Error("quota");
      },
      removeItem: () => {
        throw new Error("quota");
      },
    } as unknown as Storage;
    expect(readStoredValue("gobby:wiki-tab:mode", broken)).toBeNull();
    expect(() => writeStoredValue("gobby:wiki-tab:mode", "code", broken)).not.toThrow();
    expect(() => writeStoredValue("gobby:wiki-tab:mode", null, broken)).not.toThrow();
  });

  it("validates modes", () => {
    expect(isWikiMode("wiki")).toBe(true);
    expect(isWikiMode("research")).toBe(true);
    expect(isWikiMode("graph")).toBe(false);
    expect(isWikiMode(null)).toBe(false);
  });
});

describe("modeForPath", () => {
  it("routes code/ pages to code mode and everything else to wiki", () => {
    expect(modeForPath("code/files/src/gobby/runner.py.md")).toBe("code");
    expect(modeForPath("code/_architecture.md")).toBe("code");
    expect(modeForPath("knowledge/concepts/gobby.md")).toBe("wiki");
    expect(modeForPath("_index.md")).toBe("wiki");
  });
});

describe("useWikiNav", () => {
  it("pushes entries, derives mode from the path, and reports history state", async () => {
    const onNavigate = vi.fn<(entry: WikiNavEntry) => void>();
    const { result } = renderHook(() =>
      useWikiNav({ guardedRun: runImmediately, onNavigate }),
    );

    expect(result.current.current).toBeNull();
    expect(result.current.canBack).toBe(false);
    expect(result.current.canForward).toBe(false);

    await act(() => result.current.openPage("knowledge/concepts/gobby.md"));
    await act(() => result.current.openPage("code/files/src/gobby/runner.py.md"));

    expect(result.current.current).toEqual({
      path: "code/files/src/gobby/runner.py.md",
      mode: "code",
    });
    expect(result.current.canBack).toBe(true);
    expect(result.current.canForward).toBe(false);
    expect(onNavigate).toHaveBeenNthCalledWith(1, {
      path: "knowledge/concepts/gobby.md",
      mode: "wiki",
    });
    expect(onNavigate).toHaveBeenNthCalledWith(2, {
      path: "code/files/src/gobby/runner.py.md",
      mode: "code",
    });
  });

  it("honors an explicit mode override", async () => {
    const { result } = renderHook(() => useWikiNav({ guardedRun: runImmediately }));
    await act(() => result.current.openPage("outputs/GRAPH_REPORT.md", { mode: "code" }));
    expect(result.current.current?.mode).toBe("code");
  });

  it("moves back and forward through history and fires onNavigate for each move", async () => {
    const onNavigate = vi.fn<(entry: WikiNavEntry) => void>();
    const { result } = renderHook(() =>
      useWikiNav({ guardedRun: runImmediately, onNavigate }),
    );

    await act(() => result.current.openPage("a.md"));
    await act(() => result.current.openPage("b.md"));
    await act(() => result.current.back());

    expect(result.current.current?.path).toBe("a.md");
    expect(result.current.canForward).toBe(true);
    expect(onNavigate).toHaveBeenLastCalledWith({ path: "a.md", mode: "wiki" });

    await act(() => result.current.forward());
    expect(result.current.current?.path).toBe("b.md");
    expect(result.current.canForward).toBe(false);
  });

  it("truncates forward history when opening a page after back", async () => {
    const { result } = renderHook(() => useWikiNav({ guardedRun: runImmediately }));

    await act(() => result.current.openPage("a.md"));
    await act(() => result.current.openPage("b.md"));
    await act(() => result.current.back());
    await act(() => result.current.openPage("c.md"));

    expect(result.current.current?.path).toBe("c.md");
    expect(result.current.canForward).toBe(false);
    await act(() => result.current.back());
    expect(result.current.current?.path).toBe("a.md");
  });

  it("caps the history stack at the plan limit", async () => {
    const { result } = renderHook(() => useWikiNav({ guardedRun: runImmediately }));

    for (let index = 0; index < WIKI_NAV_HISTORY_CAP + 5; index += 1) {
      await act(() => result.current.openPage(`page-${index}.md`));
    }

    let steps = 0;
    while (result.current.canBack) {
      await act(() => result.current.back());
      steps += 1;
    }
    expect(steps).toBe(WIKI_NAV_HISTORY_CAP - 1);
    expect(result.current.current?.path).toBe("page-5.md");
  });

  it("ignores re-opening the current page", async () => {
    const onNavigate = vi.fn<(entry: WikiNavEntry) => void>();
    const { result } = renderHook(() =>
      useWikiNav({ guardedRun: runImmediately, onNavigate }),
    );

    await act(() => result.current.openPage("a.md"));
    await act(() => result.current.openPage("a.md"));

    expect(onNavigate).toHaveBeenCalledTimes(1);
    expect(result.current.canBack).toBe(false);
  });

  it("runs every transition through the dirty guard", async () => {
    const onNavigate = vi.fn<(entry: WikiNavEntry) => void>();
    const { result } = renderHook(() =>
      useWikiNav({ guardedRun: blockAlways, onNavigate }),
    );

    await act(() => result.current.openPage("a.md"));

    expect(result.current.current).toBeNull();
    expect(onNavigate).not.toHaveBeenCalled();
  });

  it("guards back and forward moves too", async () => {
    let blocked = false;
    const guardedRun = async (action: () => void | Promise<void>) => {
      if (!blocked) await action();
    };
    const { result } = renderHook(() => useWikiNav({ guardedRun }));

    await act(() => result.current.openPage("a.md"));
    await act(() => result.current.openPage("b.md"));
    blocked = true;
    await act(() => result.current.back());

    expect(result.current.current?.path).toBe("b.md");
  });
});
