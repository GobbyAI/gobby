import type { ReactNode } from "react";

export type ActivityTab =
  | "sessions"
  | "pipelines"
  | "cron"
  | "traces"
  | "mcp"
  | "agents"
  | "stages"
  | "skills"
  | "memory"
  | "integrations"
  | "wiki"
  | "rules"
  | "tasks"
  | "files"
  | "plans"
  | "changes";

const iconProps = {
  width: 16,
  height: 16,
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 2,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
};

export interface ActivityPanelTab {
  id: ActivityTab;
  label: string;
  icon: ReactNode;
}

export const ACTIVITY_PANEL_TABS: ActivityPanelTab[] = [
  {
    id: "sessions",
    label: "Sessions",
    icon: (
      <svg {...iconProps}>
        <rect x="2" y="3" width="20" height="14" rx="2" ry="2" />
        <line x1="8" y1="21" x2="16" y2="21" />
        <line x1="12" y1="17" x2="12" y2="21" />
      </svg>
    ),
  },
  {
    id: "tasks",
    label: "Tasks",
    icon: (
      <svg {...iconProps}>
        <path d="M9 11l3 3L22 4" />
        <path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11" />
      </svg>
    ),
  },
  {
    id: "mcp",
    label: "MCP",
    icon: (
      <svg {...iconProps}>
        <rect x="4" y="4" width="6" height="6" rx="1" />
        <rect x="14" y="4" width="6" height="6" rx="1" />
        <rect x="9" y="14" width="6" height="6" rx="1" />
        <path d="M10 7h4" />
        <path d="M12 10v4" />
      </svg>
    ),
  },
  {
    id: "agents",
    label: "Agents",
    icon: (
      <svg {...iconProps}>
        <circle cx="12" cy="8" r="4" />
        <path d="M4 21a8 8 0 0 1 16 0" />
        <path d="M18 8h3" />
        <path d="M3 8h3" />
      </svg>
    ),
  },
  {
    id: "stages",
    label: "Stages",
    icon: (
      <svg {...iconProps}>
        <path d="M12 3 3 8l9 5 9-5-9-5z" />
        <path d="M3 13l9 5 9-5" />
        <path d="M3 18l9 5 9-5" />
      </svg>
    ),
  },
  {
    id: "skills",
    label: "Skills",
    icon: (
      <svg {...iconProps}>
        <path d="M12 3l7 4v10l-7 4-7-4V7l7-4z" />
        <path d="M12 8v8" />
        <path d="M8.5 10l3.5 2 3.5-2" />
      </svg>
    ),
  },
  {
    id: "memory",
    label: "Memory",
    icon: (
      <svg {...iconProps}>
        <path d="M12 3a7 7 0 0 0-7 7v2a4 4 0 0 0 4 4h1v3h4v-3h1a4 4 0 0 0 4-4v-2a7 7 0 0 0-7-7z" />
        <path d="M9 10h.01" />
        <path d="M15 10h.01" />
        <path d="M10 14h4" />
      </svg>
    ),
  },
  {
    id: "integrations",
    label: "Integrations",
    icon: (
      <svg {...iconProps}>
        <path d="M7 7h10" />
        <path d="M7 17h10" />
        <circle cx="5" cy="7" r="3" />
        <circle cx="19" cy="17" r="3" />
        <path d="M12 7v10" />
      </svg>
    ),
  },
  {
    id: "wiki",
    label: "Wiki",
    icon: (
      <svg {...iconProps}>
        <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" />
        <path d="M4 4.5A2.5 2.5 0 0 1 6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5z" />
        <path d="M8 7h8" />
        <path d="M8 11h6" />
      </svg>
    ),
  },
  {
    id: "rules",
    label: "Rules",
    icon: (
      <svg {...iconProps}>
        <path d="M9 3h6l4 4v14H5V3h4" />
        <path d="M14 3v5h5" />
        <path d="M8 12h8" />
        <path d="M8 16h5" />
        <path d="M8 8h2" />
      </svg>
    ),
  },
  {
    id: "plans",
    label: "Plans",
    icon: (
      <svg {...iconProps}>
        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
        <polyline points="14 2 14 8 20 8" />
        <line x1="16" y1="13" x2="8" y2="13" />
        <line x1="16" y1="17" x2="8" y2="17" />
        <polyline points="10 9 9 9 8 9" />
      </svg>
    ),
  },
  {
    id: "changes",
    label: "Changes",
    icon: (
      <svg {...iconProps}>
        <path d="M12 20h9" />
        <path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z" />
      </svg>
    ),
  },
  {
    id: "files",
    label: "Files",
    icon: (
      <svg {...iconProps}>
        <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
      </svg>
    ),
  },
  {
    id: "pipelines",
    label: "Pipelines",
    icon: (
      <svg {...iconProps}>
        <line x1="6" y1="3" x2="6" y2="15" />
        <circle cx="18" cy="6" r="3" />
        <circle cx="6" cy="18" r="3" />
        <path d="M18 9a9 9 0 0 1-9 9" />
      </svg>
    ),
  },
  {
    id: "cron",
    label: "Cron",
    icon: (
      <svg {...iconProps}>
        <circle cx="12" cy="12" r="10" />
        <polyline points="12 6 12 12 16 14" />
      </svg>
    ),
  },
  {
    id: "traces",
    label: "Traces",
    icon: (
      <svg {...iconProps}>
        <path d="M3 12h4l3-9 4 18 3-9h4" />
      </svg>
    ),
  },
];

export const ACTIVITY_PANEL_DROPDOWN_TABS = [...ACTIVITY_PANEL_TABS].sort((left, right) =>
  left.label.localeCompare(right.label, undefined, { sensitivity: "base" }),
);
