import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  loadTasksViewMode,
  useEffectiveTasksViewMode,
} from "../useEffectiveTasksViewMode";

const VIEW_MODE_KEY = "gobby-tasks-view-mode";

describe("loadTasksViewMode", () => {
  beforeEach(() => localStorage.clear());

  it("defaults to list when nothing is stored", () => {
    expect(loadTasksViewMode()).toBe("list");
  });

  it("returns board only when explicitly stored", () => {
    localStorage.setItem(VIEW_MODE_KEY, "board");
    expect(loadTasksViewMode()).toBe("board");
  });

  it("falls back to list for unrecognized values", () => {
    localStorage.setItem(VIEW_MODE_KEY, "kanban");
    expect(loadTasksViewMode()).toBe("list");
  });
});

describe("useEffectiveTasksViewMode — desktop", () => {
  beforeEach(() => localStorage.clear());

  it("starts from the persisted mode; effective === viewMode", () => {
    localStorage.setItem(VIEW_MODE_KEY, "board");
    const { result } = renderHook(() => useEffectiveTasksViewMode(false));
    expect(result.current.viewMode).toBe("board");
    expect(result.current.effectiveViewMode).toBe("board");
  });

  it("setViewMode persists the desktop key", () => {
    const { result } = renderHook(() => useEffectiveTasksViewMode(false));
    expect(result.current.effectiveViewMode).toBe("list");

    act(() => result.current.setViewMode("board"));

    expect(result.current.effectiveViewMode).toBe("board");
    expect(localStorage.getItem(VIEW_MODE_KEY)).toBe("board");
  });
});

describe("useEffectiveTasksViewMode — mobile", () => {
  beforeEach(() => localStorage.clear());

  it("forces list on an initial mobile render despite a stored board", () => {
    localStorage.setItem(VIEW_MODE_KEY, "board");
    const { result } = renderHook(() => useEffectiveTasksViewMode(true));
    expect(result.current.viewMode).toBe("board");
    expect(result.current.effectiveViewMode).toBe("list");
  });

  it("never writes the desktop key while mobile", () => {
    localStorage.setItem(VIEW_MODE_KEY, "board");
    const setItem = vi.spyOn(Storage.prototype, "setItem");

    renderHook(() => useEffectiveTasksViewMode(true));

    expect(
      setItem.mock.calls.some(([key]) => key === VIEW_MODE_KEY),
    ).toBe(false);
    expect(localStorage.getItem(VIEW_MODE_KEY)).toBe("board");
    setItem.mockRestore();
  });

  it("crossing desktop -> mobile clamps to list, key intact", () => {
    localStorage.setItem(VIEW_MODE_KEY, "board");
    const { result, rerender } = renderHook(
      ({ isMobile }: { isMobile: boolean }) =>
        useEffectiveTasksViewMode(isMobile),
      { initialProps: { isMobile: false } },
    );

    expect(result.current.effectiveViewMode).toBe("board");

    rerender({ isMobile: true });
    expect(result.current.effectiveViewMode).toBe("list");
    expect(localStorage.getItem(VIEW_MODE_KEY)).toBe("board");
  });

  it("crossing mobile -> desktop restores the persisted board verbatim", () => {
    localStorage.setItem(VIEW_MODE_KEY, "board");
    const { result, rerender } = renderHook(
      ({ isMobile }: { isMobile: boolean }) =>
        useEffectiveTasksViewMode(isMobile),
      { initialProps: { isMobile: true } },
    );

    expect(result.current.effectiveViewMode).toBe("list");

    rerender({ isMobile: false });
    expect(result.current.viewMode).toBe("board");
    expect(result.current.effectiveViewMode).toBe("board");
  });
});
