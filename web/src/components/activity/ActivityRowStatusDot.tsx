import { memo } from "react";

interface ActivityRowStatusDotProps {
  color: string;
  pulse?: boolean;
  label?: string;
  title?: string;
}

function ActivityRowStatusDotImpl({
  color,
  pulse,
  label,
  title,
}: ActivityRowStatusDotProps) {
  return (
    <span
      className={
        pulse
          ? "activity-row-status-dot activity-row-status-dot--pulse"
          : "activity-row-status-dot"
      }
      style={{ backgroundColor: color }}
      aria-label={label}
      role={label ? "img" : undefined}
      title={title}
    />
  );
}

export const ActivityRowStatusDot = memo(ActivityRowStatusDotImpl);
