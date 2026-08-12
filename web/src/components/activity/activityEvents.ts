import type { ActivityTab } from "./ActivityPanelTabs";

export const SHOW_ACTIVITY_TAB_EVENT = "gobby:show-activity-tab";

export interface ShowActivityTabDetail {
  tab: ActivityTab;
  sessionId?: string;
}

export function showActivityTab(tab: ActivityTab, sessionId?: string): void {
  const detail: ShowActivityTabDetail =
    sessionId === undefined ? { tab } : { tab, sessionId };
  window.dispatchEvent(
    new CustomEvent<ShowActivityTabDetail>(SHOW_ACTIVITY_TAB_EVENT, {
      detail,
    }),
  );
}
