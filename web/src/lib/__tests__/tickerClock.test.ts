import { readFileSync } from "node:fs";
import { join } from "node:path";

import postcss, { type Rule } from "postcss";
import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  vi,
  type Mock,
} from "vitest";

const loadClock = () => import("../tickerClock");
let clock: Awaited<ReturnType<typeof loadClock>>;

interface FakeAnimation {
  target: Element;
  keyframes: Keyframe[];
  options: KeyframeAnimationOptions;
  startTime: number | null;
  cancel: Mock;
}

let animations: FakeAnimation[] = [];

function root() {
  return document.documentElement;
}

function makeTicker(): HTMLElement {
  return document.createElement("span");
}

/** Animations still running, i.e. created and never cancelled. */
function running(): FakeAnimation[] {
  return animations.filter((animation) => !animation.cancel.mock.calls.length);
}

function runningOn(target: Element): FakeAnimation {
  const animation = running().find((candidate) => candidate.target === target);
  if (!animation) throw new Error("no running animation on the target");
  return animation;
}

const originalMatchMedia = window.matchMedia;

describe("tickerClock", () => {
  beforeEach(async () => {
    // The clock is a module singleton; a fresh copy per test keeps the
    // epoch, pace, direction and media query from leaking between cases.
    vi.resetModules();
    clock = await loadClock();
    animations = [];
    Object.defineProperty(Element.prototype, "animate", {
      configurable: true,
      writable: true,
      value(
        this: Element,
        keyframes: Keyframe[],
        options: KeyframeAnimationOptions,
      ) {
        const animation: FakeAnimation = {
          target: this,
          keyframes,
          options,
          startTime: null,
          cancel: vi.fn(),
        };
        animations.push(animation);
        return animation;
      },
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    delete (Element.prototype as Partial<Element>).animate;
    window.matchMedia = originalMatchMedia;
    root().removeAttribute("data-ticker-active");
  });

  it("parks each title at its own tail on the longest title's loop", () => {
    const expectOffsets = (overflow: number, expected: number[]) => {
      const offsets = clock
        .tickerKeyframes(overflow, 480, "left")
        .map((frame) => frame.offset);
      expect(offsets).toHaveLength(expected.length);
      expected.forEach((offset, index) => {
        expect(offsets[index]).toBeCloseTo(offset, 10);
      });
    };

    // The longest holds to 20%, travels to 80% and holds to the end, where the
    // next iteration jumps it back to its head: no leg travels back.
    expectOffsets(480, [0, 0.2, 0.8, 1]);
    // A quarter of the distance at the same pace parks at its tail from 35%.
    expectOffsets(120, [0, 0.2, 0.35, 1]);
    expect(
      clock.tickerKeyframes(120, 480, "left").map((frame) => frame.transform),
    ).toEqual([
      "translateX(0px)",
      "translateX(0px)",
      "translateX(-120px)",
      "translateX(-120px)",
    ]);
  });

  it("runs every overflowing title on one start time and one cycle", () => {
    const now = vi.spyOn(performance, "now").mockReturnValue(1000);
    const long = makeTicker();
    const short = makeTicker();
    const late = makeTicker();

    clock.reportTickerOverflow(long, 480);
    clock.reportTickerOverflow(short, 120);
    // Mounting mid-loop joins in phase rather than starting its own loop.
    now.mockReturnValue(9000);
    clock.reportTickerOverflow(late, 240);

    expect(root()).toHaveAttribute("data-ticker-active");
    expect(running()).toHaveLength(3);
    for (const animation of running()) {
      expect(animation.startTime).toBe(1000);
      expect(animation.options.duration).toBeCloseTo(
        (480 / (0.6 * 24)) * 1000,
        5,
      );
      expect(animation.options.iterations).toBe(Infinity);
    }
    expect(runningOn(short).keyframes[2]?.offset).toBeCloseTo(0.35, 10);
  });

  it("derives the cycle from the widest overflow at the selected pace", () => {
    const ticker = makeTicker();
    clock.reportTickerOverflow(ticker, 480);

    clock.setTickerSpeed("fast");
    expect(runningOn(ticker).options.duration).toBeCloseTo(
      (480 / (0.6 * clock.TICKER_SPEED_PX_PER_SEC.fast)) * 1000,
      5,
    );

    clock.setTickerSpeed("slow");
    expect(runningOn(ticker).options.duration).toBeCloseTo(
      (480 / (0.6 * clock.TICKER_SPEED_PX_PER_SEC.slow)) * 1000,
      5,
    );
  });

  it("floors the cycle so a short overflow does not twitch", () => {
    const ticker = makeTicker();
    clock.reportTickerOverflow(ticker, 8);
    expect(runningOn(ticker).options.duration).toBe(4000);
  });

  it("keeps the loop's position when a longer title stretches the cycle", () => {
    const now = vi.spyOn(performance, "now").mockReturnValue(1000);
    const first = makeTicker();
    clock.reportTickerOverflow(first, 216); // 15s loop

    now.mockReturnValue(1000 + 7_500); // halfway through it
    const longer = makeTicker();
    clock.reportTickerOverflow(longer, 432); // 30s loop

    // Still halfway: 8.5s now, minus half of the new 30s loop.
    for (const animation of running()) {
      expect(animation.startTime).toBeCloseTo(8_500 - 15_000, 6);
    }
    expect(running()).toHaveLength(2);
  });

  it("mirrors the path for right-to-left readers", () => {
    const ticker = makeTicker();
    const transforms = () =>
      runningOn(ticker).keyframes.map((frame) => frame.transform);
    clock.setTickerDirection("right");
    clock.reportTickerOverflow(ticker, 300);

    // Starts flush right and travels right until its left end clears the
    // 20px fade on that edge: mirrored in space, never played in reverse.
    expect(transforms()).toEqual([
      "translateX(-280px)",
      "translateX(-280px)",
      "translateX(20px)",
      "translateX(20px)",
    ]);
    expect(runningOn(ticker).options.direction).toBeUndefined();

    clock.setTickerDirection("left");
    expect(running()).toHaveLength(1);
    expect(transforms()).toEqual([
      "translateX(0px)",
      "translateX(0px)",
      "translateX(-300px)",
      "translateX(-300px)",
    ]);
  });

  it("stops every title when scrolling is switched off", () => {
    const ticker = makeTicker();
    clock.reportTickerOverflow(ticker, 300);
    expect(running()).toHaveLength(1);

    clock.setTickerDirection("off");
    expect(running()).toHaveLength(0);
    expect(root()).not.toHaveAttribute("data-ticker-active");

    clock.setTickerDirection("left");
    expect(running()).toHaveLength(1);
  });

  it("holds still under reduced motion and resumes when it is lifted", () => {
    const listeners: (() => void)[] = [];
    const query = {
      matches: true,
      addEventListener: (_type: string, listener: () => void) => {
        listeners.push(listener);
      },
    };
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      writable: true,
      value: () => query,
    });

    const ticker = makeTicker();
    clock.reportTickerOverflow(ticker, 300);
    expect(running()).toHaveLength(0);
    expect(root()).not.toHaveAttribute("data-ticker-active");

    query.matches = false;
    listeners.forEach((listener) => listener());
    expect(running()).toHaveLength(1);
    expect(root()).toHaveAttribute("data-ticker-active");
  });

  it("cancels a title that now fits or is released, idling with the last", () => {
    const ticker = makeTicker();
    clock.reportTickerOverflow(ticker, 200);
    expect(running()).toHaveLength(1);

    // Still registered, but its text now fits.
    clock.reportTickerOverflow(ticker, 0);
    expect(running()).toHaveLength(0);
    expect(root()).not.toHaveAttribute("data-ticker-active");

    clock.reportTickerOverflow(ticker, 200);
    clock.releaseTickerOverflow(ticker);
    expect(running()).toHaveLength(0);
    expect(root()).not.toHaveAttribute("data-ticker-active");
  });

  it("stays idle where Web Animations are unavailable", () => {
    delete (Element.prototype as Partial<Element>).animate;
    expect(() => clock.reportTickerOverflow(makeTicker(), 300)).not.toThrow();
    expect(root()).not.toHaveAttribute("data-ticker-active");
  });
});

