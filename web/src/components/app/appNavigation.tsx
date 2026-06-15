import type { ReactNode } from "react";
import { ChatIcon } from "../icons";

export interface AppNavItem {
  id: string;
  label: string;
  icon: ReactNode;
  separator?: boolean;
}

export const APP_NAV_PAGES: Array<{ id: string; label: string }> = [];

export const APP_VALID_TABS = new Set([
  "dashboard",
  "chat",
]);

export function createAppNavItems(): AppNavItem[] {
  return [
    { id: "chat", label: "Chat", icon: <ChatIcon /> },
  ];
}
