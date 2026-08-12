/**
 * Wiki vault tree (plan wiki-obsidian-panel §3.1): renders the pages listing
 * as a folder tree with kind-colored icons, keyboard navigation, a flat
 * search-match list, and a per-row actions kebab. Pure presentation — data
 * arrives from WikiBrowse, navigation flows back through callbacks.
 *
 * §4.2 code rootFilter tree: in code mode the `code/` mirror is scoped by
 * `rootFilter`, its own top level is promoted to the root set via
 * `promoteRoot`, folders start collapsed so the 1,000+-page `files/**`
 * mirror never renders unrequested rows, and search switches to the flat
 * (virtualized at scale) match list.
 */

import { useMemo, useState } from "react";
import { Virtuoso } from "react-virtuoso";

import { useTreeKeyboardNavigation } from "../../../hooks/useTreeKeyboardNavigation";
import { Button } from "../../ui/Button";
import { coarseHitAreaCls } from "../../ui/controlStyles";
import { ActivityPanelEmpty } from "../ActivityPanelEmpty";
import { QuickMenu, type QuickMenuItem } from "../QuickMenu";
import {
  buildPageTree,
  pageKindFromPath,
  type PageTreeNode,
  type WikiOutputMeta,
  type WikiPageKind,
  type WikiPageMeta,
} from "./WikiTabModel";

/** Kind → design token. Icon shape (folder vs page) is the primary signal;
 * color reinforces per the deutan-safe contract in .impeccable.md. */
const TREE_KIND_COLOR_VARS: Partial<Record<WikiPageKind, string>> = {
  concept: "--accent",
  topic: "--color-info",
  source: "--text-muted",
  recap: "--color-warning-foreground",
  code: "--color-info",
};

const FOLDER_COLOR_VAR = "--lang-folder";
const VIRTUOSO_MATCH_THRESHOLD = 100;

const ROW_CLASS =
  "flex h-7 w-full min-w-0 items-center gap-1.5 rounded-md px-1.5 text-left text-sm " +
  "text-foreground hover:bg-muted focus-visible:outline-none focus-visible:ring-1 " +
  "focus-visible:ring-accent pointer-coarse:min-h-11";

function KindIcon({
  kind,
  isFolder,
}: {
  kind: WikiPageKind | null;
  isFolder: boolean;
}) {
  const colorVar = isFolder
    ? FOLDER_COLOR_VAR
    : (kind && TREE_KIND_COLOR_VARS[kind]) || "--text-muted";
  return (
    <span
      data-testid="wiki-kind-icon"
      aria-hidden="true"
      className="flex shrink-0 items-center"
      style={{ color: `var(${colorVar})` }}
    >
      {isFolder ? (
        <svg
          width="14"
          height="14"
          viewBox="0 0 16 16"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.5"
        >
          <path d="M1.75 3.5h4l1.5 1.75h7v7.25a1 1 0 0 1-1 1H2.75a1 1 0 0 1-1-1V3.5Z" />
        </svg>
      ) : (
        <svg
          width="14"
          height="14"
          viewBox="0 0 16 16"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.5"
        >
          <path d="M4 1.75h5.5L12.5 5v9.25H4V1.75Z" />
          <path d="M9.5 1.75V5h3" />
        </svg>
      )}
    </span>
  );
}

interface FlatRow {
  node: PageTreeNode;
  depth: number;
  isExpandable: boolean;
  isExpanded: boolean;
}

function flattenTree(
  roots: PageTreeNode[],
  isExpanded: (node: PageTreeNode) => boolean,
): FlatRow[] {
  const rows: FlatRow[] = [];
  const visit = (nodes: PageTreeNode[], depth: number) => {
    for (const node of nodes) {
      const expandable = node.kind === "folder";
      const expanded = expandable && isExpanded(node);
      rows.push({
        node,
        depth,
        isExpandable: expandable,
        isExpanded: expanded,
      });
      if (expanded) visit(node.children, depth + 1);
    }
  };
  visit(roots, 0);
  return rows;
}

