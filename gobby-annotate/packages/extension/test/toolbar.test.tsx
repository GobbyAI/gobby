// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, expect, it, vi } from "vitest";
import { Toolbar } from "../src/Toolbar";
import type { Mode } from "../src/selection";
afterEach(cleanup);
const props = {
  count: 2,
  collapsed: false,
  mode: "browse" as const,
  panel: "none" as const,
  onMode: vi.fn(),
  onList: vi.fn(),
  onMenu: vi.fn(),
  onCollapse: vi.fn(),
  onDrag: vi.fn(),
};
it("exposes compact named actions and an annotation count", () => {
  render(<Toolbar {...props} />);
  fireEvent.click(screen.getByRole("button", { name: "Select element" }));
  expect(props.onMode).toHaveBeenCalledWith("element");
  expect(screen.getByRole("button", { name: "Annotations (2)" })).toBeTruthy();
  expect(screen.getAllByRole("button")).toHaveLength(6);
});
function Harness() {
  const [mode, setMode] = useState<Mode>("browse");
  const [panel, setPanel] = useState<"none" | "list" | "menu">("none");
  return (
    <Toolbar
      {...props}
      mode={mode}
      panel={panel}
      onMode={setMode}
      onList={() => setPanel(panel === "list" ? "none" : "list")}
      onMenu={() => setPanel(panel === "menu" ? "none" : "menu")}
    />
  );
}
it("flips aria-pressed and aria-expanded when each toggle is activated", () => {
  render(<Harness />);
  const element = screen.getByRole("button", { name: "Select element" });
  const rectangle = screen.getByRole("button", { name: "Select rectangle" });
  const list = screen.getByRole("button", { name: "Annotations (2)" });
  const menu = screen.getByRole("button", { name: "Batch and export menu" });
  expect(element.getAttribute("aria-pressed")).toBe("false");
  fireEvent.click(element);
  expect(element.getAttribute("aria-pressed")).toBe("true");
  expect(screen.getAllByRole("button")).toHaveLength(6);
  fireEvent.click(rectangle);
  expect(element.getAttribute("aria-pressed")).toBe("false");
  expect(rectangle.getAttribute("aria-pressed")).toBe("true");
  fireEvent.click(rectangle);
  expect(rectangle.getAttribute("aria-pressed")).toBe("false");
  expect(list.getAttribute("aria-expanded")).toBe("false");
  fireEvent.click(list);
  expect(list.getAttribute("aria-expanded")).toBe("true");
  fireEvent.click(menu);
  expect(menu.getAttribute("aria-expanded")).toBe("true");
  expect(list.getAttribute("aria-expanded")).toBe("false");
  fireEvent.click(menu);
  expect(menu.getAttribute("aria-expanded")).toBe("false");
});
it("collapses to one accessible control", () => {
  render(<Toolbar {...props} collapsed />);
  expect(screen.getAllByRole("button")).toHaveLength(1);
  fireEvent.click(
    screen.getByRole("button", { name: "Expand Gobby Annotate" }),
  );
  expect(props.onCollapse).toHaveBeenCalled();
});
