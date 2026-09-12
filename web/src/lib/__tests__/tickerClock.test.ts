import { readFileSync } from "node:fs";
import { join } from "node:path";

import postcss, { type Rule } from "postcss";
import { afterEach, describe, expect, it } from "vitest";

import {
  releaseTickerOverflow,
  reportTickerOverflow,
  setTickerSpeed,
  TICKER_SPEED_PX_PER_SEC,
} from "../tickerClock";

function root() {
  return document.documentElement;
}

function makeTicker(): Element {
  return document.createElement("span");
}

function cycleSeconds(): number {
  return Number.parseFloat(root().style.getPropertyValue("--ticker-cycle"));
}

describe("tickerClock", () => {
  afterEach(() => {
    setTickerSpeed("normal");
    root().removeAttribute("data-ticker-active");
    root().style.removeProperty("--ticker-span");
    root().style.removeProperty("--ticker-cycle");
  });

  it("spans the widest registered overflow so the loop waits for the longest", () => {
    const short = makeTicker();
    const long = makeTicker();

    reportTickerOverflow(short, 120);
    reportTickerOverflow(long, 480);
    expect(root().style.getPropertyValue("--ticker-span")).toBe("480px");

    // The short one finishing changes nothing; the clock still runs to 480.
    reportTickerOverflow(short, 90);
    expect(root().style.getPropertyValue("--ticker-span")).toBe("480px");

    releaseTickerOverflow(long);
    expect(root().style.getPropertyValue("--ticker-span")).toBe("90px");

    releaseTickerOverflow(short);
  });

  it("derives the cycle from the span at the selected reading pace", () => {
    const ticker = makeTicker();
    // Travel is 40% of the cycle in each direction, so a 480px span at
    // 24px/s spends 20s travelling and 50s on the full loop.
    reportTickerOverflow(ticker, 480);
    expect(cycleSeconds()).toBeCloseTo(480 / (0.4 * 24), 5);

    setTickerSpeed("fast");
    expect(cycleSeconds()).toBeCloseTo(
      480 / (0.4 * TICKER_SPEED_PX_PER_SEC.fast),
      5,
    );

    setTickerSpeed("slow");
    expect(cycleSeconds()).toBeCloseTo(
      480 / (0.4 * TICKER_SPEED_PX_PER_SEC.slow),
      5,
    );

    releaseTickerOverflow(ticker);
  });

  it("floors the cycle so a short overflow does not twitch", () => {
    const ticker = makeTicker();
    reportTickerOverflow(ticker, 8);
    expect(cycleSeconds()).toBe(4);
    releaseTickerOverflow(ticker);
  });

  it("stops the clock once the last overflowing ticker is gone", () => {
    const ticker = makeTicker();
    reportTickerOverflow(ticker, 200);
    expect(root()).toHaveAttribute("data-ticker-active");

    // Still registered, but its text now fits.
    reportTickerOverflow(ticker, 0);
    expect(root()).not.toHaveAttribute("data-ticker-active");

    releaseTickerOverflow(ticker);
  });
});

describe("ticker clock stylesheet", () => {
  const sheet = postcss.parse(
    readFileSync(join(process.cwd(), "src/styles/base.css"), "utf8"),
  );

  function selectorOf(decl: { parent: unknown }): string {
    return (decl.parent as Rule).selector;
  }

  it("keeps animation-direction in the rule that starts the clock", () => {
    // The `animation` shorthand also sets animation-direction, at the
    // shorthand rule's own specificity. A longhand parked in a separate,
    // less specific `[data-ticker=...]` rule therefore never applies — the
    // Right setting would silently behave exactly like Left.
    const orphans: string[] = [];
    sheet.walkDecls("animation-direction", (decl) => {
      if (decl.important) return;
      const siblings = (decl.parent as Rule).nodes;
      const shorthand = siblings.findIndex(
        (node) => node.type === "decl" && node.prop === "animation",
      );
      if (shorthand === -1 || shorthand > siblings.indexOf(decl)) {
        orphans.push(selectorOf(decl));
      }
    });
    expect(orphans).toEqual([]);
  });

  it("drives the rightward reveal through an inherited direction token", () => {
    const declarations: [string, string][] = [];
    sheet.walkDecls("--ticker-direction", (decl) => {
      declarations.push([selectorOf(decl), decl.value]);
    });
    expect(declarations).toEqual([['html[data-ticker="right"]', "reverse"]]);
  });
});
