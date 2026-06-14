import type { ReactNode } from "react";
import {
  ChatIcon,
  ConfigurationIcon,
  MemoryIcon,
} from "../icons";

export interface AppNavItem {
  id: string;
  label: string;
  icon: ReactNode;
  separator?: boolean;
}

export const APP_NAV_PAGES: Array<{ id: string; label: string }> = [
  { id: "memory", label: "Memory" },
  { id: "configuration", label: "Configuration" },
];

export const APP_VALID_TABS = new Set([
  "dashboard",
  "chat",
  "memory",
  "configuration",
]);

export function createAppNavItems(): AppNavItem[] {
  return [
    { id: "chat", label: "Chat", icon: <ChatIcon /> },
    { id: "memory", label: "Memory", icon: <MemoryIcon /> },
    {
      id: "configuration",
      label: "Configuration",
      icon: <ConfigurationIcon />,
      separator: true,
    },
  ];
}
