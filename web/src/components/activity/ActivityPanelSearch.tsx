import { memo, type ChangeEvent } from "react";

interface ActivityPanelSearchProps {
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
  ariaLabel?: string;
}

function ActivityPanelSearchImpl({
  value,
  onChange,
  placeholder,
  ariaLabel,
}: ActivityPanelSearchProps) {
  const inputLabel = ariaLabel ?? placeholder;

  return (
    <input
      type="search"
      className="activity-panel-search pointer-coarse:min-h-11"
      value={value}
      name={inputLabel.toLowerCase().replace(/\s+/g, "-")}
      onChange={(event: ChangeEvent<HTMLInputElement>) =>
        onChange(event.target.value)
      }
      placeholder={placeholder}
      aria-label={inputLabel}
    />
  );
}

export const ActivityPanelSearch = memo(ActivityPanelSearchImpl);
