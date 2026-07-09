import { memo } from "react";

interface WikiTabProps {
  projectId?: string | null;
}

// Intentionally blank: the legacy wiki UI was removed ahead of the
// wiki-obsidian-panel epic (.gobby/plans/wiki-obsidian-panel.md), which
// replaces this stub with the four-mode shell.
export const WikiTab = memo(function WikiTab(_props: WikiTabProps) {
  return null;
});
