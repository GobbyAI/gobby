import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TickerText } from "../TickerText";

function stubWidths(scrollWidth: number, clientWidth: number) {
  vi.spyOn(HTMLElement.prototype, "scrollWidth", "get").mockReturnValue(
    scrollWidth,
  );
  vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(
    clientWidth,
  );
}

function root() {
  return document.documentElement;
}

describe("TickerText", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    root().removeAttribute("data-ticker-active");
    root().style.removeProperty("--ticker-span");
    root().style.removeProperty("--ticker-cycle");
  });

  it("stays static and leaves the clock idle when the text fits its slot", () => {
    stubWidths(120, 200);
    const { container } = render(<TickerText>fits</TickerText>);
    const ticker = container.firstElementChild as HTMLElement;

    expect(ticker).toHaveClass("ticker");
    expect(ticker).not.toHaveClass("ticker--overflow");
    expect(ticker).toHaveTextContent("fits");
    // Nothing can move, so the panel runs no animation at all.
    expect(root()).not.toHaveAttribute("data-ticker-active");
  });

  it("publishes its own travel distance and starts the shared clock", () => {
    stubWidths(320, 200);
    const { container } = render(<TickerText>too long</TickerText>);
    const ticker = container.firstElementChild as HTMLElement;

    // 120px of overflow plus the 20px edge mask the slide has to clear.
    expect(ticker).toHaveClass("ticker--overflow");
    expect(ticker.style.getPropertyValue("--ticker-overflow")).toBe("140px");
    expect(root()).toHaveAttribute("data-ticker-active");
    expect(root().style.getPropertyValue("--ticker-span")).toBe("140px");
  });

  it("forwards style and data attributes so callers keep their own presentation", () => {
    stubWidths(120, 200);
    const { container } = render(
      <TickerText data-task-row-title style={{ color: "rgb(1, 2, 3)" }}>
        titled
      </TickerText>,
    );
    const ticker = container.firstElementChild as HTMLElement;

    expect(ticker).toHaveAttribute("data-task-row-title");
    expect(ticker.style.color).toBe("rgb(1, 2, 3)");
  });

  it("releases its distance when it unmounts", () => {
    stubWidths(320, 200);
    const { unmount } = render(<TickerText>too long</TickerText>);
    expect(root()).toHaveAttribute("data-ticker-active");

    unmount();
    expect(root()).not.toHaveAttribute("data-ticker-active");
  });
});
