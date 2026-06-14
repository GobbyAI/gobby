import type { ReactNode } from "react";
import {
  ChatIcon,
  ConfigurationIcon,
} from "../icons";

export interface AppNavItem {
  id: string;
  label: string;
  icon: ReactNode;
  separator?: boolean;
}

export const APP_NAV_PAGES: Array<{ id: string; label: string }> = [
  { id: "configuration", label: "Configuration" },
];

export const APP_VALID_TABS = new Set([
  "dashboard",
  "chat",
  "configuration",
]);

export function createAppNavItems(): AppNavItem[] {
  return [
    { id: "chat", label: "Chat", icon: <ChatIcon /> },
    {
      id: "configuration",
      label: "Configuration",
      icon: <ConfigurationIcon />,
      separator: true,
    },
  ];
}
