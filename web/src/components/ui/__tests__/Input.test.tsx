import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createRef } from "react";
import { describe, expect, it, vi } from "vitest";
import { Input } from "../Input";

describe("Input", () => {
  it("renders a native input and forwards value changes", async () => {
    const onChange = vi.fn();
    render(<Input aria-label="Name" value="" onChange={onChange} />);
    await userEvent.type(screen.getByRole("textbox", { name: "Name" }), "a");
    expect(onChange).toHaveBeenCalled();
  });

  it("forwards its ref to the real HTMLInputElement", () => {
    const ref = createRef<HTMLInputElement>();
    render(<Input ref={ref} aria-label="Name" />);
    expect(ref.current).toBeInstanceOf(HTMLInputElement);
  });

  it("marks the control invalid and switches the border when error is set", () => {
    render(<Input aria-label="Name" error />);
    const input = screen.getByRole("textbox", { name: "Name" });
    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(input.className).toContain("border-destructive");
    expect(input.className).not.toContain("border-border");
  });

  it("stays valid with the default border when error is unset", () => {
    render(<Input aria-label="Name" />);
    const input = screen.getByRole("textbox", { name: "Name" });
    expect(input).toHaveAttribute("aria-invalid", "false");
    expect(input.className).toContain("border-border");
  });

  it("lets a caller className win conflicting utilities via twMerge", () => {
    render(<Input aria-label="Name" className="h-11" />);
    const input = screen.getByRole("textbox", { name: "Name" });
    expect(input.className).toContain("h-11");
    expect(input.className).not.toContain("h-9");
  });

  it("wraps the control in a label carrying the invisible coarse hit-area expansion", () => {
    render(<Input aria-label="Name" />);
    const wrapper = screen
      .getByRole("textbox", { name: "Name" })
      .closest("label");
    expect(wrapper).not.toBeNull();
    expect(wrapper!.className).toContain("pointer-coarse:before:min-h-11");
    expect(wrapper!.className).toContain("pointer-coarse:before:min-w-11");
  });

  it("applies wrapperClassName to the wrapper, not the control", () => {
    render(<Input aria-label="Name" wrapperClassName="flex-1" />);
    const input = screen.getByRole("textbox", { name: "Name" });
    expect(input.className).not.toContain("flex-1");
    expect(input.closest("label")!.className).toContain("flex-1");
  });

  it("passes native props through to the control", () => {
    render(
      <Input aria-label="Name" type="password" placeholder="Secret" disabled />,
    );
    const input = screen.getByLabelText("Name");
    expect(input).toHaveAttribute("type", "password");
    expect(input).toHaveAttribute("placeholder", "Secret");
    expect(input).toBeDisabled();
  });
});
