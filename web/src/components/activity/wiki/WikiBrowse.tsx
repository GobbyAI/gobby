/**
 * Browse composition for the wiki and code modes (plan wiki-obsidian-panel
 * §3.1/§3.2): pages fetch + node index, lazy graph fetch, wide/narrow layout
 * with persisted tree sizing, quick-open, the tree/reader/backlinks wiring,
 * and the edit/create editor takeover of the reader pane.
 */

import { useCallback, useEffect, useMemo, useState, type KeyboardEvent } from "react";

import { useConfirmDialog } from "../../../hooks/useConfirmDialog";
import { ResizeHandle } from "../../shared/ResizeHandle";
import { ActivityPanelEmpty } from "../ActivityPanelEmpty";
import { DEFAULT_TOP_PANEL_PERCENT } from "../constants";
import { WikiBacklinks } from "./WikiBacklinks";
import { WikiCodewikiStatus } from "./WikiCodewikiStatus";
import { WikiPageEditor, type WikiEditorIntent } from "./WikiPageEditor";
import { WikiPageTree } from "./WikiPageTree";
import { WikiPageReader } from "./WikiPageReader";
import { WikiQuickOpen } from "./WikiQuickOpen";
import {
  fetchGraph,
  fetchPages,
  type WikiFetchScope,
  type WikiPagesResult,
} from "./WikiTabData";
import { buildNodeIndex, type WikiGraphPayload } from "./WikiTabModel";
import type { WikiTabActions } from "./WikiTabActions";
import {
  WIKI_TAB_KEYS,
  loadLastPage,
  modeForPath,
  readStoredValue,
  writeStoredValue,
  type WikiBrowseMode,
  type WikiNav,
} from "./WikiTabState";

const TREE_WIDTH_MIN = 200;
const TREE_WIDTH_MAX = 480;
const TREE_WIDTH_DEFAULT = 260;

function loadTreeWidth(): number {
  const stored = Number(readStoredValue(WIKI_TAB_KEYS.treeWidth));
  if (!Number.isFinite(stored) || stored <= 0) return TREE_WIDTH_DEFAULT;
  return Math.min(Math.max(stored, TREE_WIDTH_MIN), TREE_WIDTH_MAX);
}

function loadSplit(): number {
  const stored = Number(readStoredValue(WIKI_TAB_KEYS.split));
  if (!Number.isFinite(stored) || stored <= 0 || stored >= 100) {
    return DEFAULT_TOP_PANEL_PERCENT;
  }
  return stored;
}

type PagesState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; result: WikiPagesResult };

interface WikiBrowseProps {
  mode: WikiBrowseMode;
  scope: WikiFetchScope;
  nav: WikiNav;
  /** Toolbar search text — filters the tree to a flat match list. */
  search: string;
  wide: boolean;
  actions: WikiTabActions;
  /** Bumped by the shell after writes — refetches pages (and graph if loaded). */
  refreshSeq?: number;
  onOpenGraph: () => void;
}

