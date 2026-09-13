import { cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TickerText } from "../TickerText";

function stubWidths({
  content,
  slot,
  scroll = content,
}: {
  content: number;
  slot: number;
  scroll?: number;
}) {
  vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(
    content,
  );
  vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(slot);
  vi.spyOn(HTMLElement.prototype, "scrollWidth", "get").mockReturnValue(scroll);
}

function root() {
  return document.documentElement;
}

// jsdom has no Web Animations; the clock only needs animate() and cancel().
const cancel = vi.fn();
const animate = vi.fn<(keyframes: Keyframe[]) => Partial<Animation>>(() => ({
  cancel,
  startTime: null,
}));

describe("TickerText", () => {
  beforeEach(() => {
    Object.defineProperty(Element.prototype, "animate", {
      configurable: true,
      writable: true,
      value: animate,
    });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    animate.mockClear();
    cancel.mockClear();
    delete (Element.prototype as Partial<Element>).animate;
    root().removeAttribute("data-ticker-active");
  });

  it("stays static and leaves the clock idle when the text fits its slot", () => {
    stubWidths({ content: 120, slot: 200 });
    const { container } = render(<TickerText>fits</TickerText>);
    const ticker = container.firstElementChild as HTMLElement;

    expect(ticker).toHaveClass("ticker");
    expect(ticker).not.toHaveClass("ticker--overflow");
    expect(ticker).toHaveTextContent("fits");
    // Nothing can move, so no animation runs at all.
    expect(animate).not.toHaveBeenCalled();
    expect(root()).not.toHaveAttribute("data-ticker-active");
  });

  it("animates its inner span across its own travel distance", () => {
    stubWidths({ content: 320, slot: 200 });
    const { container } = render(<TickerText>too long</TickerText>);
    const ticker = container.firstElementChild as HTMLElement;

    expect(ticker).toHaveClass("ticker--overflow");
    expect(root()).toHaveAttribute("data-ticker-active");
    expect(animate).toHaveBeenCalledTimes(1);
    expect(animate.mock.contexts[0]).toBe(ticker.firstElementChild);
    // 120px of overflow plus the 20px edge mask the slide has to clear.
    const keyframes = animate.mock.calls[0]?.[0];
    expect(keyframes?.[2]?.transform).toBe("translateX(-140px)");
  });

  it("measures the full overflow while the text is already translated", () => {
    // Parked at its tail, the slide has pulled scrollWidth down to the slot.
    // Measuring that would report the title as fitting and stop the clock.
    stubWidths({ content: 320, slot: 200, scroll: 200 });
    const { container } = render(<TickerText>too long</TickerText>);
    const ticker = container.firstElementChild as HTMLElement;

    expect(ticker).toHaveClass("ticker--overflow");
    expect(root()).toHaveAttribute("data-ticker-active");
  });

  it("forwards style and data attributes so callers keep their own presentation", () => {
    stubWidths({ content: 120, slot: 200 });
    const { container } = render(
      <TickerText data-task-row-title style={{ color: "rgb(1, 2, 3)" }}>
        titled
      </TickerText>,
    );
    const ticker = container.firstElementChild as HTMLElement;

    expect(ticker).toHaveAttribute("data-task-row-title");
    expect(ticker.style.color).toBe("rgb(1, 2, 3)");
  });

  it("cancels its animation and releases the clock when it unmounts", () => {
    stubWidths({ content: 320, slot: 200 });
    const { unmount } = render(<TickerText>too long</TickerText>);
    expect(root()).toHaveAttribute("data-ticker-active");

    unmount();
    expect(cancel).toHaveBeenCalled();
    expect(root()).not.toHaveAttribute("data-ticker-active");
  });
});
