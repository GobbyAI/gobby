/**
 * Four-mode wiki shell (plan wiki-obsidian-panel §2.2): Wiki | Code | Ask |
 * Research with persisted mode/scope, dirty-guarded transitions, and the
 * kebab action surface. Wiki and Code render the §3.1 browse experience,
 * Ask renders the §5.1 grounded Q&A mode, and Research renders the §5.2
 * pipeline launch/monitor mode.
 */

import {
  memo,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
} from "react";

import { useWiki } from "../../hooks/useWiki";
import { useDirtyGuard } from "./dirtyGuard";
import { WikiAskMode } from "./wiki/WikiAskMode";
import { WikiBrowse } from "./wiki/WikiBrowse";
import { WikiGraphView } from "./wiki/WikiGraphView";
import { WikiSourcesManager } from "./wiki/WikiSourcesManager";
import { useWikiTabActions, type WikiTabActions } from "./wiki/WikiTabActions";
import {
  summarizeWikiStatus,
  type WikiFetchScope,
  type WikiStatusSummary,
} from "./wiki/WikiTabData";
import type { WikiMode } from "./wiki/WikiTabModel";
import {
  loadStoredMode,
  loadStoredTopic,
  storeLastPage,
  storeMode,
  storeTopic,
  useWikiNav,
  type WikiNav,
} from "./wiki/WikiTabState";
import { WikiDegradedBanner, WikiTabToolbar } from "./wiki/WikiTabToolbar";

interface WikiTabProps {
  projectId?: string | null;
  /** Full-panel takeover for the graph view — mirrors the memory tab. */
  requestPanelOverride?: () => void;
  releasePanelOverride?: () => void;
}

type WikiView = "main" | "sources" | "graph";

const WIDE_THRESHOLD = 560;

const noop = () => {};

const ghostButton =
  "rounded-md border border-border px-2 py-1 text-xs text-muted-foreground hover:bg-muted hover:text-foreground";

interface ModeBodyProps {
  mode: WikiMode;
  summary: WikiStatusSummary;
  scope: WikiFetchScope;
  nav: WikiNav;
  search: string;
  wide: boolean;
  actions: WikiTabActions;
  refreshSeq: number;
  onOpenGraph: () => void;
  /** Unresolved-citation fallback (§5.1): wiki-mode vault search. */
  onSearchVault: (query: string) => void;
}

function ModeBody({
  mode,
  summary,
  scope,
  nav,
  search,
  wide,
  actions,
  refreshSeq,
  onOpenGraph,
  onSearchVault,
}: ModeBodyProps) {
  // Loading counts as offline for the ask composer until the gateway's state is known.
  const offline = summary.state === "unavailable" || summary.state === "loading";
  if (mode === "ask") {
    return (
      <WikiAskMode scope={scope} nav={nav} offline={offline} onSearchVault={onSearchVault} />
    );
  }
  return (
    <WikiBrowse
      mode={mode}
      scope={scope}
      nav={nav}
      search={search}
      wide={wide}
      actions={actions}
      refreshSeq={refreshSeq}
      onOpenGraph={onOpenGraph}
    />
  );
}

