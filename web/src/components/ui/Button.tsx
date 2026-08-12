import {
  cloneElement,
  forwardRef,
  isValidElement,
  type ButtonHTMLAttributes,
  type MouseEvent,
  type MouseEventHandler,
  type ReactElement,
} from "react";
import { Slot } from "@radix-ui/react-slot";
import { type VariantProps } from "class-variance-authority";
import { cn } from "../../lib/utils";
import { buttonVariants } from "./buttonVariants";

const BUTTON_SPINNER_CLASS_NAME =
  "mr-2 size-3 animate-spin rounded-full border border-current border-t-transparent motion-reduce:animate-none";

export interface ButtonProps
  extends
    ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  /**
   * Render through Radix Slot so caller-supplied elements receive button
   * styling and state props. Loading disables the slotted control and sets
   * aria-busy, but does not inject a spinner because Slot requires one child.
   */
  asChild?: boolean;
  loading?: boolean;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  (
    {
      className,
      variant,
      size,
      dense,
      asChild = false,
      loading = false,
      disabled,
      children,
      onClick,
      tabIndex,
      ...props
    },
    ref,
  ) => {
    const Comp = asChild ? Slot : "button";
    const isDisabled = Boolean(disabled || loading);
    if (import.meta.env.DEV && asChild && loading) {
      console.warn(
        "Button asChild loading state cannot inject a spinner; render loading UI in the child.",
      );
    }
    const handleClick = (event: MouseEvent<HTMLButtonElement>) => {
      if (asChild && isDisabled) {
        event.preventDefault();
        event.stopPropagation();
        return;
      }
      onClick?.(event);
    };
    const renderedChildren =
      asChild && isDisabled && isValidElement(children)
        ? cloneElement(
            children as ReactElement<{
              onClick?: MouseEventHandler<HTMLElement>;
              onClickCapture?: MouseEventHandler<HTMLElement>;
            }>,
            { onClick: undefined, onClickCapture: undefined },
          )
        : children;
    return (
      <Comp
        {...props}
        className={cn(buttonVariants({ variant, size, dense, className }))}
        ref={ref}
        disabled={asChild ? undefined : isDisabled}
        aria-disabled={asChild && isDisabled ? true : props["aria-disabled"]}
        aria-busy={loading || props["aria-busy"] || undefined}
        tabIndex={asChild && isDisabled ? -1 : tabIndex}
        onClick={handleClick}
      >
        {asChild ? (
          // Radix Slot enforces a single element child, so the spinner is
          // never injected as a sibling here. State still propagates via the
          // disabled / aria-busy props above.
          renderedChildren
        ) : (
          <>
            {loading && (
              <span aria-hidden="true" className={BUTTON_SPINNER_CLASS_NAME} />
            )}
            {children}
          </>
        )}
      </Comp>
    );
  },
);
Button.displayName = "Button";
