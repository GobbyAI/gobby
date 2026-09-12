import {
  type CSSProperties,
  type ReactNode,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { cn } from "../../lib/utils";

interface TickerTextProps {
  children: ReactNode;
  className?: string;
}

// The trailing-edge mask in base.css is this wide; the slide overshoots by
// it so the end of the text clears the fade at the far stop.
const TICKER_MASK_PX = 20;

/**
 * Single-line text that slides back and forth to reveal its tail when it
 * overflows its slot, instead of truncating — for row titles whose static
 * neighbours (status, ref, chips) must stay put. Overflow is measured on
 * mount and on every resize; the reduced-motion kill switch in base.css
 * stops the slide.
 */
export function TickerText({ children, className }: TickerTextProps) {
  const ref = useRef<HTMLSpanElement>(null);
  const [overflow, setOverflow] = useState(0);

  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return;
    const measure = () =>
      setOverflow(Math.max(0, element.scrollWidth - element.clientWidth));
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [children]);

  const style = {
    "--ticker-distance": `${-(overflow + TICKER_MASK_PX)}px`,
    // ~24px/s reading pace with a floor so short overflows don't twitch.
    "--ticker-duration": `${Math.max(6, (overflow + TICKER_MASK_PX) / 24)}s`,
  } as CSSProperties;

  return (
    <span
      ref={ref}
      className={cn("ticker", overflow > 0 && "ticker--overflow", className)}
      style={style}
    >
      <span className="ticker__inner">{children}</span>
    </span>
  );
}
