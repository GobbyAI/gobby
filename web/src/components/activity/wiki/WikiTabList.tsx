import type { WikiSourceRecord } from "../../../hooks/useWiki";
import { cn } from "../../../lib/utils";
import { QuickMenu, type QuickMenuItem } from "../QuickMenu";
import {
  sourceLabel,
  sourceLinks,
  sourcePath,
} from "./WikiTabData";

interface WikiTabListProps {
  sources: WikiSourceRecord[];
  selectedId: string | null;
  busyId: string | null;
  onSelect: (source: WikiSourceRecord) => void;
  onRemove: (source: WikiSourceRecord) => void;
}

export function WikiTabList({
  sources,
  selectedId,
  busyId,
  onSelect,
  onRemove,
}: WikiTabListProps) {
  return (
    <div className="flex flex-col" role="list" aria-label="Wiki sources">
      {sources.map((source) => {
        const label = sourceLabel(source);
        const path = sourcePath(source) || source.id;
        const selected = source.id === selectedId;
        const busy = source.id === busyId;
        const links = sourceLinks(source);
        const menuItems: QuickMenuItem[] = [
          {
            label: "Remove source",
            destructive: true,
            disabled: busy,
            onSelect: () => onRemove(source),
          },
        ];

        return (
          <div
            key={source.id}
            role="listitem"
            aria-label={`${label} source`}
            className={cn(
              "activity-list-row",
              selected && "activity-list-row--selected",
            )}
          >
            <button
              type="button"
              className="activity-list-row__body"
              aria-label={`Select ${label}`}
              onClick={() => onSelect(source)}
            >
              <span className="activity-row-title">{label}</span>
              {links.length > 0 && (
                <span className="activity-chip">
                  Linked
                </span>
              )}
              <span className="activity-row-meta shrink truncate">{path}</span>
            </button>
            <div className="flex items-center px-1">
              <QuickMenu
                items={menuItems}
                menuLabel={`Actions for ${label}`}
                triggerLabel={`Open actions for ${label}`}
                disabled={busy}
              />
            </div>
          </div>
        );
      })}
    </div>
  );
}