export interface WikiPageTreeProps {
  pages: WikiPageMeta[];
  outputs: WikiOutputMeta[];
  /** Wiki mode hides `code/`; code mode shows only `code/`. */
  rootFilter: (path: string) => boolean;
  /** Wrapper folder whose children become the roots (code mode: "code"). */
  promoteRoot?: string;
  selectedPath: string | null;
  /** Toolbar search — non-empty switches to the flat match list. */
  search: string;
  error: string | null;
  onRetry: () => void;
  onOpen: (path: string) => void;
  /** Optional until §3.2 wires page creation. */
  onCreateAt?: (prefix: string) => void;
  /** Optional delete — hidden for code pages regardless. */
  onDelete?: (path: string) => void;
}

interface SearchMatch {
  page: WikiPageMeta;
  kind: WikiPageKind;
}

function MatchRow({
  match,
  onOpen,
}: {
  match: SearchMatch;
  onOpen: (path: string) => void;
}) {
  return (
    <li>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        className={`${coarseHitAreaCls} ${ROW_CLASS} justify-start`}
        onClick={() => onOpen(match.page.path)}
      >
        <KindIcon kind={match.kind} isFolder={false} />
        <span className="truncate">{match.page.title}</span>
        <span className="ml-auto truncate pl-2 font-mono text-2xs text-muted-foreground">
          {match.page.path}
        </span>
      </Button>
    </li>
  );
}

