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
  return (
    <input
      type="search"
      className="activity-panel-search"
      value={value}
      onChange={(event: ChangeEvent<HTMLInputElement>) =>
        onChange(event.target.value)
      }
      placeholder={placeholder}
      aria-label={ariaLabel ?? placeholder}
    />
  );
}

export const ActivityPanelSearch = memo(ActivityPanelSearchImpl);
