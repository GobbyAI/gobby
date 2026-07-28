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
    <div className="flex min-h-8 items-center gap-2">
      <span className="text-sm font-medium text-muted-foreground">{label}</span>
      <Switch
        checked={value}
        disabled={disabled}
        aria-label={ariaLabel}
        onChange={onChange}
      />
    </div>
  );
}
