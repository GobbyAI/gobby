import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const defaultUA = navigator.userAgent;
const defaultTouchPoints = navigator.maxTouchPoints;
const defaultInnerWidth = window.innerWidth;
const defaultInnerHeight = window.innerHeight;

function viewportMatches(query: string): boolean {
  const maxWidth = query.match(/\(max-width:\s*(\d+)px\)/);
  const maxHeight = query.match(/\(max-height:\s*(\d+)px\)/);
  return (
    (maxWidth !== null && window.innerWidth <= Number(maxWidth[1])) ||
    (maxHeight !== null && window.innerHeight <= Number(maxHeight[1]))
  );
}

beforeEach(() => {
  vi.resetModules();
  vi.restoreAllMocks();
  Object.defineProperty(navigator, "userAgent", {
    value: defaultUA,
    configurable: true,
  });
  Object.defineProperty(navigator, "maxTouchPoints", {
    value: defaultTouchPoints,
    configurable: true,
  });
  Object.defineProperty(window, "innerWidth", {
    value: defaultInnerWidth,
    configurable: true,
  });
  Object.defineProperty(window, "innerHeight", {
    value: defaultInnerHeight,
    configurable: true,
  });
  document.documentElement.style.setProperty(
    "--breakpoint-mobile-max-width",
    "767px",
  );
  document.documentElement.style.setProperty(
    "--breakpoint-mobile-max-height",
    "500px",
  );
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: vi.fn((query: string) => ({ matches: viewportMatches(query) })),
  });
});

afterEach(() => {
  document.documentElement.style.removeProperty(
    "--breakpoint-mobile-max-width",
  );
  document.documentElement.style.removeProperty(
    "--breakpoint-mobile-max-height",
  );
});

describe("IS_MOBILE_DEVICE", () => {
  it("returns false in default jsdom (no touch, desktop UA)", async () => {
    const { IS_MOBILE_DEVICE } = await import("../platform");
    expect(IS_MOBILE_DEVICE).toBe(false);
  });

  it("returns true for touch + mobile UA", async () => {
    Object.defineProperty(navigator, "maxTouchPoints", {
      value: 5,
      configurable: true,
    });
    Object.defineProperty(navigator, "userAgent", {
      value: "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)",
      configurable: true,
    });
    const { IS_MOBILE_DEVICE } = await import("../platform");
    expect(IS_MOBILE_DEVICE).toBe(true);
  });

  it("returns true for touch + narrow viewport", async () => {
    Object.defineProperty(navigator, "maxTouchPoints", {
      value: 5,
      configurable: true,
    });
    Object.defineProperty(window, "innerWidth", {
      value: 375,
      configurable: true,
    });
    const { IS_MOBILE_DEVICE } = await import("../platform");
    expect(IS_MOBILE_DEVICE).toBe(true);
  });

  it("returns false for touch without mobile UA or narrow viewport", async () => {
    Object.defineProperty(navigator, "maxTouchPoints", {
      value: 5,
      configurable: true,
    });
    Object.defineProperty(navigator, "userAgent", {
      value: "Mozilla/5.0 (Macintosh; Intel Mac OS X)",
      configurable: true,
    });
    Object.defineProperty(window, "innerWidth", {
      value: 1920,
      configurable: true,
    });
    const { IS_MOBILE_DEVICE } = await import("../platform");
    expect(IS_MOBILE_DEVICE).toBe(false);
  });
});

describe("IS_IOS_DEVICE", () => {
  it("returns false for desktop UA", async () => {
    const { IS_IOS_DEVICE } = await import("../platform");
    expect(IS_IOS_DEVICE).toBe(false);
  });

  it("returns true for iPhone UA", async () => {
    Object.defineProperty(navigator, "userAgent", {
      value: "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)",
      configurable: true,
    });
    const { IS_IOS_DEVICE } = await import("../platform");
    expect(IS_IOS_DEVICE).toBe(true);
  });

  it("returns true for iPad UA", async () => {
    Object.defineProperty(navigator, "userAgent", {
      value: "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X)",
      configurable: true,
    });
    const { IS_IOS_DEVICE } = await import("../platform");
    expect(IS_IOS_DEVICE).toBe(true);
  });

  it("returns true for iPadOS (Macintosh UA + touch)", async () => {
    Object.defineProperty(navigator, "userAgent", {
      value: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
      configurable: true,
    });
    Object.defineProperty(navigator, "maxTouchPoints", {
      value: 5,
      configurable: true,
    });
    const { IS_IOS_DEVICE } = await import("../platform");
    expect(IS_IOS_DEVICE).toBe(true);
  });
});

describe("WEBGL_CAP", () => {
  it("reports supported in jsdom with mocked WebGL", async () => {
    // jsdom doesn't support canvas/WebGL, so we get the no-gl path
    const { WEBGL_CAP } = await import("../platform");
    // In jsdom, getContext returns null, so we expect no support
    expect(WEBGL_CAP).toEqual({
      supported: false,
      tier: "none",
      maxTextureSize: 0,
    });
  });

  it("reports correct tier for high-end GPU", async () => {
    const mockCtx = {
      getParameter: vi.fn().mockReturnValue(16384),
      getExtension: vi.fn().mockReturnValue({ loseContext: vi.fn() }),
      MAX_TEXTURE_SIZE: 0x0d33,
    };
    vi.spyOn(document, "createElement").mockReturnValue({
      getContext: vi.fn().mockReturnValue(mockCtx),
    } as unknown as HTMLCanvasElement);

    const { WEBGL_CAP } = await import("../platform");
    expect(WEBGL_CAP).toEqual({
      supported: true,
      tier: "high",
      maxTextureSize: 16384,
    });
  });

  it("reports medium tier for 8192 texture size", async () => {
    const mockCtx = {
      getParameter: vi.fn().mockReturnValue(8192),
      getExtension: vi.fn().mockReturnValue(null),
      MAX_TEXTURE_SIZE: 0x0d33,
    };
    vi.spyOn(document, "createElement").mockReturnValue({
      getContext: vi.fn().mockReturnValue(mockCtx),
    } as unknown as HTMLCanvasElement);

    const { WEBGL_CAP } = await import("../platform");
    expect(WEBGL_CAP).toEqual({
      supported: true,
      tier: "medium",
      maxTextureSize: 8192,
    });
  });

  it("reports low tier for small texture size", async () => {
    const mockCtx = {
      getParameter: vi.fn().mockReturnValue(4096),
      getExtension: vi.fn().mockReturnValue(null),
      MAX_TEXTURE_SIZE: 0x0d33,
    };
    vi.spyOn(document, "createElement").mockReturnValue({
      getContext: vi.fn().mockReturnValue(mockCtx),
    } as unknown as HTMLCanvasElement);

    const { WEBGL_CAP } = await import("../platform");
    expect(WEBGL_CAP).toEqual({
      supported: true,
      tier: "low",
      maxTextureSize: 4096,
    });
  });
});
