import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useIsMobile } from "../useIsMobile";

const MOBILE_MAX_WIDTH_TOKEN = "--breakpoint-mobile-max-width";
const MOBILE_MAX_HEIGHT_TOKEN = "--breakpoint-mobile-max-height";

interface Viewport {
  width: number;
  height: number;
}

interface MatchMediaHarness {
  matchMedia: ReturnType<typeof vi.fn<(query: string) => MediaQueryList>>;
  resize: (viewport: Viewport) => void;
  mediaQueries: MediaQueryList[];
}

function queryMatches(query: string, viewport: Viewport): boolean {
  const maxWidth = query.match(/\(max-width:\s*(\d+)px\)/);
  const maxHeight = query.match(/\(max-height:\s*(\d+)px\)/);
  return (
    (maxWidth !== null && viewport.width <= Number(maxWidth[1])) ||
    (maxHeight !== null && viewport.height <= Number(maxHeight[1]))
  );
}

function installMatchMedia(initialViewport: Viewport): MatchMediaHarness {
  let viewport = initialViewport;
  const mediaQueries: Array<MediaQueryList & { emitChange: () => void }> = [];
  const matchMedia = vi.fn<(query: string) => MediaQueryList>((query) => {
    const listeners = new Set<(event: MediaQueryListEvent) => void>();
    let previousMatches = queryMatches(query, viewport);
    const mediaQuery = {
      get matches() {
        return queryMatches(query, viewport);
      },
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(
        (type: string, listener: EventListenerOrEventListenerObject) => {
          if (type === "change" && typeof listener === "function") {
            listeners.add(listener as (event: MediaQueryListEvent) => void);
          }
        },
      ),
      removeEventListener: vi.fn(
        (type: string, listener: EventListenerOrEventListenerObject) => {
          if (type === "change" && typeof listener === "function") {
            listeners.delete(listener as (event: MediaQueryListEvent) => void);
          }
        },
      ),
      dispatchEvent: vi.fn(() => true),
      emitChange() {
        const matches = queryMatches(query, viewport);
        if (matches === previousMatches) return;
        previousMatches = matches;
        const event = { matches, media: query } as MediaQueryListEvent;
        for (const listener of listeners) listener(event);
      },
    } satisfies MediaQueryList & { emitChange: () => void };
    mediaQueries.push(mediaQuery);
    return mediaQuery;
  });

  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    writable: true,
    value: matchMedia,
  });

  return {
    matchMedia,
    mediaQueries,
    resize(nextViewport) {
      viewport = nextViewport;
      for (const mediaQuery of mediaQueries) mediaQuery.emitChange();
    },
  };
}

function setTierTokens(width = "767px", height = "500px"): void {
  document.documentElement.style.setProperty(MOBILE_MAX_WIDTH_TOKEN, width);
  document.documentElement.style.setProperty(MOBILE_MAX_HEIGHT_TOKEN, height);
}

describe("useIsMobile", () => {
  beforeEach(() => {
    setTierTokens();
  });

  afterEach(() => {
    document.documentElement.style.removeProperty(MOBILE_MAX_WIDTH_TOKEN);
    document.documentElement.style.removeProperty(MOBILE_MAX_HEIGHT_TOKEN);
    vi.restoreAllMocks();
  });

  it.each([
    {
      viewport: { width: 767, height: 900 },
      expected: true,
      boundary: "767px width",
    },
    {
      viewport: { width: 768, height: 900 },
      expected: false,
      boundary: "768px width",
    },
    {
      viewport: { width: 1200, height: 500 },
      expected: true,
      boundary: "500px height",
    },
    {
      viewport: { width: 1200, height: 501 },
      expected: false,
      boundary: "501px height",
    },
  ])(
    "returns $expected at the $boundary boundary",
    ({ viewport, expected }) => {
      installMatchMedia(viewport);

      const { result } = renderHook(() => useIsMobile());

      expect(result.current).toBe(expected);
    },
  );

  it("renders a fine-pointer landscape phone in the mobile tier", () => {
    const { matchMedia } = installMatchMedia({ width: 932, height: 430 });

    const { result } = renderHook(() => useIsMobile());

    expect(result.current).toBe(true);
    expect(matchMedia).toHaveBeenCalledWith(
      "(max-width: 767px), (max-height: 500px)",
    );
    expect(matchMedia.mock.calls[0][0]).not.toContain("pointer");
  });

  it("updates when the viewport crosses a tier boundary", () => {
    const harness = installMatchMedia({ width: 768, height: 501 });
    const { result } = renderHook(() => useIsMobile());

    expect(result.current).toBe(false);

    act(() => harness.resize({ width: 767, height: 501 }));

    expect(result.current).toBe(true);
  });

  it.each([
    { width: "wide", height: "500px", invalidKind: "malformed" },
    { width: "", height: "", invalidKind: "missing" },
  ])(
    "warns and uses authored defaults for $invalidKind tokens",
    ({ width, height }) => {
      setTierTokens(width, height);
      const warn = vi
        .spyOn(console, "warn")
        .mockImplementation(() => undefined);
      const { matchMedia } = installMatchMedia({ width: 767, height: 501 });

      const { result } = renderHook(() => useIsMobile());

      expect(result.current).toBe(true);
      expect(matchMedia).toHaveBeenCalledWith(
        "(max-width: 767px), (max-height: 500px)",
      );
      expect(warn).toHaveBeenCalled();
    },
  );

  it("degrades without crashing when matchMedia is unavailable", () => {
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      writable: true,
      value: undefined,
    });

    const { result } = renderHook(() => useIsMobile());

    expect(result.current).toBe(false);
  });

  it("removes its change listener on unmount", () => {
    const { mediaQueries } = installMatchMedia({ width: 1200, height: 900 });
    const { unmount } = renderHook(() => useIsMobile());
    const mediaQuery = mediaQueries[0];
    const addEventListener = vi.mocked(mediaQuery.addEventListener);
    const removeEventListener = vi.mocked(mediaQuery.removeEventListener);
    const listener = addEventListener.mock.calls[0][1];

    unmount();

    expect(removeEventListener).toHaveBeenCalledWith("change", listener);
  });
});
