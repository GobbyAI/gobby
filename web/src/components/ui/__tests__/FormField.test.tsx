import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { FormField } from "../FormField";
import { Input } from "../Input";

describe("FormField", () => {
  it("associates the label with the control through the render-prop id", () => {
    render(
      <FormField label="Display name">
        {({ id, describedBy, invalid }) => (
          <Input id={id} aria-describedby={describedBy} error={invalid} />
        )}
      </FormField>,
    );
    const control = screen.getByLabelText("Display name");
    expect(control).toBeInstanceOf(HTMLInputElement);
  });

  it("wires the hint into aria-describedby", () => {
    render(
      <FormField label="Port" hint="1024-65535">
        {({ id, describedBy }) => (
          <Input id={id} aria-describedby={describedBy} />
        )}
      </FormField>,
    );
    const control = screen.getByLabelText("Port");
    const hint = screen.getByText("1024-65535");
    expect(hint.id).not.toBe("");
    expect(control.getAttribute("aria-describedby")).toBe(hint.id);
  });

  it("wires the error into aria-describedby and marks the control invalid", () => {
    render(
      <FormField label="Port" error="Out of range">
        {({ id, describedBy, invalid }) => (
          <Input id={id} aria-describedby={describedBy} error={invalid} />
        )}
      </FormField>,
    );
    const control = screen.getByLabelText("Port");
    const error = screen.getByText("Out of range");
    expect(control.getAttribute("aria-describedby")).toBe(error.id);
    expect(control).toHaveAttribute("aria-invalid", "true");
  });

  it("lists hint then error when both are present", () => {
    render(
      <FormField label="Port" hint="1024-65535" error="Out of range">
        {({ id, describedBy }) => (
          <Input id={id} aria-describedby={describedBy} />
        )}
      </FormField>,
    );
    const control = screen.getByLabelText("Port");
    const hintId = screen.getByText("1024-65535").id;
    const errorId = screen.getByText("Out of range").id;
    expect(control.getAttribute("aria-describedby")).toBe(
      `${hintId} ${errorId}`,
    );
  });

  it("omits aria-describedby when there is no hint or error", () => {
    render(
      <FormField label="Port">
        {({ id, describedBy }) => (
          <Input id={id} aria-describedby={describedBy} />
        )}
      </FormField>,
    );
    expect(screen.getByLabelText("Port")).not.toHaveAttribute(
      "aria-describedby",
    );
  });

  it("renders composite fields as a labelled group", () => {
    render(
      <FormField label="Tags" group>
        {() => <span>chips</span>}
      </FormField>,
    );
    const group = screen.getByRole("group", { name: "Tags" });
    // The group label is plain text, not a <label> pointing at nothing.
    expect(group.querySelector("label")).toBeNull();
  });

  it("merges a caller className onto the shell", () => {
    const { container } = render(
      <FormField label="Name" className="gap-3">
        {({ id }) => <Input id={id} />}
      </FormField>,
    );
    const shell = container.firstElementChild!;
    expect(shell.className).toContain("gap-3");
    expect(shell.className).not.toContain("gap-1.5");
  });
});
