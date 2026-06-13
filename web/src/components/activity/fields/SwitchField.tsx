import { Switch } from "../../ui/Switch";

interface SwitchFieldProps {
  label: string;
  value: boolean;
  onChange: (value: boolean) => void;
  disabled?: boolean;
  ariaLabel: string;
}

export function SwitchField({
  label,
  value,
  onChange,
  disabled,
  ariaLabel,
}: SwitchFieldProps) {
  return (
    <div className="flex min-h-11 items-center justify-between gap-3">
      <span className="text-xs font-medium text-muted-foreground">{label}</span>
      <Switch
        checked={value}
        disabled={disabled}
        aria-label={ariaLabel}
        onChange={onChange}
      />
    </div>
  );
}
