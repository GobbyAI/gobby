// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { Batches } from "../src/Batches";
import type { Batch } from "../src/drafts";
import { fixture } from "../../core/test/fixtures";
afterEach(cleanup);

const note = fixture().manifest.annotations[0]!;
const exported: Batch = {
  id: "exported",
  title: "Checkout review",
  updatedAt: "2026-10-15T10:00:00.000Z",
  annotations: [note],
  exports: [{ id: "e1", at: "2026-10-15T10:00:00.000Z", staged: false }],
};
const draft: Batch = {
  id: "draft",
  title: "Pricing page",
  updatedAt: "2026-10-15T11:00:00.000Z",
  annotations: [note, { ...note, id: "second" }],
  exports: [{ id: "e2", at: "2026-10-15T10:30:00.000Z", staged: true }],
};
function setup() {
  const handlers = {
    onOpen: vi.fn(),
    onRemove: vi.fn(),
    onClearExported: vi.fn(),
  };
  render(
    <Batches batches={[exported, draft]} current="exported" {...handlers} />,
  );
  return handlers;
}

it("lists each batch with its export state and reopens it", () => {
  const { onOpen } = setup();
  const open = screen.getByRole("button", { name: "Checkout review" });
  expect(open.getAttribute("aria-current")).toBe("true");
  expect(
    screen
      .getByRole("button", { name: "Pricing page" })
      .hasAttribute("aria-current"),
  ).toBe(false);
  const rows = screen.getAllByRole("listitem");
  expect(rows[0]!.textContent).toContain("Exported");
  expect(rows[1]!.textContent).toContain("Draft");
  fireEvent.click(screen.getByRole("button", { name: "Pricing page" }));
  expect(onOpen).toHaveBeenCalledWith("draft");
});

it("deletes a batch only after confirmation and cancels without deleting", () => {
  const { onRemove } = setup();
  fireEvent.click(
    screen.getByRole("button", { name: "Delete batch Pricing page" }),
  );
  const dialog = screen.getByRole("alertdialog");
  expect(dialog.textContent).toContain("removes its 2 notes");
  expect(dialog.textContent).toContain(
    "staged ZIP from the Gobby Annotate app",
  );
  expect(dialog.textContent).toContain("never exported");
  const cancel = screen.getByRole("button", { name: "Cancel" });
  expect(document.activeElement).toBe(cancel);
  fireEvent.click(cancel);
  expect(screen.queryByRole("alertdialog")).toBeNull();
  expect(onRemove).not.toHaveBeenCalled();
  fireEvent.click(
    screen.getByRole("button", { name: "Delete batch Pricing page" }),
  );
  fireEvent.click(screen.getByRole("button", { name: "Delete batch" }));
  expect(onRemove).toHaveBeenCalledWith("draft");
  expect(screen.queryByRole("alertdialog")).toBeNull();
});

it("clears only exported batches after confirmation", () => {
  const { onClearExported, onRemove } = setup();
  fireEvent.click(
    screen.getByRole("button", { name: "Clear exported batches (1)" }),
  );
  expect(screen.getByRole("alertdialog").textContent).toContain(
    "Drafts with unexported changes stay",
  );
  fireEvent.click(screen.getByRole("button", { name: "Delete 1 exported" }));
  expect(onClearExported).toHaveBeenCalledOnce();
  expect(onRemove).not.toHaveBeenCalled();
});
