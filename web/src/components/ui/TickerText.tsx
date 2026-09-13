import {
  type ComponentPropsWithoutRef,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import {
  releaseTickerOverflow,
  reportTickerOverflow,
  TICKER_MASK_PX,
} from "../../lib/tickerClock";
import { cn } from "../../lib/utils";

type TickerTextProps = ComponentPropsWithoutRef<"span">;

/**
 * Single-line text that slides to reveal its tail when it overflows its slot,
 * instead of truncating — for row titles whose static neighbours (status, ref,
 * chips) must stay put. Overflow is measured on mount and on every resize and
 * published to the panel-wide clock in lib/tickerClock.ts, which animates the
 * inner span so every title moves together at one pace and the loop waits for
 * the longest. Text that fits never animates; the setting and
 * `prefers-reduced-motion` both fall back to a plain ellipsis.
 */
export function TickerText({ children, className, ...rest }: TickerTextProps) {
  const ref = useRef<HTMLSpanElement>(null);
  const innerRef = useRef<HTMLSpanElement>(null);
  const [overflow, setOverflow] = useState(0);

  useLayoutEffect(() => {
    const element = ref.current;
    const inner = innerRef.current;
    if (!element || !inner) return;
    const measure = () => {
      // Layout width ignores the slide's transform; scrollWidth does not. It
      // shrinks as the text travels, so a title parked at its tail would
      // measure as fitting and knock the shared clock over mid-loop.
      const next = Math.max(0, inner.offsetWidth - element.clientWidth);
      setOverflow(next);
      reportTickerOverflow(inner, next > 0 ? next + TICKER_MASK_PX : 0);
    };
    measure();
    if (typeof ResizeObserver === "undefined") {
      return () => releaseTickerOverflow(inner);
    }
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => {
      observer.disconnect();
      releaseTickerOverflow(inner);
    };
  }, [children]);

  return (
    <span
      {...rest}
      dir={rest.dir ?? "auto"}
      ref={ref}
      className={cn(
        "ticker block min-w-0 overflow-hidden text-ellipsis whitespace-nowrap",
        overflow > 0 && "ticker--overflow",
        className,
      )}
    >
      <span
        ref={innerRef}
        className="ticker__inner inline [unicode-bidi:isolate]"
      >
        {children}
      </span>
    </span>
  );
}
