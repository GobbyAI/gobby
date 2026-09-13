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

// The edge fade in base.css is this wide. Every slide overshoots by it so the
// far end of the text clears the fade when it parks.
export const TICKER_MASK_PX = 20;

// One loop holds at the head for 20%, travels for 60% and holds at the tail
// for 20% — for the longest title — then every title jumps back to its head
// and goes again; it never travels back. A shorter title covers its own
// distance at the same pace, so it reaches its tail sooner and parks there
// until the loop restarts.
const HOLD_FRACTION = 0.2;
const TRAVEL_FRACTION = 0.6;
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
 * One title's slide within the shared loop, in its reading direction. Left
 * starts flush left and travels left; Right mirrors it for right-to-left
 * readers, starting flush right and travelling right. `overflow` is the
 * title's own travel distance, fade overshoot included, and `span` the
 * longest title's, so it parks at its tail once the longest is
 * `overflow / span` of the way there.
 */
export function tickerKeyframes(
  overflow: number,
  span: number,
  reading: Exclude<TickerDirection, "off">,
): Keyframe[] {
  const [head, tail] =
    reading === "left"
      ? [0, -overflow]
      : [TICKER_MASK_PX - overflow, TICKER_MASK_PX];
  const arrive = HOLD_FRACTION + TRAVEL_FRACTION * (overflow / span);
  return [
    { offset: 0, transform: `translateX(${head}px)` },
    { offset: HOLD_FRACTION, transform: `translateX(${head}px)` },
    { offset: arrive, transform: `translateX(${tail}px)` },
    { offset: 1, transform: `translateX(${tail}px)` },
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

  for (const [inner, ticker] of tickers) {
    if (!retimeAll && inner !== changed) continue;
    stop(ticker);
    if (ticker.overflow <= 0) continue;
    // Recreated rather than retimed: pinned to the epoch, a new animation
    // lands in phase with the rest.
    ticker.animation = inner.animate(
      tickerKeyframes(ticker.overflow, span, direction),
      { duration: cycleMs, iterations: Infinity, easing: "linear" },
    );
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