export function WikiPageTree({
  pages,
  outputs,
  rootFilter,
  promoteRoot,
  selectedPath,
  search,
  error,
  onRetry,
  onOpen,
  onCreateAt,
  onDelete,
}: WikiPageTreeProps) {
  // Expansion is tracked as overrides on top of the defaults (top-level
  // folders open, everything deeper — notably knowledge/sources — closed),
  // so the sources folder's 266 entries never render until asked for.
  const [expandOverrides, setExpandOverrides] = useState<
    ReadonlyMap<string, boolean>
  >(new Map());
  const [menuPath, setMenuPath] = useState<string | null>(null);

  const roots = useMemo(
    () => buildPageTree(pages, outputs, rootFilter, promoteRoot),
    [pages, outputs, promoteRoot, rootFilter],
  );

  const rows = useMemo(() => {
    // Everything starts collapsed — knowledge/sources (266 entries) and the
    // code/files mirror never render a child row until expanded.
    const isExpanded = (node: PageTreeNode) =>
      expandOverrides.get(node.path) ?? false;
    return flattenTree(roots, isExpanded);
  }, [expandOverrides, roots]);

  const navItems = useMemo(
    () =>
      rows.map((row) => ({
        id: row.node.path,
        depth: row.depth,
        isExpandable: row.isExpandable,
        isExpanded: row.isExpanded,
      })),
    [rows],
  );

  const toggleFolder = (path: string) => {
    setExpandOverrides((current) => {
      const next = new Map(current);
      const row = rows.find((entry) => entry.node.path === path);
      next.set(path, !(row?.isExpanded ?? false));
      return next;
    });
  };

  const activateRow = (path: string) => {
    const row = rows.find((entry) => entry.node.path === path);
    if (!row) return;
    if (row.isExpandable) toggleFolder(path);
    else onOpen(path);
  };

  const { setRowRef, handleKeyDown, getTabIndex } = useTreeKeyboardNavigation({
    items: navItems,
    selectedId: selectedPath,
    onSelect: activateRow,
    onToggle: toggleFolder,
    selectionFollowsFocus: false,
  });

  const matches = useMemo<SearchMatch[]>(() => {
    const query = search.trim().toLowerCase();
    if (!query) return [];
    return pages
      .filter((page) => rootFilter(page.path))
      .filter(
        (page) =>
          page.title.toLowerCase().includes(query) ||
          page.path.toLowerCase().includes(query),
      )
      .map((page) => ({ page, kind: pageKindFromPath(page.path) }));
  }, [pages, rootFilter, search]);

  if (error) {
    return (
      <ActivityPanelEmpty
        heading="Wiki pages unavailable"
        body={error}
        footer={
          <Button
            type="button"
            variant="secondary"
            size="sm"
            className={coarseHitAreaCls}
            onClick={onRetry}
          >
            Retry
          </Button>
        }
      />
    );
  }

  if (search.trim()) {
    if (matches.length === 0) {
      return <ActivityPanelEmpty body={`No pages match “${search.trim()}”`} />;
    }
    if (matches.length > VIRTUOSO_MATCH_THRESHOLD) {
      return (
        <ul
          aria-label="Matching pages"
          className="h-full min-h-0 list-none overflow-hidden p-1"
        >
          <Virtuoso
            totalCount={matches.length}
            itemContent={(index) => {
              const match = matches[index];
              return match ? <MatchRow match={match} onOpen={onOpen} /> : null;
            }}
          />
        </ul>
      );
    }
    return (
      <ul
        aria-label="Matching pages"
        className="min-h-0 list-none overflow-y-auto p-1"
      >
        {matches.map((match) => (
          <MatchRow key={match.page.path} match={match} onOpen={onOpen} />
        ))}
      </ul>
    );
  }

  const rowMenuItems = (row: FlatRow): QuickMenuItem[] => {
    const path = row.node.path;
    const isCodePage = path.startsWith("code/");
    const items: QuickMenuItem[] = [];
    if (!row.isExpandable) {
      items.push({ label: "Open", onSelect: () => onOpen(path) });
    }
    if (onCreateAt && !isCodePage) {
      const prefix = row.isExpandable ? `${path}/` : path.replace(/[^/]+$/, "");
      items.push({
        label: "New page here",
        onSelect: () => onCreateAt(prefix),
      });
    }
    items.push({
      label: "Copy path",
      onSelect: () => void navigator.clipboard?.writeText(path),
    });
    if (
      onDelete &&
      !row.isExpandable &&
      !isCodePage &&
      row.node.kind === "page"
    ) {
      items.push({
        label: "Delete page",
        destructive: true,
        onSelect: () => onDelete(path),
      });
    }
    return items;
  };

  return (
    <div
      role="tree"
      aria-label="Wiki pages"
      className="min-h-0 overflow-y-auto p-1"
    >
      {rows.map((row) => {
        const path = row.node.path;
        const kind = row.node.kind === "folder" ? null : pageKindFromPath(path);
        const label = row.node.page?.title ?? row.node.name;
        return (
          <div key={path} className="group/row relative flex items-center">
            <div
              role="treeitem"
              aria-selected={path === selectedPath}
              {...(row.isExpandable ? { "aria-expanded": row.isExpanded } : {})}
              aria-label={label}
              tabIndex={getTabIndex(path)}
              ref={(node) => setRowRef(path, node)}
              className={`${ROW_CLASS} cursor-pointer ${
                path === selectedPath ? "bg-accent/10 text-foreground" : ""
              }`}
              style={{
                paddingLeft: `calc(0.375rem + ${row.depth} * 0.875rem)`,
              }}
              onClick={() => activateRow(path)}
              onKeyDown={(event) => handleKeyDown(path, event)}
            >
              <KindIcon kind={kind} isFolder={row.isExpandable} />
              <span className="truncate">{label}</span>
            </div>
            <span
              className={`absolute right-1 ${
                menuPath === path
                  ? ""
                  : "opacity-0 group-hover/row:opacity-100 focus-within:opacity-100"
              }`}
            >
              <QuickMenu
                items={rowMenuItems(row)}
                menuLabel={`Actions for ${row.node.name}`}
                triggerLabel={`Actions for ${row.node.name}`}
                onOpenChange={(open) => setMenuPath(open ? path : null)}
              />
            </span>
          </div>
        );
      })}
    </div>
  );
}
