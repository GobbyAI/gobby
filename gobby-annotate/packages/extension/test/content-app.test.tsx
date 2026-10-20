// @vitest-environment jsdom
import { cleanup, fireEvent, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { activate } from "../src/content";
import type { Batch } from "../src/drafts";

const HOST_ID = "gobby-annotate-root";
const batch: Batch = {
  id: "batch-1",
  title: "Checkout review",
  updatedAt: "2026-10-15T10:00:00.000Z",
  annotations: [],
};

let deactivate: (() => void) | undefined;
let release: (() => void) | undefined;

afterEach(() => {
  deactivate?.();
  deactivate = undefined;
  release = undefined;
  cleanup();
  vi.unstubAllGlobals();
});

/** Stubs the browser surface Annotate touches. `list` stays pending until `release()`. */
function stubBrowser(osPrefersLight: boolean) {
  const pending: Array<() => void> = [];
  release = () => pending.splice(0).forEach((resolve) => resolve());
  vi.stubGlobal("chrome", {
    runtime: {
      sendMessage: vi.fn(
        (message: { type: string }) =>
          new Promise((resolve) => {
            const value = message.type === "list" ? [batch] : batch;
            pending.push(() => resolve({ ok: true, value }));
          }),
      ),
      onMessage: { addListener: vi.fn(), removeListener: vi.fn() },
    },
  });
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: osPrefersLight && query.includes("light"),
    media: query,
    addEventListener: () => {},
    removeEventListener: () => {},
  }));
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      disconnect() {}
    },
  );
  vi.stubGlobal("visualViewport", {
    width: 1024,
    height: 768,
    offsetLeft: 0,
    offsetTop: 0,
    scale: 1,
    addEventListener: () => {},
    removeEventListener: () => {},
  });
}

async function openMenu() {
  const host = document.getElementById(HOST_ID)!;
  const shadow = within(host.shadowRoot as unknown as HTMLElement);
  await waitFor(() =>
    shadow.getByRole("button", { name: "Batch and export menu" }),
  );
  fireEvent.click(
    shadow.getByRole("button", { name: "Batch and export menu" }),
  );
  return { host, shadow };
}

it("opens dark even when the operating system prefers light", async () => {
  stubBrowser(true);
  deactivate = activate();
  const host = document.getElementById(HOST_ID)!;
  expect(host.dataset.theme).toBe("dark");
});

it("flips the theme and its pressed state when Light theme is activated", async () => {
  stubBrowser(false);
  deactivate = activate();
  const { host, shadow } = await openMenu();
  const toggle = shadow.getByRole("button", { name: "Light theme" });
  expect(toggle.getAttribute("aria-pressed")).toBe("false");
  fireEvent.click(toggle);
  expect(host.dataset.theme).toBe("light");
  expect(toggle.getAttribute("aria-pressed")).toBe("true");
  fireEvent.click(toggle);
  expect(host.dataset.theme).toBe("dark");
  expect(toggle.getAttribute("aria-pressed")).toBe("false");
});

it("explains why Export ZIP is disabled until the batch loads", async () => {
  stubBrowser(false);
  deactivate = activate();
  const { shadow } = await openMenu();
  const exportButton = shadow.getByRole("button", { name: "Export ZIP" });
  expect((exportButton as HTMLButtonElement).disabled).toBe(true);
  const reason = shadow.getByText(
    "Export is unavailable until this batch loads.",
  );
  expect(exportButton.getAttribute("aria-describedby")).toBe(reason.id);
  release?.();
  await waitFor(() =>
    expect((exportButton as HTMLButtonElement).disabled).toBe(false),
  );
  expect(exportButton.getAttribute("aria-describedby")).toBeNull();
});

it("orders the batch menu as Export, then batch actions, then panel controls", async () => {
  stubBrowser(false);
  deactivate = activate();
  const { shadow } = await openMenu();
  const exportButton = shadow.getByRole("button", { name: "Export ZIP" });
  const batchActions = shadow.getByRole("group", { name: "Batch actions" });
  const panelControls = shadow.getByRole("group", { name: "Panel controls" });
  expect(
    within(batchActions).getByRole("button", { name: "New batch" }),
  ).toBeTruthy();
  for (const name of [
    "Dock left",
    "Dock right",
    "Light theme",
    "Deactivate annotation",
  ]) {
    expect(within(panelControls).getByRole("button", { name })).toBeTruthy();
  }
  expect(
    exportButton.compareDocumentPosition(batchActions) &
      Node.DOCUMENT_POSITION_FOLLOWING,
  ).toBeTruthy();
  expect(
    batchActions.compareDocumentPosition(panelControls) &
      Node.DOCUMENT_POSITION_FOLLOWING,
  ).toBeTruthy();
});
