import { render } from "@testing-library/react";
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

describe("TickerText", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("stays static when the text fits its slot", () => {
    stubWidths(120, 200);
    const { container } = render(<TickerText>fits</TickerText>);
    const ticker = container.firstElementChild as HTMLElement;
    expect(ticker).toHaveClass("ticker");
    expect(ticker).not.toHaveClass("ticker--overflow");
    expect(ticker).toHaveTextContent("fits");
  });

  it("slides by the overflow plus the edge mask when the text is wider than its slot", () => {
    stubWidths(320, 200);
    const { container } = render(<TickerText>too long</TickerText>);
    const ticker = container.firstElementChild as HTMLElement;
    expect(ticker).toHaveClass("ticker--overflow");
    expect(ticker.style.getPropertyValue("--ticker-distance")).toBe("-140px");
    expect(ticker.style.getPropertyValue("--ticker-duration")).toBe("6s");
  });
});
