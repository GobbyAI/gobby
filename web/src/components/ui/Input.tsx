import { forwardRef, type InputHTMLAttributes } from "react";
import { cn } from "../../lib/utils";
import { controlSurfaceCls, controlWrapperCls } from "./controlStyles";

export interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  error?: boolean;
  /** Layout overrides for the wrapper label (flex-1, width caps, …). */
  wrapperClassName?: string;
}

export const Input = forwardRef<HTMLInputElement, InputProps>(
  ({ className, error, wrapperClassName, ...props }, ref) => {
    return (
      <label className={cn(controlWrapperCls, wrapperClassName)}>
        <input
          className={cn(
            "flex h-9 py-1",
            controlSurfaceCls,
            error ? "border-destructive" : "border-border",
            className,
          )}
          aria-invalid={!!error}
          ref={ref}
          {...props}
        />
      </label>
    );
  },
);
Input.displayName = "Input";
