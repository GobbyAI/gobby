import { forwardRef, type ForwardedRef, type HTMLAttributes } from "react";
import { Slot } from "@radix-ui/react-slot";
import { type VariantProps } from "class-variance-authority";
import { cn } from "../../lib/utils";
import { cardVariants } from "./cardVariants";

export interface CardProps
  extends HTMLAttributes<HTMLElement>, VariantProps<typeof cardVariants> {
  /**
   * Render through Radix Slot so caller-supplied elements (forms, links,
   * iframes) receive the card shell instead of a wrapping element. Interactive
   * cards rendered asChild must supply their own semantic focusable host.
   */
  asChild?: boolean;
}

// Interactive cards render a real <button> so the whole shell is one semantic
// focusable host; content inside must stay non-interactive (no nested
// controls). The public ref is HTMLElement because the host element varies;
// each branch narrows it to its concrete host.
export const Card = forwardRef<HTMLElement, CardProps>(
  ({ className, padding, interactive, asChild = false, ...props }, ref) => {
    const classes = cn(cardVariants({ padding, interactive, className }));
    if (asChild) {
      return <Slot {...props} ref={ref} className={classes} />;
    }
    if (interactive) {
      return (
        <button
          type="button"
          {...props}
          ref={ref as ForwardedRef<HTMLButtonElement>}
          className={classes}
        />
      );
    }
    return (
      <div
        {...props}
        ref={ref as ForwardedRef<HTMLDivElement>}
        className={classes}
      />
    );
  },
);
Card.displayName = "Card";
