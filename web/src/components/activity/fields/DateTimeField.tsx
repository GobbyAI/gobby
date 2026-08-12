import { useResolvedTheme } from "../../../hooks/useResolvedTheme";
import { FormField } from "../../ui/FormField";
import { Input } from "../../ui/Input";
import {
  localInputValueToUtcIso,
  utcIsoToLocalInputValue,
} from "./dateTimeConversion";
import type { DraftFieldBaseProps } from "./types";

interface DateTimeFieldProps extends DraftFieldBaseProps {
  value: string;
  onChange: (value: string) => void;
}

export function DateTimeField({
  label,
  value,
  onChange,
  disabled,
  placeholder,
  ariaLabel,
}: DateTimeFieldProps) {
  const resolvedTheme = useResolvedTheme();

  return (
    <FormField label={label}>
      {({ id }) => (
        <Input
          id={id}
          type="datetime-local"
          aria-label={ariaLabel}
          value={utcIsoToLocalInputValue(value)}
          disabled={disabled}
          placeholder={placeholder}
          step={60}
          style={{ colorScheme: resolvedTheme }}
          onChange={(event) =>
            onChange(localInputValueToUtcIso(event.target.value, value))
          }
        />
      )}
    </FormField>
  );
}
