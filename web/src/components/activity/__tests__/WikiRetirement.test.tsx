import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { ACTIVITY_PANEL_TABS } from "../ActivityPanelTabs";
import { SHOW_ACTIVITY_TAB_EVENT } from "../activityEvents";
import { useActivityPanel } from "../useActivityPanel";

const TAB_KEY = "gobby-activity-panel-tab-v2";

beforeEach(() => localStorage.clear());
afterEach(() => localStorage.clear());

describe("wiki retirement", () => {
  it.each([false, true])(
    "restores the normal default for a saved wiki tab (mobile: %s)",
    (isMobile) => {
      const fresh = renderHook(() => useActivityPanel(isMobile));
      const normalDefault = fresh.result.current.activeTab;
      fresh.unmount();
      localStorage.setItem(TAB_KEY, "wiki");

      const { result } = renderHook(() => useActivityPanel(isMobile));

      expect(result.current.activeTab).toBe(normalDefault);
      expect(localStorage.getItem(TAB_KEY)).toBe(normalDefault);
    },
  );

  it.each(ACTIVITY_PANEL_TABS.map(({ id }) => id))(
    "preserves the saved %s selection",
    (tab) => {
      localStorage.setItem(TAB_KEY, tab);

      const { result } = renderHook(() => useActivityPanel(false));

      expect(result.current.activeTab).toBe(tab);
      expect(localStorage.getItem(TAB_KEY)).toBe(tab);
    },
  );

  it("ignores a stale request to open the wiki tab", () => {
    localStorage.setItem(TAB_KEY, "memory");
    const { result } = renderHook(() => useActivityPanel(false));

    act(() => {
      window.dispatchEvent(
        new CustomEvent(SHOW_ACTIVITY_TAB_EVENT, { detail: { tab: "wiki" } }),
      );
    });

    expect(result.current.activeTab).toBe("memory");
    expect(localStorage.getItem(TAB_KEY)).toBe("memory");
  });
});