export function WikiBrowse({
  mode,
  scope,
  nav,
  search,
  wide,
  actions,
  refreshSeq = 0,
  onOpenGraph,
}: WikiBrowseProps) {
  const requestKey = `${scope.projectId ?? ""}:${scope.topic ?? ""}`;
  const [pagesResult, setPagesResult] = useState<{
    key: string;
    state: Exclude<PagesState, { status: "loading" }>;
  } | null>(null);
  const [retrySeq, setRetrySeq] = useState(0);
  const [graph, setGraph] = useState<{ key: string; payload: WikiGraphPayload } | null>(null);
  const [graphWanted, setGraphWanted] = useState(false);
  const [quickOpenVisible, setQuickOpenVisible] = useState(false);
  // The editor intent captures the selection it was opened over; any guarded
  // navigation that changes the selection dismisses it by derivation.
  const [editorIntent, setEditorIntent] = useState<{
    intent: WikiEditorIntent;
    at: string | null;
  } | null>(null);
  const [treeWidth, setTreeWidth] = useState(loadTreeWidth);
  const [split, setSplit] = useState(loadSplit);
  const { confirm, ConfirmDialogElement } = useConfirmDialog();

  useEffect(() => {
    let cancelled = false;
    fetchPages(scope)
      .then((result) => {
        if (!cancelled) setPagesResult({ key: requestKey, state: { status: "ready", result } });
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          const message = error instanceof Error ? error.message : "Failed to list pages";
          setPagesResult({ key: requestKey, state: { status: "error", message } });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [refreshSeq, requestKey, retrySeq, scope]);

  // The graph payload is heavy — fetch it once, only after something asks
  // for it (backlinks expand or the unresolved-mentions view). The key folds
  // in refreshSeq so an already-loaded graph refetches after writes.
  const graphKey = `${requestKey}#${refreshSeq}`;
  useEffect(() => {
    if (!graphWanted || graph?.key === graphKey) return;
    let cancelled = false;
    fetchGraph(scope)
      .then((payload) => {
        if (!cancelled) setGraph({ key: graphKey, payload });
      })
      .catch(() => {
        // Backlinks degrade to linked mentions alone without the graph.
      });
    return () => {
      cancelled = true;
    };
  }, [graph, graphKey, graphWanted, scope]);

  const pagesState: PagesState =
    pagesResult && pagesResult.key === requestKey ? pagesResult.state : { status: "loading" };
  // Memoized from the stored result (stable identity), not the per-render
  // pagesState object, so downstream memo deps only change on new data.
  const { pages, outputs } = useMemo<WikiPagesResult>(() => {
    if (pagesResult && pagesResult.key === requestKey && pagesResult.state.status === "ready") {
      return pagesResult.state.result;
    }
    return { pages: [], outputs: [] };
  }, [pagesResult, requestKey]);

  const nodeIndex = useMemo(() => buildNodeIndex(pages), [pages]);

  const rootFilter = useCallback(
    (path: string) =>
      mode === "code" ? path.startsWith("code/") : !path.startsWith("code/"),
    [mode],
  );

  const selectedPath =
    nav.current && modeForPath(nav.current.path) === mode
      ? nav.current.path
      : loadLastPage(mode);

  const openPage = useCallback(
    (path: string) => {
      void nav.openPage(path);
    },
    [nav],
  );

  const handleDelete = useCallback(
    (path: string) => {
      void (async () => {
        const confirmed = await confirm({
          title: "Delete page",
          description: `Delete “${path}”? The page file is removed from the vault.`,
          confirmLabel: "Delete",
          destructive: true,
        });
        if (confirmed) await actions.deletePageAndNavigateBack(path);
      })();
    },
    [actions, confirm],
  );

  const handleResizeTree = useCallback((value: number) => {
    setTreeWidth(value);
    writeStoredValue(WIKI_TAB_KEYS.treeWidth, String(value));
  }, []);

  const handleResizeSplit = useCallback((value: number) => {
    setSplit(value);
    writeStoredValue(WIKI_TAB_KEYS.split, String(value));
  }, []);

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      // Claim the shortcut before the window-level app palette sees it.
      event.stopPropagation();
      setQuickOpenVisible(true);
    }
  };

  const openCreate = useCallback(
    (seed: string) => {
      setEditorIntent({ intent: { kind: "create", seed }, at: selectedPath });
    },
    [selectedPath],
  );

  const tree = (
    <div className="flex h-full min-h-0 flex-col">
      {mode === "code" ? <WikiCodewikiStatus /> : null}
      <div className="min-h-0 flex-1 overflow-hidden">
        <WikiPageTree
          pages={pages}
          outputs={outputs}
          rootFilter={rootFilter}
          promoteRoot={mode === "code" ? "code" : undefined}
          selectedPath={selectedPath}
          search={search}
          error={pagesState.status === "error" ? pagesState.message : null}
          onRetry={() => setRetrySeq((seq) => seq + 1)}
          onOpen={openPage}
          onCreateAt={openCreate}
          onDelete={handleDelete}
        />
      </div>
    </div>
  );

  const liveEditorIntent =
    editorIntent && editorIntent.at === selectedPath ? editorIntent.intent : null;

  const reader = liveEditorIntent ? (
    <WikiPageEditor
      key={
        liveEditorIntent.kind === "edit"
          ? `edit:${liveEditorIntent.path}`
          : `create:${liveEditorIntent.seed}`
      }
      scope={scope}
      intent={liveEditorIntent}
      actions={actions}
      onClose={() => setEditorIntent(null)}
      onSaved={(path) => {
        setEditorIntent(null);
        if (path !== selectedPath) void nav.openPage(path);
      }}
    />
  ) : selectedPath ? (
    <WikiPageReader
      scope={scope}
      path={selectedPath}
      nav={nav}
      nodeIndex={nodeIndex}
      graph={graph?.key === graphKey ? graph.payload : null}
      onOpenGraph={onOpenGraph}
      onToggleEdit={() =>
        setEditorIntent({ intent: { kind: "edit", path: selectedPath }, at: selectedPath })
      }
      onCreate={openCreate}
      onDelete={handleDelete}
      footer={
        <WikiBacklinks
          scope={scope}
          path={selectedPath}
          nodeIndex={nodeIndex}
          graph={graph?.key === graphKey ? graph.payload : null}
          onExpand={() => setGraphWanted(true)}
          onOpen={openPage}
        />
      }
    />
  ) : (
    <ActivityPanelEmpty
      heading={mode === "code" ? "Code wiki" : "Wiki"}
      body="Select a page from the tree, or press ⌘K to jump to one."
    />
  );

  return (
    // This container scopes shortcuts while focus remains in the wiki surface.
    <div
      className="relative flex min-h-0 flex-1 flex-col"
      onKeyDown={handleKeyDown}
    >
      {wide ? (
        <div className="flex min-h-0 flex-1">
          <div style={{ width: treeWidth }} className="min-h-0 shrink-0 overflow-hidden border-r border-border">
            {tree}
          </div>
          <ResizeHandle
            direction="horizontal"
            horizontalAnchor="left"
            onResize={handleResizeTree}
            panelWidth={treeWidth}
            minWidth={TREE_WIDTH_MIN}
            maxWidth={TREE_WIDTH_MAX}
          />
          <div className="min-h-0 min-w-0 flex-1">{reader}</div>
        </div>
      ) : (
        <div className="flex min-h-0 flex-1 flex-col">
          <div style={{ height: `${split}%` }} className="min-h-0 overflow-hidden border-b border-border">
            {tree}
          </div>
          <ResizeHandle
            direction="vertical"
            onResize={handleResizeSplit}
            panelHeight={split}
            minHeight={15}
            maxHeight={80}
          />
          <div className="min-h-0 min-w-0 flex-1">{reader}</div>
        </div>
      )}

      {quickOpenVisible ? (
        <WikiQuickOpen
          scope={scope}
          pages={pages.filter((page) => rootFilter(page.path))}
          nodeIndex={nodeIndex}
          onOpen={openPage}
          onClose={() => setQuickOpenVisible(false)}
        />
      ) : null}

      {ConfirmDialogElement}
    </div>
  );
}
