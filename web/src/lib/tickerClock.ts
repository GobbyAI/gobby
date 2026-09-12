import type { TickerSpeed } from "../hooks/useSettings";

/**
 * Reading pace per preset. `normal` is the 24px/s the first ticker shipped
 * with, so the default keeps the pace that was already signed off.
 */
export const TICKER_SPEED_PX_PER_SEC: Record<TickerSpeed, number> = {
  slow: 14,
  normal: 24,
  fast: 42,
};

// `@keyframes ticker-clock` spends 40% of the cycle travelling out and 40%
// travelling back; the other 20% is the hold at each end.
const TRAVEL_FRACTION = 0.4;
// Floor so a one-word overflow doesn't twitch through its cycle.
const MIN_CYCLE_SECONDS = 4;

const overflows = new Map<Element, number>();
let speedPxPerSec = TICKER_SPEED_PX_PER_SEC.normal;
let appliedSpan = -1;

/**
 * Publish the widest overflow on the page as `--ticker-span`, and the cycle
 * that covers it at the chosen pace as `--ticker-cycle`. Every ticker inherits
 * both and clamps at its own overflow, so the longest title alone decides when
 * the shared clock loops — the rest park at their end and wait. Without
 * `data-ticker-active` the panel runs no animation at all, which is the case
 * whenever every title fits.
 */
function apply(force: boolean): void {
  let span = 0;
  for (const overflow of overflows.values()) {
    if (overflow > span) span = overflow;
  }
  if (!force && span === appliedSpan) return;
  appliedSpan = span;

  const root = document.documentElement;
  if (span <= 0) {
    root.removeAttribute("data-ticker-active");
    return;
  }
  const cycle = Math.max(
    MIN_CYCLE_SECONDS,
    span / (TRAVEL_FRACTION * speedPxPerSec),
  );
  root.style.setProperty("--ticker-span", `${span}px`);
  root.style.setProperty("--ticker-cycle", `${cycle}s`);
  root.setAttribute("data-ticker-active", "");
}

/** Register one ticker's travel distance (0 when its text fits its slot). */
export function reportTickerOverflow(element: Element, px: number): void {
  overflows.set(element, px);
  apply(false);
}

export function releaseTickerOverflow(element: Element): void {
  if (overflows.delete(element)) apply(false);
}

export function setTickerSpeed(speed: TickerSpeed): void {
  speedPxPerSec = TICKER_SPEED_PX_PER_SEC[speed];
  apply(true);
}
