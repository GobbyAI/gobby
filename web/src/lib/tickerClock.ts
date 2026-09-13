import type { TickerDirection, TickerSpeed } from "../hooks/useSettings";

/**
 * Reading pace per preset. `normal` is the 24px/s the first ticker shipped
 * with, so the default keeps the pace that was already signed off.
 */
export const TICKER_SPEED_PX_PER_SEC: Record<TickerSpeed, number> = {
  slow: 14,
  normal: 24,
  fast: 42,
};

// One loop holds at the head for 10%, travels out for 40%, holds at the tail
// for 10% and travels back for 40% — for the longest title. A shorter title
// covers its own distance at the same pace, so it reaches its tail sooner and
// parks there until the longest comes back past it.
const HOLD_FRACTION = 0.1;
const TRAVEL_FRACTION = 0.4;
// Floor so a one-word overflow doesn't twitch through its cycle.
const MIN_CYCLE_SECONDS = 4;

interface Ticker {
  overflow: number;
  animation: Animation | null;
}

const tickers = new Map<HTMLElement, Ticker>();
let speedPxPerSec = TICKER_SPEED_PX_PER_SEC.normal;
let direction: TickerDirection = "left";
let reducedMotionQuery: MediaQueryList | null = null;
// Shared start time on the document timeline. Every animation is pinned to
// it, so a title that mounts mid-loop joins in phase instead of starting over.
let epoch: number | null = null;
let appliedSpan = 0;
let appliedCycleMs = 0;

/**
 * One title's slide within the shared loop. `overflow` is its own travel
 * distance and `span` the longest title's, so it reaches its tail when the
 * longest is `overflow / span` of the way out, and leaves when the longest
 * passes that point on the way back.
 */
export function tickerKeyframes(overflow: number, span: number): Keyframe[] {
  const reach = TRAVEL_FRACTION * (overflow / span);
  const home = "translateX(0px)";
  const tail = `translateX(${-overflow}px)`;
  return [
    { offset: 0, transform: home },
    { offset: HOLD_FRACTION, transform: home },
    { offset: HOLD_FRACTION + reach, transform: tail },
    { offset: 1 - reach, transform: tail },
    { offset: 1, transform: home },
  ];
}

function prefersReducedMotion(): boolean {
  if (!reducedMotionQuery) {
    reducedMotionQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
    reducedMotionQuery.addEventListener("change", () => apply());
  }
  return reducedMotionQuery.matches;
}

function stop(ticker: Ticker): void {
  ticker.animation?.cancel();
  ticker.animation = null;
}

/**
 * Run every overflowing title on one loop sized for the widest overflow at
 * the chosen pace. Each title gets a plain transform animation — compositor
 * work — never a custom property the whole panel inherits: that restyled
 * every row on every frame and crashed iOS Safari (#22281). `changed` limits
 * the work to one title when nothing shared moved; omit it to retime all.
 */
function apply(changed?: HTMLElement): void {
  let span = 0;
  for (const { overflow } of tickers.values()) {
    if (overflow > span) span = overflow;
  }

  const root = document.documentElement;
  if (
    span <= 0 ||
    direction === "off" ||
    typeof Element.prototype.animate !== "function" ||
    prefersReducedMotion()
  ) {
    tickers.forEach(stop);
    epoch = null;
    appliedSpan = 0;
    appliedCycleMs = 0;
    root.removeAttribute("data-ticker-active");
    return;
  }

  const cycleMs =
    Math.max(MIN_CYCLE_SECONDS, span / (TRAVEL_FRACTION * speedPxPerSec)) *
    1000;
  const now = performance.now();
  if (epoch === null) {
    epoch = now;
  } else if (cycleMs !== appliedCycleMs) {
    // Keep the loop where it is: a new longest title stretches the cycle
    // rather than sending every title home.
    const fraction = ((now - epoch) % appliedCycleMs) / appliedCycleMs;
    epoch = now - fraction * cycleMs;
  }
  const retimeAll =
    changed === undefined || span !== appliedSpan || cycleMs !== appliedCycleMs;
  appliedSpan = span;
  appliedCycleMs = cycleMs;

  const playback = direction === "right" ? "reverse" : "normal";
  for (const [inner, ticker] of tickers) {
    if (!retimeAll && inner !== changed) continue;
    stop(ticker);
    if (ticker.overflow <= 0) continue;
    // Recreated rather than retimed: pinned to the epoch, a new animation
    // lands in phase with the rest.
    ticker.animation = inner.animate(tickerKeyframes(ticker.overflow, span), {
      duration: cycleMs,
      iterations: Infinity,
      easing: "linear",
      direction: playback,
    });
    ticker.animation.startTime = epoch;
  }
  root.setAttribute("data-ticker-active", "");
}

/** Register one title's travel distance (0 when its text fits its slot). */
export function reportTickerOverflow(inner: HTMLElement, px: number): void {
  const ticker = tickers.get(inner);
  if (ticker?.overflow === px) return;
  if (ticker) {
    ticker.overflow = px;
  } else {
    tickers.set(inner, { overflow: px, animation: null });
  }
  apply(inner);
}

export function releaseTickerOverflow(inner: HTMLElement): void {
  const ticker = tickers.get(inner);
  if (!ticker) return;
  stop(ticker);
  tickers.delete(inner);
  apply(inner);
}

export function setTickerSpeed(speed: TickerSpeed): void {
  speedPxPerSec = TICKER_SPEED_PX_PER_SEC[speed];
  apply();
}

export function setTickerDirection(next: TickerDirection): void {
  direction = next;
  apply();
}
