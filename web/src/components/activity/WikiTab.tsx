/**
 * Four-mode wiki shell (plan wiki-obsidian-panel §2.2): Wiki | Code | Ask |
 * Research with persisted mode/scope, dirty-guarded transitions, and the
 * kebab action surface. Mode bodies are placeholders until the browse,
 * graph, and ask/research milestones replace them (§P3–P5).
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
import { WikiSourcesManager } from "./wiki/WikiSourcesManager";
import { useWikiTabActions } from "./wiki/WikiTabActions";
import {
  summarizeWikiStatus,
  type WikiFetchScope,
  type WikiStatusSummary,
} from "./wiki/WikiTabData";
import type { WikiMode } from "./wiki/WikiTabModel";
import {
  loadLastPage,
  loadStoredMode,
  loadStoredTopic,
  storeLastPage,
  storeMode,
  storeTopic,
  useWikiNav,
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

function ModeBody({ mode, summary }: { mode: WikiMode; summary: WikiStatusSummary }) {
  const offline = summary.state === "unavailable";
  if (mode === "ask" || mode === "research") {
    const isAsk = mode === "ask";
    return (
      <div className="flex min-h-0 flex-1 flex-col gap-2 px-4 py-6">
        <textarea
          aria-label={isAsk ? "Ask the wiki" : "Research prompt"}
          disabled
          rows={3}
          placeholder={isAsk ? "Ask a grounded question…" : "Describe a research run…"}
          className="max-w-[65ch] resize-none rounded-md border border-border bg-transparent px-2 py-1.5 text-sm text-foreground placeholder:text-muted-foreground disabled:opacity-60"
        />
        <p className="max-w-[65ch] text-xs text-muted-foreground">
          {offline
            ? "The wiki gateway is unreachable — the composer is disabled until it recovers."
            : isAsk
              ? "Grounded Q&A over the wiki lands with the ask milestone."
              : "Research runs launch the wiki-research pipeline; the composer lands with the research milestone."}
        </p>
      </div>
    );
  }
  const lastPage = loadLastPage(mode);
  return (
    <div className="flex min-h-0 flex-1 flex-col items-start gap-2 px-4 py-6">
      <h3 className="text-sm font-semibold text-foreground">
        {mode === "code" ? "Code wiki" : "Wiki"}
      </h3>
      <p className="max-w-[65ch] text-sm text-muted-foreground">
        The page tree and reader land with the browse milestone. Refresh, compile, audit,
        attach, and ingest are live now from the actions menu.
      </p>
      {lastPage ? (
        <p className="font-mono text-xs text-muted-foreground">Last page: {lastPage}</p>
      ) : null}
    </div>
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
  const containerRef = useRef<HTMLDivElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const dirtyGuard = useDirtyGuard();
  const wiki = useWiki({ projectId, topic });
  const scope = useMemo<WikiFetchScope>(() => ({ projectId, topic }), [projectId, topic]);
  const summary = useMemo(
    () => summarizeWikiStatus(wiki.status, wiki.health, wiki.error),
    [wiki.error, wiki.health, wiki.status],
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
    onRefetch: wiki.refresh,
    onNavigate: (path) => nav.openPage(path),
    onNavigateBack: () => nav.back(),
  });

  const handleOpenGraph = useCallback(() => {
    void dirtyGuard.guardedRun(() => {
      setView("graph");
      requestPanelOverride();
    });
  }, [dirtyGuard, requestPanelOverride]);

  const handleCloseGraph = useCallback(() => {
    setView("main");
    releasePanelOverride();
  }, [releasePanelOverride]);

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
      <div className="flex h-full min-h-0 flex-col">
        <div className="flex items-center gap-2 border-b border-border px-3 py-2">
          <h3 className="text-sm font-semibold text-foreground">Wiki graph</h3>
          <button type="button" onClick={handleCloseGraph} className={`ml-auto ${ghostButton}`}>
            Close graph
          </button>
        </div>
        <p className="max-w-[65ch] px-4 py-6 text-sm text-muted-foreground">
          The interactive graph lands with the graph milestone. This view already takes the
          full activity panel via the panel override.
        </p>
      </div>
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
        actionsDisabled={summary.state === "unavailable"}
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
        <p role="alert" className="border-b border-border px-3 py-1.5 text-xs text-destructive">
          {actions.status.error}
        </p>
      ) : actions.status.message ? (
        <p role="status" className="border-b border-border px-3 py-1.5 text-xs text-muted-foreground">
          {actions.status.message}
        </p>
      ) : null}

      <ModeBody mode={mode} summary={summary} />

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