describe("ticker stylesheet", () => {
  const sheet = postcss.parse(
    readFileSync(join(process.cwd(), "src/styles/base.css"), "utf8"),
  );

  it("keeps the ticker clock out of CSS", () => {
    // An inherited, animated custom property on the panel root restyled every
    // row on every frame and crashed iOS Safari (#22281). The clock lives in
    // lib/tickerClock.ts as per-title transform animations.
    const tickerAtRules: string[] = [];
    sheet.walkAtRules(/^(property|keyframes)$/, (rule) => {
      if (rule.params.includes("ticker")) tickerAtRules.push(rule.params);
    });
    expect(tickerAtRules).toEqual([]);

    const panelAnimations: string[] = [];
    sheet.walkDecls(/^animation/, (decl) => {
      const selector = (decl.parent as Rule).selector ?? "";
      if (selector.includes(".activity-panel")) panelAnimations.push(selector);
    });
    expect(panelAnimations).toEqual([]);
  });

  it("fades the edge each reading direction starts clipped at", () => {
    const fades = new Map<string, string>();
    sheet.walkDecls("mask-image", (decl) => {
      fades.set((decl.parent as Rule).selector, decl.value);
    });

    // Left starts flush left, clipped on the right; Right mirrors it.
    expect(
      fades.get(
        'html[data-ticker-active]:not([data-ticker="off"]) .ticker--overflow',
      ),
    ).toMatch(/^linear-gradient\(to right,/);
    expect(
      fades.get(
        'html[data-ticker-active][data-ticker="right"] .ticker--overflow',
      ),
    ).toMatch(/^linear-gradient\(to left,/);
  });
});
