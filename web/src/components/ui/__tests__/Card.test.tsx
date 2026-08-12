import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Card } from "../Card";

const INTERACTIVE_SELECTOR = "button, a, input, select, textarea, [tabindex]";

describe("Card", () => {
  it("renders a div with the shared card shell", () => {
    render(<Card data-testid="card">body</Card>);

    const card = screen.getByTestId("card");
    expect(card.tagName).toBe("DIV");
    expect(card).toHaveClass(
      "rounded-lg",
      "border",
      "border-border",
      "bg-background",
    );
  });

  it("defaults to no padding", () => {
    render(<Card data-testid="card">body</Card>);

    const card = screen.getByTestId("card");
    expect(card).not.toHaveClass("p-3");
    expect(card).not.toHaveClass("p-4");
  });

  it.each([
    ["sm", "p-3"],
    ["md", "p-4"],
  ] as const)("applies the %s padding step", (padding, cls) => {
    render(
      <Card data-testid="card" padding={padding}>
        body
      </Card>,
    );

    expect(screen.getByTestId("card")).toHaveClass(cls);
  });

  it("stays a non-focusable div when not interactive", () => {
    render(<Card data-testid="card">body</Card>);

    const card = screen.getByTestId("card");
    expect(card).not.toHaveAttribute("tabindex");
    expect(card.className).not.toContain("cursor-pointer");
  });

  it("renders interactive cards as a semantic focusable button host", () => {
    render(<Card interactive>open task</Card>);

    const card = screen.getByRole("button", { name: "open task" });
    expect(card.tagName).toBe("BUTTON");
    expect(card).toHaveAttribute("type", "button");
    expect(card.className).toContain("cursor-pointer");
    expect(card.className).toContain("focus-visible:ring-2");
    expect(card.className).toContain("focus-visible:ring-accent");
  });

  it("does not nest interactive elements inside an interactive card", () => {
    render(
      <Card interactive>
        <span>title</span>
        <span>meta</span>
      </Card>,
    );

    const card = screen.getByRole("button");
    expect(card.querySelector(INTERACTIVE_SELECTOR)).toBeNull();
  });

  it("renders through the caller element with asChild", () => {
    render(
      <Card asChild padding="md">
        <section data-testid="card">body</section>
      </Card>,
    );

    const card = screen.getByTestId("card");
    expect(card.tagName).toBe("SECTION");
    expect(card).toHaveClass("rounded-lg", "border-border", "p-4");
  });

  it("lets asChild supply the semantic host for interactive cards", () => {
    render(
      <Card asChild interactive>
        <a href="/tasks/42">open task</a>
      </Card>,
    );

    const card = screen.getByRole("link", { name: "open task" });
    expect(card.className).toContain("cursor-pointer");
    expect(card.querySelector(INTERACTIVE_SELECTOR)).toBeNull();
  });

  it("lets call-site background overrides win over the base surface", () => {
    render(<Card data-testid="card" className="animate-pulse bg-muted/30" />);

    const card = screen.getByTestId("card");
    expect(card).toHaveClass("animate-pulse", "bg-muted/30");
    expect(card).not.toHaveClass("bg-background");
  });
});
