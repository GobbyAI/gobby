import { act, render, renderHook, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { WikiTab } from "../../WikiTab";
import { DirtyGuardProvider } from "../../DirtyGuardContext";
import type { DirtyGuard, DirtyGuardContextValue } from "../../dirtyGuard";
import { WIKI_TAB_KEYS, useWikiNav } from "../WikiTabState";
import {
  degradedStatusEnvelope,
  healthEnvelope,
  sourcesEnvelope,
  statusEnvelope,
} from "./fixtures";

class MockResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status < 400,
    status,
    json: async () => body,
  } as Response;
}

type FetchOverrides = Partial<Record<"status" | "health" | "sources", Response>>;

function stubWikiFetch(overrides: FetchOverrides = {}) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/api/wiki/status")) {
      return overrides.status ?? jsonResponse(statusEnvelope);
    }
    if (url.includes("/api/wiki/health")) {
      return overrides.health ?? jsonResponse(healthEnvelope);
    }
    if (url.includes("/api/wiki/sources")) {
      return overrides.sources ?? jsonResponse(sourcesEnvelope);
    }
    return jsonResponse({ ok: true, payload: {} });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function makeGuardValue(guards: DirtyGuard[]): DirtyGuardContextValue {
  const registered = new Set<DirtyGuard>(guards);
  return {
    registerDirtyGuard: (guard) => {
      registered.add(guard);
      return () => {
        registered.delete(guard);
      };
    },
    guardedRun: async (action) => {
      for (const guard of registered) {
        if (guard.isDirty() && !(await guard.confirmLeave())) return;
      }
      await action();
    },
  };
}

function renderShell(ui: ReactNode) {
  return render(ui);
}

beforeEach(() => {
  vi.stubGlobal("ResizeObserver", MockResizeObserver);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  window.localStorage.clear();
  window.sessionStorage.clear();
});

describe("WikiTab shell", () => {
  it("renders the three-mode segmented control and defaults to wiki mode", async () => {
    stubWikiFetch();
    renderShell(<WikiTab projectId="p1" />);

    const group = await screen.findByRole("radiogroup", { name: /wiki mode/i });
    expect(group).toBeInTheDocument();
    for (const label of ["Wiki", "Code", "Ask"]) {
      expect(screen.getByRole("radio", { name: label })).toBeInTheDocument();
    }
    expect(screen.queryByRole("radio", { name: "Research" })).not.toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Wiki" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
  });

  it("persists mode switches and restores the stored mode on mount", async () => {
    stubWikiFetch();
    const user = userEvent.setup();
    const { unmount } = renderShell(<WikiTab projectId="p1" />);

    await user.click(await screen.findByRole("radio", { name: "Code" }));
    expect(window.localStorage.getItem(WIKI_TAB_KEYS.mode)).toBe("code");
    unmount();

    renderShell(<WikiTab projectId="p1" />);
    expect(await screen.findByRole("radio", { name: "Code" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
  });

  it("blocks mode switches while a registered dirty guard declines", async () => {
    stubWikiFetch();
    const user = userEvent.setup();
    const confirmLeave = vi.fn(async () => false);
    const guardValue = makeGuardValue([{ isDirty: () => true, confirmLeave }]);

    renderShell(
      <DirtyGuardProvider value={guardValue}>
        <WikiTab projectId="p1" />
      </DirtyGuardProvider>,
    );

    await user.click(await screen.findByRole("radio", { name: "Ask" }));
    expect(confirmLeave).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("radio", { name: "Wiki" })).toHaveAttribute(
      "aria-checked",
      "true",
    );

    confirmLeave.mockResolvedValueOnce(true);
    await user.click(screen.getByRole("radio", { name: "Ask" }));
    await waitFor(() =>
      expect(screen.getByRole("radio", { name: "Ask" })).toHaveAttribute(
        "aria-checked",
        "true",
      ),
    );
  });

  it("guards page navigation through the same dirty-guard contract", async () => {
    const confirmLeave = vi.fn(async () => false);
    const guardValue = makeGuardValue([{ isDirty: () => true, confirmLeave }]);
    const { result } = renderHook(() =>
      useWikiNav({ guardedRun: guardValue.guardedRun }),
    );

    await act(() => result.current.openPage("knowledge/concepts/gobby.md"));
    expect(confirmLeave).toHaveBeenCalledTimes(1);
    expect(result.current.current).toBeNull();

    confirmLeave.mockResolvedValueOnce(true);
    await act(() => result.current.openPage("knowledge/concepts/gobby.md"));
    expect(result.current.current?.path).toBe("knowledge/concepts/gobby.md");
  });

  it("shows the degraded banner with the affected services", async () => {
    stubWikiFetch({ status: jsonResponse(degradedStatusEnvelope) });
    renderShell(<WikiTab projectId="p1" />);

    const banner = await screen.findByText(/wiki degraded/i);
    expect(banner.textContent).toMatch(/embeddings/);
    expect(banner.textContent).toMatch(/falkordb/);
  });

  it("disables the ask composer when the gateway is unavailable", async () => {
    window.localStorage.setItem(WIKI_TAB_KEYS.mode, "ask");
    stubWikiFetch({
      status: jsonResponse({ detail: "wiki gateway offline" }, 503),
      health: jsonResponse({ detail: "wiki gateway offline" }, 503),
      sources: jsonResponse({ detail: "wiki gateway offline" }, 503),
    });
    renderShell(<WikiTab projectId="p1" />);

    expect(await screen.findByText(/gateway offline/i)).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: /ask the wiki/i })).toBeDisabled();
  });

  it("opens the sources manager from the kebab menu", async () => {
    stubWikiFetch();
    const user = userEvent.setup();
    renderShell(<WikiTab projectId="p1" />);

    await user.click(await screen.findByRole("button", { name: "Wiki actions" }));
    await user.click(await screen.findByRole("menuitem", { name: /manage sources/i }));

    expect(
      await screen.findByRole("heading", { name: /wiki sources/i }),
    ).toBeInTheDocument();
    expect(await screen.findByText("Session: 019efb0c")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /back to wiki/i }));
    expect(screen.queryByRole("heading", { name: /wiki sources/i })).not.toBeInTheDocument();
  });
});
