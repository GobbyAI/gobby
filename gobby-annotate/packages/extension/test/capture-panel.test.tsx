// @vitest-environment jsdom
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { CapturePanel, SaveStatus } from "../src/CapturePanel";
import { fixture } from "../../core/test/fixtures";
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});
it("keeps save feedback steady through typing and reports failures immediately", () => {
  vi.useFakeTimers();
  const { rerender } = render(<SaveStatus status="Saved locally" />);
  expect(screen.getByRole("status").textContent).toBe("Saved locally");
  for (let i = 0; i < 4; i++) {
    rerender(<SaveStatus status="Saving…" />);
    rerender(<SaveStatus status="Saved locally" />);
    act(() => vi.advanceTimersByTime(200));
    expect(screen.getByRole("status").textContent).toBe("Saving…");
  }
  act(() => vi.advanceTimersByTime(600));
  expect(screen.getByRole("status").textContent).toBe("Saved locally");
  rerender(<SaveStatus status="Saving…" />);
  rerender(<SaveStatus status="Saved locally" />);
  rerender(<SaveStatus status="Unsaved changes" />);
  expect(screen.getByRole("status").textContent).toBe("Unsaved changes");
  act(() => vi.advanceTimersByTime(1000));
  expect(screen.getByRole("status").textContent).toBe("Unsaved changes");
});
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
