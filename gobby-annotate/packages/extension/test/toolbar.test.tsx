// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { Toolbar } from "../src/Toolbar";
afterEach(cleanup);
const props = {
  count: 2,
  collapsed: false,
  mode: "browse" as const,
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
it("collapses to one accessible control", () => {
  render(<Toolbar {...props} collapsed />);
  expect(screen.getAllByRole("button")).toHaveLength(1);
  fireEvent.click(
    screen.getByRole("button", { name: "Expand Gobby Annotate" }),
  );
  expect(props.onCollapse).toHaveBeenCalled();
});
