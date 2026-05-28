import type { ReactNode } from "react";
import {
  ChatIcon,
  ConfigurationIcon,
  CronIcon,
  IntegrationsIcon,
  MemoryIcon,
  ProjectsIcon,
  ReportsIcon,
  SkillsIcon,
  TracesIcon,
  WorkflowsIcon,
} from "../icons";

export interface AppNavItem {
  id: string;
  label: string;
  icon: ReactNode;
  separator?: boolean;
}

export const APP_NAV_PAGES: Array<{ id: string; label: string }> = [
  { id: "workflows", label: "Workflows" },
  { id: "reports", label: "Reports" },
  { id: "cron", label: "Cron Jobs" },
  { id: "traces", label: "Traces" },
  { id: "memory", label: "Memory" },
  { id: "skills", label: "Skills" },
  { id: "configuration", label: "Configuration" },
];

export const APP_VALID_TABS = new Set([
  "dashboard",
  "chat",
  "projects",
  "workflows",
  "reports",
  "cron",
  "traces",
  "memory",
  "skills",
  "configuration",
]);

export function createAppNavItems(): AppNavItem[] {
  return [
    { id: "chat", label: "Chat", icon: <ChatIcon /> },
    {
      id: "projects",
      label: "Project",
      icon: <ProjectsIcon />,
      separator: true,
    },
    { id: "workflows", label: "Workflows", icon: <WorkflowsIcon /> },
    { id: "cron", label: "Cron Jobs", icon: <CronIcon /> },
    { id: "reports", label: "Reports", icon: <ReportsIcon /> },
    { id: "traces", label: "Traces", icon: <TracesIcon /> },
    { id: "memory", label: "Memory", icon: <MemoryIcon /> },
    { id: "skills", label: "Skills", icon: <SkillsIcon /> },
    { id: "integrations", label: "Integrations", icon: <IntegrationsIcon /> },
    {
      id: "configuration",
      label: "Configuration",
      icon: <ConfigurationIcon />,
      separator: true,
    },
  ];
}
