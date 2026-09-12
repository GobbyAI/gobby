// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { CapturePanel } from "../src/CapturePanel";
import { fixture } from "../../core/test/fixtures";
afterEach(cleanup);
it("authors comments and exposes save status independently of input text", () => {
  const edit = vi.fn();
  render(
    <CapturePanel
      annotation={fixture().manifest.annotations[0]!}
      image={null}
      status="Unsaved changes"
      onEdit={edit}
      onClose={vi.fn()}
      onDelete={vi.fn()}
    />,
  );
  fireEvent.change(screen.getByLabelText("What should change?"), {
    target: { value: "New comment" },
  });
  expect(edit).toHaveBeenCalledWith("New comment");
  expect(screen.getByRole("status").textContent).toBe("Unsaved changes");
  expect(
    screen.getByRole("button", { name: "Delete annotation" }),
  ).toBeTruthy();
});
