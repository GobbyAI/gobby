import { cn } from "../../lib/utils";

// Invisible coarse-pointer hit-area expansion: a centered, absolutely
// positioned ::before that floors the tap target at 44×44 while covering the
// whole control, without touching the host's rendered box. Hosts that are
// real elements (Radix Select trigger/items) take this directly; form
// controls (input/textarea/select) cannot render pseudo-elements, so their
// primitives apply it to a wrapping <label>, which also forwards perimeter
// clicks to the control natively.
export const coarseHitAreaCls = cn(
  "relative",
  "pointer-coarse:before:content-['']",
  "pointer-coarse:before:absolute",
  "pointer-coarse:before:left-1/2",
  "pointer-coarse:before:top-1/2",
  "pointer-coarse:before:size-full",
  "pointer-coarse:before:min-h-11",
  "pointer-coarse:before:min-w-11",
  "pointer-coarse:before:-translate-x-1/2",
  "pointer-coarse:before:-translate-y-1/2",
);

// Wrapper label shared by Input, Textarea, and NativeSelect. Layout overrides
// (flex-1, width caps) belong here via wrapperClassName; visual overrides
// belong on the control via className.
export const controlWrapperCls = cn("inline-flex w-full", coarseHitAreaCls);

// Shared visual contract for form controls: 36px-ladder bordered box, token
// colors, brand focus ring, disabled dimming. Border color is applied by each
// primitive from its error state.
export const controlSurfaceCls = cn(
  "w-full rounded-md border bg-transparent px-3 text-sm transition-colors",
  "placeholder:text-muted-foreground",
  "focus-visible:ring-2 focus-visible:ring-accent focus-visible:outline-none",
  "focus-visible:ring-offset-2 focus-visible:ring-offset-background",
  "disabled:cursor-not-allowed disabled:opacity-50",
);