export const WikiTab = memo(function WikiTab({
  projectId = null,
  requestPanelOverride = noop,
  releasePanelOverride = noop,
}: WikiTabProps) {
  const [mode, setMode] = useState<WikiMode>(() => loadStoredMode());
  const [topic, setTopic] = useState<string | null>(() => loadStoredTopic());
  const [search, setSearch] = useState("");
  const [view, setView] = useState<WikiView>("main");
  const [topicOpen, setTopicOpen] = useState(false);
  const [topicDraft, setTopicDraft] = useState("");
  const [ingestOpen, setIngestOpen] = useState(false);
  const [ingestDraft, setIngestDraft] = useState("");
  const [wide, setWide] = useState(true);
  // Bumped after successful writes so the browse pages/graph fetches re-run.
  const [browseRefreshSeq, setBrowseRefreshSeq] = useState(0);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const dirtyGuard = useDirtyGuard();
  const wiki = useWiki({ projectId, topic });
  const scope = useMemo<WikiFetchScope>(() => ({ projectId, topic }), [projectId, topic]);
  const summary = useMemo(
    () => summarizeWikiStatus(wiki.status, wiki.health, wiki.error, wiki.isLoading),
    [wiki.error, wiki.health, wiki.isLoading, wiki.status],
  );

  useEffect(() => {
    const element = containerRef.current;
    if (!element) return;
    setWide(element.getBoundingClientRect().width >= WIDE_THRESHOLD);
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(([entry]) => {
      if (entry) setWide(entry.contentRect.width >= WIDE_THRESHOLD);
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const applyMode = useCallback((next: WikiMode) => {
    setMode(next);
    storeMode(next);
  }, []);

  const handleModeChange = useCallback(
    (next: WikiMode) => {
      if (next === mode) return;
      void dirtyGuard.guardedRun(() => applyMode(next));
    },
    [applyMode, dirtyGuard, mode],
  );

  const nav = useWikiNav({
    guardedRun: dirtyGuard.guardedRun,
    onNavigate: (entry) => {
      applyMode(entry.mode);
      if (entry.mode === "wiki" || entry.mode === "code") {
        storeLastPage(entry.mode, entry.path);
      }
    },
  });

  const actions = useWikiTabActions({
    scope,
    wiki,
    onRefetch: async () => {
      setBrowseRefreshSeq((seq) => seq + 1);
      await wiki.refresh();
    },
    onNavigateBack: () => nav.back(),
  });

  const handleOpenGraph = useCallback(() => {
    void dirtyGuard.guardedRun(() => {
      setView("graph");
      requestPanelOverride();
    });
  }, [dirtyGuard, requestPanelOverride]);

  // §5.1 unresolved-citation fallback: seed the toolbar search and flip to
  // wiki mode so the flat match list takes over.
  const handleSearchVault = useCallback(
    (query: string) => {
      setSearch(query);
      void dirtyGuard.guardedRun(() => applyMode("wiki"));
    },
    [applyMode, dirtyGuard],
  );

  const handleTopicSubmit = useCallback(
    (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      const next = topicDraft.trim() || null;
      setTopic(next);
      storeTopic(next);
      setTopicOpen(false);
    },
    [topicDraft],
  );

  const handleTopicClear = useCallback(() => {
    setTopic(null);
    storeTopic(null);
    setTopicDraft("");
    setTopicOpen(false);
  }, []);

  const handleIngestSubmit = useCallback(
    (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      const url = ingestDraft.trim();
      if (!url) return;
      void actions.ingestUrl(url);
      setIngestDraft("");
      setIngestOpen(false);
    },
    [actions, ingestDraft],
  );

  if (view === "sources") {
    return (
      <WikiSourcesManager
        sources={wiki.sources}
        isLoading={wiki.isLoading}
        error={wiki.error}
        onClose={() => setView("main")}
        removeSource={wiki.removeSource}
        onRemoved={wiki.refresh}
      />
    );
  }

  if (view === "graph") {
    return (
      <WikiGraphView
        scope={scope}
        initialInclude={mode === "code" ? "code" : null}
        onOpenPage={(path) => {
          setView("main");
          void nav.openPage(path);
        }}
        onClose={() => setView("main")}
        releasePanelOverride={releasePanelOverride}
      />
    );
  }

  return (
    <div ref={containerRef} className="flex h-full min-h-0 flex-col">
      <WikiTabToolbar
        mode={mode}
        onModeChange={handleModeChange}
        search={search}
        onSearchChange={setSearch}
        wide={wide}
        onOpenGraph={handleOpenGraph}
        actionsDisabled={summary.state === "unavailable" || summary.state === "loading"}
        actions={{
          onRefreshIndex: () => void actions.refreshIndex(),
          onCompile: () => void actions.runCompile(),
          onAudit: () => void actions.runAudit(),
          onAttachFile: () => fileInputRef.current?.click(),
          onIngestUrl: () => setIngestOpen((open) => !open),
          onManageSources: () => setView("sources"),
          onTopicScope: () => {
            setTopicDraft(topic ?? "");
            setTopicOpen((open) => !open);
          },
        }}
      />
      <WikiDegradedBanner summary={summary} />

      {topicOpen ? (
        <form
          onSubmit={handleTopicSubmit}
          className="flex items-center gap-2 border-b border-border px-3 py-2"
        >
          <label htmlFor="wiki-topic-scope" className="text-xs text-muted-foreground">
            Topic scope
          </label>
          <input
            id="wiki-topic-scope"
            name="wiki-topic-scope"
            value={topicDraft}
            onChange={(event) => setTopicDraft(event.target.value)}
            placeholder="All topics"
            className="min-w-0 flex-1 rounded-md border border-border bg-transparent px-2 py-1 text-sm text-foreground"
          />
          <button type="submit" className={ghostButton}>
            Apply
          </button>
          <button type="button" onClick={handleTopicClear} className={ghostButton}>
            Clear
          </button>
        </form>
      ) : null}

      {ingestOpen ? (
        <form
          onSubmit={handleIngestSubmit}
          className="flex items-center gap-2 border-b border-border px-3 py-2"
        >
          <label htmlFor="wiki-ingest-url" className="text-xs text-muted-foreground">
            Ingest URL
          </label>
          <input
            id="wiki-ingest-url"
            name="wiki-ingest-url"
            type="url"
            value={ingestDraft}
            onChange={(event) => setIngestDraft(event.target.value)}
            placeholder="https://…"
            className="min-w-0 flex-1 rounded-md border border-border bg-transparent px-2 py-1 text-sm text-foreground"
          />
          <button type="submit" className={ghostButton}>
            Ingest
          </button>
        </form>
      ) : null}

      {actions.status.error ? (
        <p role="alert" className="border-b border-border px-3 py-1.5 text-xs text-destructive-foreground">
          {actions.status.error}
        </p>
      ) : actions.status.message ? (
        <p role="status" className="border-b border-border px-3 py-1.5 text-xs text-muted-foreground">
          {actions.status.message}
        </p>
      ) : null}

      <ModeBody
        mode={mode}
        summary={summary}
        scope={scope}
        nav={nav}
        search={search}
        wide={wide}
        actions={actions}
        refreshSeq={browseRefreshSeq}
        onOpenGraph={handleOpenGraph}
        onSearchVault={handleSearchVault}
      />

      <input
        ref={fileInputRef}
        type="file"
        className="hidden"
        aria-label="Attach file to wiki"
        onChange={(event) => {
          const file = event.target.files?.[0];
          if (file) void actions.attachFile(file);
          event.target.value = "";
        }}
      />
    </div>
  );
});
