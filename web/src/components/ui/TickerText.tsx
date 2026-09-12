import {
  type ComponentPropsWithoutRef,
  type CSSProperties,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import {
  releaseTickerOverflow,
  reportTickerOverflow,
} from "../../lib/tickerClock";
import { cn } from "../../lib/utils";

type TickerTextProps = ComponentPropsWithoutRef<"span">;

// The trailing-edge mask in base.css is this wide; the slide overshoots by
// it so the end of the text clears the fade at the far stop.
const TICKER_MASK_PX = 20;

/**
 * Single-line text that slides to reveal its tail when it overflows its slot,
 * instead of truncating — for row titles whose static neighbours (status, ref,
 * chips) must stay put. Overflow is measured on mount and on every resize and
 * published to the panel-wide clock in lib/tickerClock.ts, so every title
 * moves together at one pace and the loop waits for the longest. Text that
 * fits never animates; the setting and `prefers-reduced-motion` both fall back
 * to a plain ellipsis.
 */
export function TickerText({
  children,
  className,
  style,
  ...rest
}: TickerTextProps) {
  const ref = useRef<HTMLSpanElement>(null);
  const [overflow, setOverflow] = useState(0);

  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return;
    const measure = () => {
      const next = Math.max(0, element.scrollWidth - element.clientWidth);
      setOverflow(next);
      reportTickerOverflow(element, next > 0 ? next + TICKER_MASK_PX : 0);
    };
    measure();
    if (typeof ResizeObserver === "undefined") {
      return () => releaseTickerOverflow(element);
    }
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => {
      observer.disconnect();
      releaseTickerOverflow(element);
    };
  }, [children]);

  return (
    <span
      {...rest}
      ref={ref}
      className={cn(
        "ticker block min-w-0 overflow-hidden text-ellipsis whitespace-nowrap",
        overflow > 0 && "ticker--overflow",
        className,
      )}
      style={
        {
          ...style,
          "--ticker-overflow": `${overflow + TICKER_MASK_PX}px`,
        } as CSSProperties
      }
    >
      <span className="ticker__inner inline">{children}</span>
    </span>
  );
}
