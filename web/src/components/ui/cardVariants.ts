import { cva, type VariantProps } from "class-variance-authority";

// Canonical card shell — the `rounded-lg border border-border bg-background`
// surface repeated across the hand-rolled shells this primitive retires.
// Hierarchy comes from spacing and background shift, minimal borders, no
// nested cards (.impeccable.md). Padding defaults to none so migrated shells
// keep byte parity; padded surfaces opt in per step.
export const cardVariants = cva(
  "rounded-lg border border-border bg-background",
  {
    variants: {
      padding: {
        none: "",
        sm: "p-3",
        md: "p-4",
      },
      // Clickable cards: the whole shell is the semantic focusable host, so it
      // carries the shared hover shift and focus ring itself. text-left undoes
      // the button host's centered default.
      interactive: {
        true: "cursor-pointer text-left transition-colors hover:bg-muted focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-background focus-visible:outline-none",
        false: "",
      },
    },
    defaultVariants: {
      padding: "none",
      interactive: false,
    },
  },
);

export type CardPadding = NonNullable<
  VariantProps<typeof cardVariants>["padding"]
>;
