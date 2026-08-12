import { forwardRef, type HTMLAttributes } from "react";
import { Slot } from "@radix-ui/react-slot";
import { type VariantProps } from "class-variance-authority";
import { cn } from "../../lib/utils";
import { chipVariants } from "./chipVariants";

export interface ChipProps
  extends HTMLAttributes<HTMLSpanElement>, VariantProps<typeof chipVariants> {
  /**
   * Render through Radix Slot so caller-supplied elements (links, buttons)
   * receive the chip styling instead of a wrapping span.
   */
  asChild?: boolean;
}

export const Chip = forwardRef<HTMLSpanElement, ChipProps>(
  ({ className, tone, uppercase, asChild = false, ...props }, ref) => {
    const Comp = asChild ? Slot : "span";
    return (
      <Comp
        {...props}
        ref={ref}
        className={cn(chipVariants({ tone, uppercase, className }))}
      />
    );
  },
);
Chip.displayName = "Chip";
