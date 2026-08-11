/**
 * Two-mode wiki shell (plan wiki-obsidian-panel §2.2): Wiki | Code with
 * persisted mode/scope, dirty-guarded transitions, and the kebab action
 * surface. Both modes render the §3.1 browse experience; grounded Q&A is
 * agent-native (#19672), not a panel mode.
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
import { Button } from "../ui/Button";
import { coarseHitAreaCls } from "../ui/controlStyles";
import { Input } from "../ui/Input";
import { useDirtyGuard } from "./dirtyGuard";
import { WikiBrowse } from "./wiki/WikiBrowse";
import { WikiGraphView } from "./wiki/WikiGraphView";
import { WikiSourcesManager } from "./wiki/WikiSourcesManager";
import { useWikiTabActions, type WikiTabActions } from "./wiki/WikiTabActions";
import { summarizeWikiStatus, type WikiFetchScope } from "./wiki/WikiTabData";
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

interface ModeBodyProps {
  mode: WikiMode;
  scope: WikiFetchScope;
  nav: WikiNav;
  search: string;
  wide: boolean;
  actions: WikiTabActions;
  refreshSeq: number;
  onOpenGraph: () => void;
}

function ModeBody({
  mode,
  scope,
  nav,
  search,
  wide,
  actions,
  refreshSeq,
  onOpenGraph,
}: ModeBodyProps) {
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
      storeLastPage(entry.mode, entry.path);
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
          <Input
            id="wiki-topic-scope"
            name="wiki-topic-scope"
            value={topicDraft}
            onChange={(event) => setTopicDraft(event.target.value)}
            placeholder="All topics"
            wrapperClassName="min-w-0 flex-1"
            className="h-8 px-2 text-sm"
          />
          <Button
            type="submit"
            variant="secondary"
            size="sm"
            className={coarseHitAreaCls}
          >
            Apply
          </Button>
          <Button
            type="button"
            variant="secondary"
            size="sm"
            className={coarseHitAreaCls}
            onClick={handleTopicClear}
          >
            Clear
          </Button>
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
          <Input
            id="wiki-ingest-url"
            name="wiki-ingest-url"
            type="url"
            value={ingestDraft}
            onChange={(event) => setIngestDraft(event.target.value)}
            placeholder="https://…"
            wrapperClassName="min-w-0 flex-1"
            className="h-8 px-2 text-sm"
          />
          <Button
            type="submit"
            variant="secondary"
            size="sm"
            className={coarseHitAreaCls}
          >
            Ingest
          </Button>
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
        scope={scope}
        nav={nav}
        search={search}
        wide={wide}
        actions={actions}
        refreshSeq={browseRefreshSeq}
        onOpenGraph={handleOpenGraph}
      />

      <Input
        ref={fileInputRef}
        type="file"
        className="hidden"
        wrapperClassName="hidden"
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
